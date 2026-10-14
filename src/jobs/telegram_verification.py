"""Read-only bot/group checks. No send methods, raw responses or stored secrets."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib

import httpx
from src.models import utcnow

EXPECTED_BOT = 'BKupon8Bot'
TTL = timedelta(minutes=5)
_cached = None
_lock = asyncio.Lock()

HELP = {
    'not_checked': 'Bağlantı henüz doğrulanmadı. Yönetim anahtarıyla mesaj göndermeden kontrol edin.',
    'expired': 'Bağlantı kontrolü eski; bot/grup izinlerini yeniden doğrulayın.',
    'missing': 'Sunucuda bot tokenı ve sayısal sohbet kimliği tanımlanmalı.',
    'invalid': 'Sunucudaki Telegram ayarlarının biçimini kontrol edin.',
    'verified': '@BKupon8Bot kimliği, hedef sohbet erişimi ve uygun izinler doğrulandı. Test mesajı gönderilmedi.',
    'wrong_bot': 'Token @BKupon8Bot kimliğini doğrulamıyor; sunucudaki bot tokenını kontrol edin.',
    'unsupported_chat': 'Hedef sohbet özel sohbet, grup veya süpergrup olmalı; sunucudaki grup kimliğini kontrol edin.',
    'unknown_permissions': 'Grup izinleri tam doğrulanamadı. Bot üyeliğini ve metin/belge izinlerini kontrol edin.',
    'missing_permissions': 'Botun metin veya belge gönderme izni yok. Grup yöneticisi izinleri düzeltmeli.',
    'bot_removed': 'Bot hedef grupta değil; @BKupon8Bot grubun üyesi olmalı.',
    'telegram_chat_not_found': 'Hedef grup bulunamadı. Negatif grup kimliğini ve botun gruba eklenmesini kontrol edin.',
    'telegram_missing_permissions': 'Telegram grup erişimini reddetti; botun grup izinlerini kontrol edin.',
    'telegram_bot_forbidden': 'Botun grup erişimi engellenmiş; üyelik ve izinleri kontrol edin.',
    'telegram_chat_migrated': 'Grup süpergruba dönüşmüş olabilir; sunucudaki grup kimliğini güncelleyin.',
    'telegram_bot_removed': 'Bot gruptan çıkarılmış; üyeliğini kontrol edin.',
    'telegram_401': 'Telegram tokenı kabul etmiyor; sunucudaki tokenı kontrol edin.',
    'telegram_400': 'Telegram isteği geçersiz; bot/grup yapılandırmasını kontrol edin.',
    'telegram_429': 'Telegram kontrol sınırına ulaşıldı; daha sonra yeniden kontrol edin.',
    'unavailable': 'Telegram bağlantısı şu anda doğrulanamıyor. Gönderim yapılmadı.',
}


def _identity():
    from src.jobs.telegram import configuration
    token, chat, valid = configuration()
    key = hashlib.sha256((token + '\0' + chat).encode()).digest()
    return token, chat, valid, key


def _result(state, now=None, text=None, documents=None):
    return dict(status=state, checked_at=now.isoformat() if now else None,
        expected_bot=EXPECTED_BOT, can_send_messages=text, can_send_documents=documents,
        connection_verified=state == 'verified', message=HELP.get(state, HELP['unavailable']),
        read_only=True)


def snapshot(now=None):
    now = now or utcnow()
    token, chat, valid, key = _identity()
    if not valid:
        return _result('missing' if not token or not chat else 'invalid')
    if _cached and _cached[0] == key:
        checked, result = _cached[1:]
        if checked <= now < checked + TTL:
            return deepcopy(result)
        return _result('expired', checked)
    return _result('not_checked')


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code


async def _call(client, token, method, params):
    # POST JSON keeps chat/user IDs out of URL access logs. These methods only read.
    assert method in ('getMe', 'getChat', 'getChatMember')
    response = await client.post(f'https://api.telegram.org/bot{token}/{method}', json=params)
    try:
        body = response.json()
    except ValueError:
        raise VerificationError('unavailable') from None
    if not isinstance(body, dict):
        raise VerificationError('unavailable')
    if response.is_success and body.get('ok') is True and isinstance(body.get('result'), dict):
        return body['result']
    if body.get('ok') is False and 400 <= response.status_code < 500:
        from src.jobs.telegram import telegram_client_error
        code = telegram_client_error(response.status_code, body.get('description'))
        raise VerificationError(code if code in HELP else 'unavailable')
    raise VerificationError('unavailable')


def _permissions(chat, member, bot_id):
    user = member.get('user')
    if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] != bot_id or user.get('is_bot') is not True:
        raise VerificationError('unknown_permissions')
    state = member.get('status')
    if state in ('left', 'kicked') or state == 'restricted' and member.get('is_member') is not True:
        raise VerificationError('bot_removed')
    if state in ('creator', 'administrator'):
        return True, True
    if state == 'restricted':
        permissions = member
    elif state == 'member':
        permissions = chat.get('permissions')
    else:
        raise VerificationError('unknown_permissions')
    if not isinstance(permissions, dict):
        raise VerificationError('unknown_permissions')
    text, documents = permissions.get('can_send_messages'), permissions.get('can_send_documents')
    if type(text) is not bool or type(documents) is not bool:
        raise VerificationError('unknown_permissions')
    return text, documents


async def verify(transport=None, now=None):
    """Bounded, cached checks; provider failures never reserve a delivery or retry."""
    global _cached
    now = now or utcnow()
    current = snapshot(now)
    if current['status'] not in ('not_checked', 'expired'):
        return current
    async with _lock:
        current = snapshot(now)
        if current['status'] not in ('not_checked', 'expired'):
            return current
        token, chat_id, valid, key = _identity()
        if not valid:
            return snapshot(now)
        try:
            async with asyncio.timeout(12), httpx.AsyncClient(timeout=3, follow_redirects=False,
                    trust_env=False, transport=transport) as client:
                me = await _call(client, token, 'getMe', {})
                if type(me.get('id')) is not int or me['id'] <= 0 or me.get('is_bot') is not True or str(me.get('username', '')).casefold() != EXPECTED_BOT.casefold():
                    raise VerificationError('wrong_bot')
                chat = await _call(client, token, 'getChat', {'chat_id': chat_id})
                if type(chat.get('id')) is not int or chat['id'] != int(chat_id) or chat.get('type') not in ('private', 'group', 'supergroup'):
                    raise VerificationError('unsupported_chat')
                if chat['type'] == 'private':
                    text, documents = True, True
                else:
                    member = await _call(client, token, 'getChatMember', {'chat_id': chat_id, 'user_id': me['id']})
                    text, documents = _permissions(chat, member, me['id'])
                result = _result('verified' if text and documents else 'missing_permissions', now, text, documents)
        except asyncio.CancelledError:
            raise
        except VerificationError as exc:
            result = _result(exc.code, now)
        except Exception:
            # Exception messages, provider descriptions and IDs must not be logged.
            result = _result('unavailable', now)
        if _identity()[3] != key:
            return snapshot(now)
        _cached = key, now, result
        return deepcopy(result)


if __name__ == '__main__':
    import json
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))

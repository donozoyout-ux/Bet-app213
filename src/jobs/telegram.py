"""Durable at-most-once Telegram attempts; no blind retries after ambiguity."""
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select,text
from sqlalchemy.exc import IntegrityError
from src.models import SavedCoupon,TelegramDelivery,CollectorRetry,utcnow
from src.jobs.worker import aware
from src.analytics.saved_coupons import automatic
from src.analytics.coupon import DraftRequest,quote_draft

TZ=ZoneInfo('Europe/Istanbul')
DELIVERY_LOCK=213010
log=logging.getLogger(__name__)


class RedactBot(logging.Filter):
    def filter(self,record):
        message=record.getMessage()
        message=re.sub(r'https://api\.telegram\.org/bot[^/\s]+','https://api.telegram.org/bot[REDACTED]',message)
        message=re.sub(r'bot\d+:[A-Za-z0-9_-]+','bot[REDACTED]',message)
        record.msg=message;record.args=()
        return True


for name in ('httpx','httpcore.http11','httpcore.connection'):
    logging.getLogger(name).addFilter(RedactBot())


def configuration():
    token=os.getenv('TELEGRAM_BOT_TOKEN','');chat=os.getenv('TELEGRAM_CHAT_ID','')
    valid=bool(re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token) and re.fullmatch(r'-?[1-9]\d*',chat))
    return token,chat,valid


def window(now):
    local=now.astimezone(TZ)
    if local.hour<12:return None
    hour=18 if local.hour>=18 else 12
    return local.replace(hour=hour,minute=0,second=0,microsecond=0).isoformat()


def message(coupon,items):
    created=aware(coupon.created_at).astimezone(TZ).strftime('%d.%m.%Y %H:%M')
    lines=[f'BetApp213 • Kupon {coupon.root_id[:8]} • Sürüm {coupon.revision}',f'Oluşturulma: {created} Türkiye saati']
    for i,item in enumerate(items,1):
        m=item['match'];r=item['recommendation'];q=item['quote']
        kickoff=datetime.fromisoformat(str(m['kickoff_at'])).astimezone(TZ).strftime('%d.%m %H:%M')
        observed=datetime.fromisoformat(q['observed_at']).astimezone(TZ).strftime('%d.%m %H:%M')
        lines.append(f"{i}. {m['home_team']} — {m['away_team']} ({kickoff})\n{r['label']} • {q['bookmaker']} • {q['price']}\nOran gözlemi: {observed}")
    lines.append(f"Toplam taslak oranı: {coupon.payload['combined_odds']}")
    if coupon.payload.get('mixed_bookmakers'):lines.append('Farklı kaynak fiyatlarının çarpımı; tek bookmaker teklifi değildir.')
    lines.append('Bilgilendirme amaçlıdır. Bahis oynanmadı; başarı veya kazanç garantisi yok.')
    return '\n\n'.join(lines)


def selection_signature(coupon):
    """Observation timestamps or refreshed prices cannot make the same picks a new send."""
    return sorted((i['match']['id'],i['recommendation']['id']) for i in coupon.payload['items'])


async def prepare(db,now,coupon_id=None):
    token,chat,configured=configuration()
    if not configured:return dict(status='disabled')
    slot=None if coupon_id else window(now)
    if coupon_id is None and slot is None:return dict(status='not_due')
    try:
        async with db.session() as session:
            if session.bind.dialect.name=='postgresql':
                if not await session.scalar(text('SELECT pg_try_advisory_xact_lock(:id)'),{'id':DELIVERY_LOCK}):return dict(status='busy')
            # A crashed sender may have reached Telegram. Never automatically resend it.
            for delivery in await session.scalars(select(TelegramDelivery).where(TelegramDelivery.status=='sending',TelegramDelivery.started_at<now-timedelta(minutes=2))):
                delivery.status='uncertain';delivery.error_code='sender_interrupted';delivery.finished_at=now
            if slot:
                existing=await session.scalar(select(TelegramDelivery).where(TelegramDelivery.slot==slot))
                if existing:await session.commit();return dict(status=existing.status,delivery_id=existing.id)
                retry=await session.get(CollectorRetry,'telegram:'+slot)
                if retry and aware(retry.next_attempt_at)>now:await session.commit();return dict(status='waiting_for_quotes')
            coupon=await session.get(SavedCoupon,coupon_id) if coupon_id else await automatic(session,now)
            if coupon is None:
                if slot:
                    key='telegram:'+slot;retry=await session.get(CollectorRetry,key)
                    if retry is None:retry=CollectorRetry(key=key);session.add(retry)
                    retry.failures=0;retry.reason='no_eligible_selections';retry.next_attempt_at=now+timedelta(minutes=5)
                await session.commit();return dict(status='no_eligible_selections')
            if slot:
                # Different immutable versions can still describe the same coupon.
                from src.match_views import day_bounds
                start,end=day_bounds(now.astimezone(TZ).date(),'Europe/Istanbul')
                previous=await session.scalars(select(SavedCoupon).join(TelegramDelivery,TelegramDelivery.coupon_id==SavedCoupon.id).where(TelegramDelivery.started_at>=start,TelegramDelivery.started_at<end))
                if any(selection_signature(old)==selection_signature(coupon) for old in previous):
                    key='telegram:'+slot;retry=await session.get(CollectorRetry,key)
                    if retry is None:retry=CollectorRetry(key=key);session.add(retry)
                    retry.failures=0;retry.reason='duplicate_selections';retry.next_attempt_at=now+timedelta(minutes=5)
                    await session.commit();return dict(status='duplicate_selections')
            existing=await session.scalar(select(TelegramDelivery).where(TelegramDelivery.coupon_id==coupon.id))
            if existing:
                if slot:
                    key='telegram:'+slot;retry=await session.get(CollectorRetry,key)
                    if retry is None:retry=CollectorRetry(key=key);session.add(retry)
                    end=datetime.fromisoformat(slot).replace(hour=18) if datetime.fromisoformat(slot).hour==12 else (datetime.fromisoformat(slot)+timedelta(days=1)).replace(hour=0)
                    retry.failures=0;retry.reason='duplicate_version';retry.next_attempt_at=end
                await session.commit();return dict(status='duplicate_version',delivery_id=existing.id)
            request=DraftRequest(selections=[dict(match_id=i['match']['id'],recommendation_id=i['recommendation']['id'],bookmaker=i['quote']['bookmaker']) for i in coupon.payload['items']])
            quotes=await quote_draft(session,request,now)
            if not quotes['eligible'] or any(q['price']!=i['quote']['price'] for q,i in zip(quotes['items'],coupon.payload['items'])):
                await session.commit();return dict(status='quotes_changed')
            items=[]
            for item,quote in zip(coupon.payload['items'],quotes['items']):items.append({**item,'quote':quote})
            content=message(coupon,items)
            delivery=TelegramDelivery(id=str(uuid.uuid4()),coupon_id=coupon.id,slot=slot,kind='automatic' if slot else 'manual',status='sending',started_at=now,finished_at=None,error_code=None,message_id=None)
            session.add(delivery);await session.commit()
            # The reservation is durable BEFORE any remote side effect.
            return dict(status='claimed',delivery_id=delivery.id,text=content,created_at=aware(coupon.created_at).astimezone(TZ).strftime('%d.%m.%Y %H:%M'),file_date=aware(coupon.created_at).astimezone(TZ).strftime('%Y-%m-%d'),combined_odds=coupon.payload['combined_odds'],selections=len(items))
    except IntegrityError:
        return dict(status='duplicate_claim')


def telegram_client_error(status_code, description):
    """Map Telegram's untrusted error text to an allowlisted, secret-free reason."""
    message = str(description or '').lower()
    if status_code == 400:
        if 'chat not found' in message:
            return 'telegram_chat_not_found'
        if 'not enough rights' in message or 'not enough permissions' in message:
            return 'telegram_missing_permissions'
        if 'message is too long' in message:
            return 'telegram_message_too_long'
        if 'chat was upgraded' in message:
            return 'telegram_chat_migrated'
        if 'bot was kicked' in message:
            return 'telegram_bot_removed'
    if status_code == 403:
        return 'telegram_bot_forbidden'
    return 'telegram_' + str(status_code)


async def dispatch(db,now=None,coupon_id=None,transport=None):
    now=now or utcnow();claim=await prepare(db,now,coupon_id)
    if claim['status']!='claimed':return claim
    token,chat,_=configuration();outcome='uncertain';error='unconfirmed_response';message_id=None
    try:
        async with asyncio.timeout(15),httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False,transport=transport) as client:
            content=claim['text']
            if len(content.encode('utf-16-le'))//2<=4000:
                response=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json=dict(chat_id=chat,text=content,link_preview_options={'is_disabled':True}))
            else:
                response=await client.post(f'https://api.telegram.org/bot{token}/sendDocument',data=dict(chat_id=chat,caption=f"BetApp213 • {claim['selections']} seçim • Toplam {claim['combined_odds']} • Oluşturulma {claim['created_at']} Türkiye saati. Tüm maçlar ve gözlenen fiyatlar ekte; başarı garantisi yok."),files={'document':(f"BetApp213-{claim['file_date']}.txt",content.encode(),'text/plain')})
            try:body=response.json()
            except ValueError:body={}
            result=body.get('result') if isinstance(body,dict) else None
            body=body if isinstance(body,dict) else {}
            if response.is_success and body.get('ok') is True and isinstance(result,dict) and type(result.get('message_id')) is int and result['message_id']>0:
                outcome='sent';error=None;message_id=body['result']['message_id']
            elif body.get('ok') is False and 400<=response.status_code<500:
                outcome='failed';error=telegram_client_error(response.status_code,body.get('description'))
            else:error='unconfirmed_http_'+str(response.status_code)
    except asyncio.CancelledError:
        # The precommitted sending record already prevents a restart from resending.
        raise
    except Exception as exc:
        error='transport_'+type(exc).__name__
    async with db.session() as session:
        row=await session.get(TelegramDelivery,claim['delivery_id']);row.status=outcome;row.error_code=error;row.message_id=message_id;row.finished_at=utcnow();await session.commit()
    log.info('[TELEGRAM] delivery=%s status=%s error=%s',claim['delivery_id'],outcome,error)
    return dict(status=outcome,delivery_id=claim['delivery_id'],error_code=error)


async def send_test_message(transport=None):
    """Manual one-shot test; does not create coupon deliveries or consume a daily slot."""
    token, chat, valid = configuration()
    if not valid:
        return dict(status='disabled')
    try:
        async with asyncio.timeout(12), httpx.AsyncClient(timeout=8, follow_redirects=False,
                trust_env=False, transport=transport) as client:
            response = await client.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                json={'chat_id': chat,
                      'text': 'BetApp213 Telegram bağlantı testi başarılı. Bu bir test mesajıdır; kupon gönderimi değildir.'})
            try:
                body = response.json()
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            result = body.get('result')
            if (response.is_success and body.get('ok') is True and
                    isinstance(result, dict) and type(result.get('message_id')) is int
                    and result['message_id'] > 0):
                return dict(status='sent', message_id=result['message_id'])
            if body.get('ok') is False and 400 <= response.status_code < 500:
                return dict(status='failed',
                    error_code=telegram_client_error(response.status_code, body.get('description')))
            return dict(status='uncertain', error_code='unconfirmed_http_'+str(response.status_code))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return dict(status='uncertain', error_code='transport_'+type(exc).__name__)


async def status(session,now):
    _,_,configured=configuration();today=now.astimezone(TZ).date().isoformat()
    rows=(await session.scalars(select(TelegramDelivery).where(TelegramDelivery.slot.like(today+'%')).order_by(TelegramDelivery.slot))).all()
    entries=[dict(slot=r.slot,status=r.status,error_code=r.error_code) for r in rows]
    known={r.slot for r in rows}
    for retry in await session.scalars(select(CollectorRetry).where(CollectorRetry.key.like('telegram:'+today+'%'))):
        slot=retry.key.removeprefix('telegram:')
        if slot not in known:entries.append(dict(slot=slot,status=retry.reason,error_code=None))
    local=now.astimezone(TZ)
    known={e['slot'] for e in entries}
    for hour in (12,18):
        slot=local.replace(hour=hour,minute=0,second=0,microsecond=0).isoformat()
        if slot not in known:entries.append(dict(slot=slot,status='missed' if hour==12 and local.hour>=18 else 'pending' if local.hour>=hour else 'not_due',error_code=None))
    token,chat,_=configuration()
    state='configured' if configured else 'missing' if not token or not chat else 'invalid'
    from src.jobs.telegram_verification import snapshot
    connection=snapshot(now)
    return dict(configured=configured,configuration_status=state,connection_verified=connection['connection_verified'],connection=connection,timezone='Europe/Istanbul',times=['12:00','18:00'],automatic_daily_limit=2,today=sorted(entries,key=lambda x:x['slot']))


async def loop(db):
    while True:
        try:
            if configuration()[2]:
                if not db.ready:await db.initialize()
                await dispatch(db)
        except asyncio.CancelledError:raise
        except Exception as exc:log.warning('[TELEGRAM] scheduler deferred error=%s',type(exc).__name__)
        await asyncio.sleep(60)

"""Authenticated encryption and bounded validation; no plaintext files or overwrite."""
import base64
import hashlib
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.archive.schema import SCHEMA, MAX_BYTES, canonical, validate


def key_bytes(key):
    try:value=base64.b64decode(key,altchars=b'-_',validate=True)
    except Exception:raise ValueError('Invalid archive encryption key') from None
    if len(value)!=32:raise ValueError('Archive requires a 256-bit key')
    return value


def seal(data,key):
    data=validate(data);raw=canonical(data)
    if len(raw)>MAX_BYTES:raise ValueError('Archive byte limit')
    manifest=dict(schema_version=SCHEMA,created_at=data['created_at'],
                  counts={k:len(data[k]) for k in ('forecasts','decisions','coupons','reports')},
                  plaintext_sha256=hashlib.sha256(raw).hexdigest())
    nonce=os.urandom(12)
    encrypted=AESGCM(key_bytes(key)).encrypt(nonce,raw,canonical(manifest))
    return canonical(dict(manifest=manifest,nonce=base64.b64encode(nonce).decode(),
                          ciphertext=base64.b64encode(encrypted).decode(),
                          ciphertext_sha256=hashlib.sha256(encrypted).hexdigest()))


def unseal(blob,key):
    if len(blob)>MAX_BYTES*2:raise ValueError('Archive byte limit')
    try:
        envelope=json.loads(blob)
        if set(envelope)!={'manifest','nonce','ciphertext','ciphertext_sha256'}:raise ValueError()
        encrypted=base64.b64decode(envelope['ciphertext'],validate=True)
        nonce=base64.b64decode(envelope['nonce'],validate=True)
        if hashlib.sha256(encrypted).hexdigest()!=envelope['ciphertext_sha256']:raise ValueError()
        raw=AESGCM(key_bytes(key)).decrypt(nonce,encrypted,canonical(envelope['manifest']))
        if len(raw)>MAX_BYTES or hashlib.sha256(raw).hexdigest()!=envelope['manifest']['plaintext_sha256']:raise ValueError()
        data=validate(json.loads(raw))
        if envelope['manifest']!=dict(schema_version=SCHEMA,created_at=data['created_at'],counts={k:len(data[k]) for k in ('forecasts','decisions','coupons','reports')},plaintext_sha256=hashlib.sha256(raw).hexdigest()):raise ValueError()
        return data
    except Exception:raise ValueError('Archive authentication or validation failed') from None


def read(path,key):
    with Path(path).open('rb') as source:blob=source.read(MAX_BYTES*2+1)
    return unseal(blob,key)


def write_new(path,blob):
    """Create once with restrictive permissions; cleanup only our incomplete file."""
    path=Path(path)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as target:
            target.write(blob);target.flush();os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise

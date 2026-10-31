"""Full recovery framing: bounded gzip + AES-256-GCM, distinct from history archives."""
import gzip
import os
import zlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .crypto import key_bytes
import hashlib
import json

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

def sha(value):
    return hashlib.sha256(value).hexdigest()

SCHEMA='postgres-recovery-v1'
CHUNK=16*1024*1024
MAX_DUMP=2*1024*1024*1024


def seal(raw,key,backup_id,name):
    if len(raw)>CHUNK:raise ValueError('chunk_budget')
    nonce=os.urandom(12)
    return b'KPR1'+nonce+AESGCM(key_bytes(key)).encrypt(nonce,gzip.compress(raw,mtime=0),canonical([SCHEMA,backup_id,name]))


def unseal(blob,key,backup_id,name):
    if not 32<=len(blob)<=CHUNK+65536 or blob[:4]!=b'KPR1':raise ValueError('invalid_envelope')
    packed=AESGCM(key_bytes(key)).decrypt(blob[4:16],blob[16:],canonical([SCHEMA,backup_id,name]))
    decoder=zlib.decompressobj(31);raw=decoder.decompress(packed,CHUNK+1)
    if len(raw)>CHUNK or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('compressed_budget')
    return raw

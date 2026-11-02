"""Lossless board archive format, separate from the filtered owned-audit format."""
import gzip
import hashlib
import json
import os
import zlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.archive.crypto import key_bytes

SCHEMA = 'board-editions-lossless-v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_KEYS = 100000
MAGIC = b'KAB1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def sha(raw): return hashlib.sha256(raw).hexdigest()


def frame(raw):
    return len(raw).to_bytes(4,'big') + raw


def seal(raw, key, archive_id, name):
    if len(raw) > MAX_BYTES: raise ValueError('Plaintext byte limit exceeded')
    nonce = os.urandom(12)
    aad = canonical([SCHEMA, archive_id, name])
    return MAGIC + nonce + AESGCM(key_bytes(key)).encrypt(nonce, gzip.compress(raw, mtime=0), aad)


def unseal(blob, key, archive_id, name):
    if not 32 <= len(blob) <= MAX_BYTES + 65536 or blob[:4] != MAGIC:
        raise ValueError('Invalid encrypted chunk')
    packed = AESGCM(key_bytes(key)).decrypt(blob[4:16], blob[16:], canonical([SCHEMA, archive_id, name]))
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = decoder.decompress(packed, MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('Invalid or oversized compressed chunk')
    return raw


def records(raw, expected):
    """Validate identities without reserializing JSON numbers or historical data."""
    lines, offset = [], 0
    for key in expected:
        if offset+4 > len(raw): raise ValueError('Incomplete row frame')
        size = int.from_bytes(raw[offset:offset+4],'big'); offset += 4
        if size <= 0 or offset+size > len(raw): raise ValueError('Incomplete row')
        line = raw[offset:offset+size]; offset += size
        row = json.loads(line)
        if set(row) != {'version', 'league_id', 'created_at', 'payload'} or row['version'] != key:
            raise ValueError('Row identity/schema mismatch')
        if not isinstance(row['payload'], dict) or not isinstance(row['league_id'], int):
            raise ValueError('Invalid board row')
        lines.append(line)
    if offset != len(raw): raise ValueError('Unexpected rows or trailing bytes')
    return lines

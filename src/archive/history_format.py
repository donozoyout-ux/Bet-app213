"""Versioned relational history, distinct from filtered owned-audit exports.

No source writes, provider requests, inferred observations or executable SQL in
the format. A generation belongs to ONE source database, never a union of IDs.
"""
import gzip
import hashlib
import json
import math
import os
import zlib
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.archive.crypto import key_bytes
from src.models import Base

SCHEMA = 'match-history-v1'
MAX_CHUNK = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ROWS = 100000
MAX_CHUNKS = 8
# References and immutable evidence, including all versions, not just latest.
TABLES = frozenset(('leagues', 'seasons', 'teams', 'referees', 'bookmakers',
    'matches', 'match_statistics', 'match_events', 'odds_1x2', 'asian_handicap',
    'asian_totals', 'market_odds', 'odds_snapshots', 'prediction_snapshots',
    'prediction_settlements', 'prediction_publications', 'published_match_analyses', 'board_editions',
    'learning_reports', 'learning_slices', 'learning_lessons',
    'learning_ai_explanations', 'learning_heads'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def schema_digest():
    return sha(canonical({name: [(c.name, str(c.type), c.nullable, c.primary_key,
        sorted(f.target_fullname for f in c.foreign_keys))
        for c in Base.metadata.tables[name].columns] for name in sorted(TABLES)}))


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None: raise ValueError('Timezone required')
    return result.astimezone(timezone.utc)


def eligibility(match, evidence, now):
    """Fail closed. 24 hours alone is NOT permission to archive or evict."""
    reasons = []
    try:
        if timestamp(match['kickoff_at']) > now - timedelta(hours=24): reasons.append('too_recent')
    except (ValueError, TypeError, KeyError): reasons.append('unknown_kickoff')
    if match.get('status') != 'finished': reasons.append('not_final')
    if any(type(match.get(k)) is not int or match[k] < 0 for k in ('ft_home', 'ft_away')):
        reasons.append('unknown_result')
    for flag in ('settled', 'provider_final', 'odds_final', 'dependencies_complete'):
        if evidence.get(flag) is not True: reasons.append(flag)
    return reasons


def seal(raw, key, generation, name):
    if len(raw) > MAX_CHUNK: raise ValueError('Chunk limit')
    nonce = os.urandom(12)
    return b'KMH1' + nonce + AESGCM(key_bytes(key)).encrypt(nonce,
        gzip.compress(raw, mtime=0), canonical([SCHEMA, generation, name]))


def unseal(blob, key, generation, name):
    if not 32 <= len(blob) <= MAX_CHUNK + 65536 or blob[:4] != b'KMH1':
        raise ValueError('Invalid encrypted history')
    packed = AESGCM(key_bytes(key)).decrypt(blob[4:16], blob[16:],
        canonical([SCHEMA, generation, name]))
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = decoder.decompress(packed, MAX_CHUNK + 1)
    if len(raw) > MAX_CHUNK or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('Invalid compressed history')
    return raw


def parse(raw):
    def finite(value):
        number = float(value)
        if not math.isfinite(number): raise ValueError('Nonfinite JSON')
        return number
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_float=finite,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def raw_records(raw):
    """Yield table and original row JSON slices without float reserialization."""
    text=raw.decode('utf-8');decoder=json.JSONDecoder();pos=0
    def whitespace(index):
        while index<len(text) and text[index].isspace():index+=1
        return index
    pos=whitespace(pos)
    if text[pos]!='[':raise ValueError('Row array required')
    pos+=1
    while True:
        pos=whitespace(pos)
        if text[pos]==']':break
        if text[pos]!='{':raise ValueError('Row envelope required')
        pos+=1;fields={};slices={}
        while True:
            pos=whitespace(pos);key,end=decoder.raw_decode(text,pos);pos=whitespace(end)
            if text[pos]!=':':raise ValueError('Row separator')
            start=whitespace(pos+1);value,end=decoder.raw_decode(text,start)
            if key in fields:raise ValueError('Duplicate row field')
            fields[key]=value;slices[key]=text[start:end];pos=whitespace(end)
            if text[pos]=='}':pos+=1;break
            if text[pos]!=',':raise ValueError('Row field separator')
            pos+=1
        if set(fields)!={'table','row'}:raise ValueError('Row envelope fields')
        yield fields['table'],slices['row']
        pos=whitespace(pos)
        if text[pos]==']':break
        if text[pos]!=',':raise ValueError('Record separator')
        pos+=1
    if whitespace(pos+1)!=len(text):raise ValueError('Trailing record bytes')


def validate_catalog(data, generation):
    if set(data) != {'schema', 'schema_digest', 'generation', 'source_id', 'created_at',
                     'coverage', 'chunks', 'counts', 'eligibility'}:
        raise ValueError('Catalog fields')
    if data['schema'] != SCHEMA or data['schema_digest'] != schema_digest() or data['generation'] != generation:
        raise ValueError('Catalog version mismatch')
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', data['source_id']): raise ValueError('Source namespace')
    now = timestamp(data['created_at'])
    if now > datetime.now(timezone.utc): raise ValueError('Future catalog')
    coverage = data['coverage']
    if set(coverage) != {'from', 'to', 'league_ids', 'complete'} or coverage['complete'] is not True:
        raise ValueError('Incomplete coverage')
    if timestamp(coverage['from']) >= timestamp(coverage['to']): raise ValueError('Coverage range')
    if not coverage['league_ids'] or any(type(x) is not int or x <= 0 for x in coverage['league_ids']):
        raise ValueError('Coverage leagues')
    counts = data['counts']
    if set(counts) != TABLES or any(type(n) is not int or n < 0 for n in counts.values()) or sum(counts.values()) > MAX_ROWS:
        raise ValueError('Missing tables/count limit')
    if not counts['matches']: raise ValueError('Empty history scope')
    if not isinstance(data['eligibility'], dict) or len(data['eligibility']) != counts['matches']:
        raise ValueError('Missing eligibility attestations')
    if not 1 <= len(data['chunks']) <= MAX_CHUNKS: raise ValueError('Chunk count limit')
    names, ids, total = set(), set(), 0
    for chunk in data['chunks']:
        if set(chunk) != {'id', 'name', 'size', 'sha256', 'raw_sha256', 'raw_bytes', 'rows'}:
            raise ValueError('Chunk metadata')
        if not re.fullmatch(r'chunk-\d{6}\.enc', chunk['name']) or chunk['name'] in names or chunk['id'] in ids:
            raise ValueError('Duplicate/invalid chunk')
        if type(chunk['id']) is not int or chunk['id'] <= 0: raise ValueError('Invalid asset ID')
        for field in ('sha256', 'raw_sha256'):
            if not re.fullmatch('[a-f0-9]{64}', chunk[field]): raise ValueError('Invalid digest')
        if type(chunk['size']) is not int or not 32 <= chunk['size'] <= MAX_CHUNK + 65536:
            raise ValueError('Encrypted size limit')
        if type(chunk['raw_bytes']) is not int or not 0 < chunk['raw_bytes'] <= MAX_CHUNK:
            raise ValueError('Plaintext size limit')
        if type(chunk['rows']) is not int or not 0 <= chunk['rows'] <= MAX_ROWS: raise ValueError('Row limit')
        names.add(chunk['name']); ids.add(chunk['id']); total += chunk['raw_bytes']
    if total > MAX_TOTAL or sum(c['rows'] for c in data['chunks']) != sum(counts.values()):
        raise ValueError('Aggregate budget/count mismatch')
    return data

"""Bounded PostgreSQL reads. Caller must enter a read-only transaction."""
import json

SNAPSHOTS = "SELECT p.match_id,p.league_id,l.name league,p.captured_at,p.model_version,p.picks::text picks_json,\njsonb_build_object('id',m.id,'external_match_id',m.external_match_id,'status',m.status,'kickoff_at',m.kickoff_at,'ft_home',m.ft_home,'ft_away',m.ft_away,'ht_home',m.ht_home,'ht_away',m.ht_away,'status_observed_at',m.status_observed_at) match,\nto_jsonb(st)-'raw' stats,\ncoalesce((SELECT jsonb_agg(to_jsonb(d) || jsonb_build_object('evidence_json',d.evidence::text)) FROM prediction_settlements d WHERE d.match_id=p.match_id),'[]') decisions\nFROM prediction_snapshots p JOIN matches m ON m.id=p.match_id JOIN leagues l ON l.id=p.league_id\nLEFT JOIN match_statistics st ON st.match_id=m.id WHERE p.match_id>$1 ORDER BY p.match_id LIMIT $2"
COUPONS = "SELECT c.id,c.created_at,c.kind,c.revision,c.root_id,c.payload->'items' items FROM saved_coupons c WHERE c.id>$1 ORDER BY c.id LIMIT $2"

def decode(row):
    row = dict(row)
    for key in ('match', 'stats', 'decisions', 'items'):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    if 'picks_json' in row:
        row['picks'] = json.loads(row.pop('picks_json'))
    for decision in row.get('decisions', []):
        # JSON text preserves numeric lexemes used by the historical SHA-256.
        decision['evidence'] = json.loads(decision.pop('evidence_json'))
    return row


async def collect(conn, batch_size=50):
    if not 1 <= batch_size <= 50:
        raise ValueError('Batch size must be between 1 and 50')
    if await conn.fetchval("SHOW transaction_read_only") != 'on':
        raise ValueError('Audit requires a read-only transaction')
    observed = await conn.fetchval("SELECT transaction_timestamp()")
    async def pages(sql, cursor, key):
        rows = []
        while True:
            page = [decode(r) for r in await conn.fetch(sql, cursor, batch_size, timeout=5)]
            rows.extend(page)
            if len(page) < batch_size:
                return rows
            cursor = page[-1][key]
    snapshots = await pages(SNAPSHOTS, 0, 'match_id')
    coupons = await pages(COUPONS, '', 'id')
    ids = sorted({i['match']['id'] for c in coupons for i in (c['items'] or [])})
    matches = []
    for start in range(0, len(ids), batch_size):
        rows = await conn.fetch("""SELECT m.id,m.external_match_id,m.league_id,l.name league,
            m.status,m.kickoff_at,m.ft_home,m.ft_away,m.ht_home,m.ht_away,m.status_observed_at,
            to_jsonb(st)-'raw' stats FROM matches m JOIN leagues l ON l.id=m.league_id
            LEFT JOIN match_statistics st ON st.match_id=m.id WHERE m.id=ANY($1::int[])
            ORDER BY m.id""", ids[start:start+batch_size], timeout=5)
        matches.extend(decode(row) for row in rows)
    # Normalize typed driver timestamps consistently without losing JSON numbers.
    def serial(value):
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        raise TypeError(type(value).__name__)
    return json.loads(json.dumps(dict(snapshots=snapshots, coupons=coupons,
        coupon_matches=matches, observed_at=observed.isoformat()), default=serial))

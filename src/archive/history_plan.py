"""Read-only, dependency-complete planner. Missing facts are retained, not invented."""
from collections import Counter
from datetime import timedelta
import re
from sqlalchemy import text, bindparam
from src.models import Base
from .history_format import TABLES, MAX_CHUNK, MAX_TOTAL, MAX_ROWS, MAX_CHUNKS, canonical, sha, parse, timestamp, eligibility

MAX_INVENTORY = 100000
PARTITION_MATCHES = 32
DIRECT = tuple(sorted(name for name in TABLES if 'match_id' in Base.metadata.tables[name].c))
BOOKS = ('Crown', 'Bet365', 'Sbobet')
MARKETS = tuple(f'{b}:{period}:{market}' for b in BOOKS for period in ('full_time','first_half') for market in ('1x2','ah','ou'))


class PlanBlocked(ValueError):
    pass


class HistorySource:
    def __init__(self, connection, namespace, schema='public'):
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}', namespace): raise ValueError('Invalid source namespace')
        if not re.fullmatch('[a-z][a-z0-9_]{0,62}', schema): raise ValueError('Invalid schema')
        self.connection, self.namespace, self.schema = connection, namespace, schema

    def table(self, name):
        if name not in TABLES | {'scraper_job_items', 'scraper_jobs', 'learning_ai_tasks'}: raise ValueError('Unapproved table')
        return f'"{self.schema}"."{name}"'

    async def fingerprint(self):
        row=(await self.connection.execute(text('SELECT current_database(),to_regclass(:name)::oid'),{'name':self.table('matches')})).one()
        if row[1] is None:raise PlanBlocked('source_schema_missing')
        return sha(canonical(list(row)))

    async def inventory(self):
        await self.connection.execute(text("SET LOCAL timezone='UTC'"))
        query = text(f'''SELECT id,external_match_id,league_id,season_id,kickoff_at,status
            FROM {self.table('matches')} WHERE kickoff_at >= '2024-01-01T00:00:00Z'
            OR kickoff_at IS NULL ORDER BY league_id,season_id,id LIMIT :cap''')
        rows = [dict(r) for r in (await self.connection.execute(query, {'cap':MAX_INVENTORY+1})).mappings()]
        if len(rows)>MAX_INVENTORY: raise PlanBlocked('inventory_budget_exceeded_no_truncation')
        return rows

    async def rows(self, name, column=None, keys=()):
        from src.models import Base
        table = Base.metadata.tables[name]
        if column is not None and column not in table.c: raise ValueError('Invalid filter')
        if column is not None and not keys: return []
        where = f'WHERE b."{column}" IN :keys' if column else ''
        order = ','.join(f'b."{c.name}"' for c in table.primary_key)
        query = text(f'''SELECT CASE WHEN octet_length(row_to_json(b)::text)<=:cap
            THEN row_to_json(b)::text END FROM {self.table(name)} b {where}
            ORDER BY {order} LIMIT :limit''')
        if column: query = query.bindparams(bindparam('keys', expanding=True))
        params = {'cap':MAX_CHUNK-128,'limit':MAX_ROWS+1}
        if column: params['keys']=sorted(set(keys))
        records, total = [], 0
        stream = await self.connection.stream(query, params)
        try:
            async for (value,) in stream:
                if value is None: raise PlanBlocked('individual_row_budget')
                raw = b'{"table":'+canonical(name)+b',"row":'+value.encode()+b'}'
                total += len(raw)
                if total>MAX_TOTAL or len(records)>=MAX_ROWS: raise PlanBlocked('dependency_budget')
                records.append((parse(value), raw))
        finally: await stream.close()
        return records

    async def reports_for(self, ids):
        query = text(f'''SELECT id FROM {self.table('learning_reports')} r
            WHERE EXISTS (SELECT 1 FROM json_array_elements(r.source_manifest) x
            WHERE (x->>'match_id')::bigint IN :ids) ORDER BY id LIMIT :cap''').bindparams(bindparam('ids', expanding=True))
        result = list(await self.connection.scalars(query, {'ids':sorted(ids),'cap':MAX_ROWS+1}))
        if len(result)>MAX_ROWS: raise PlanBlocked('report_dependency_budget')
        return result

    async def processing_complete(self, ids, reports):
        query = text(f"SELECT count(*) FROM {self.table('scraper_job_items')} WHERE match_id IN :ids AND status NOT IN ('completed','no_odds')").bindparams(bindparam('ids', expanding=True))
        if await self.connection.scalar(query, {'ids':sorted(ids)}): return False
        if reports:
            query = text(f"SELECT count(*) FROM {self.table('learning_ai_tasks')} WHERE report_id IN :ids AND status <> 'completed'").bindparams(bindparam('ids', expanding=True))
            if await self.connection.scalar(query, {'ids':sorted(reports)}): return False
        return True


def finality(match, rows, now, processing):
    """Provider absence must be explicitly evidenced, never interpreted as zero.

    Legacy source records without finality evidence remain hot with named gaps.
    No exporter manufactures these attestations.
    """
    evidence = (match.get('raw') or {}).get('archive_finality', {})
    flags = dict(settled=True, provider_final=False, odds_final=False, dependencies_complete=processing)
    gaps = []
    try:
        observed = timestamp(evidence['observed_at'])
        valid_time = timestamp(match['kickoff_at'])+timedelta(hours=3) <= observed <= now
    except (KeyError, TypeError, ValueError): valid_time=False
    if not valid_time or evidence.get('events_final') is not True: gaps.append('provider_finality_unverified')
    flags['provider_final'] = valid_time and evidence.get('events_final') is True
    for field in ('ht_home','ht_away'):
        if type(match.get(field)) is not int: gaps.append('missing_'+field)
    stats = [r for t,r in rows if t=='match_statistics']
    if not stats or stats[0].get('is_final') is not True: gaps.append('statistics_not_final')
    else:
        for field in ('home_corners','away_corners','home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards'):
            if stats[0].get(field) is None: gaps.append('missing_'+field)
    market_evidence = evidence.get('markets', {})
    for market in MARKETS:
        item = market_evidence.get(market, {})
        # Each cell needs provenance for observed prices OR provider-confirmed
        # unavailability. Unavailable remains a visible coverage gap forever.
        if item.get('status') not in {'observed','unavailable'} or not item.get('source_reference'):
            gaps.append('unverified_market:'+market)
        elif item['status']=='unavailable':
            gaps.append('unavailable_market:'+market)
        else:
            import math
            kind=market.rsplit(':',1)[1]
            fields=('home','draw','away') if kind=='1x2' else ('line','home','away') if kind=='ah' else ('line','over','under')
            for stage in ('opening','closing'):
                for field in fields:
                    value=item.get(stage,{}).get(field)
                    if type(value) not in (int,float) or not math.isfinite(value) or (field!='line' and value<=1):
                        gaps.append('unverified_market:'+market)
    flags['odds_final'] = valid_time and not any(x.startswith('unverified_market:') for x in gaps) and match.get('odds_complete') is True
    snapshots = [r for t,r in rows if t=='prediction_snapshots']
    decisions = {r['pick_index'] for t,r in rows if t=='prediction_settlements'}
    flags['settled'] = all(set(range(len(s['picks'])))<=decisions for s in snapshots)
    blockers = eligibility(match, flags, now) + [x for x in gaps if not x.startswith('unavailable_market:')]
    return flags, sorted(set(blockers)), gaps


async def bundle(source, ids, now):
    """Close both relational and embedded lesson references; never omit a lesson."""
    ids = set(ids)
    for _ in range(8):
        report_ids = await source.reports_for(ids)
        reports = await source.rows('learning_reports','id',report_ids)
        expanded = ids | {int(ref['match_id']) for row,_ in reports for ref in row['source_manifest']}
        if expanded == ids: break
        if len(expanded)>PARTITION_MATCHES*8: raise PlanBlocked('lesson_closure_budget')
        ids = expanded
    else: raise PlanBlocked('dependency_closure_depth')
    data = {name:[] for name in TABLES}
    data['matches'] = await source.rows('matches','id',ids)
    if {row['id'] for row,_ in data['matches']} != ids: raise PlanBlocked('missing_match_dependency')
    for name in DIRECT: data[name] = await source.rows(name,'match_id',ids)
    data['learning_reports'] = reports
    for name in ('learning_slices','learning_lessons','learning_ai_explanations','learning_heads'):
        data[name] = await source.rows(name,'report_id',report_ids)
    matches = [r for r,_ in data['matches']]
    if any(timestamp(m['kickoff_at']).year<2024 for m in matches):raise PlanBlocked('dependency_outside_2024_scope')
    data['leagues'] = await source.rows('leagues','id',{m['league_id'] for m in matches})
    data['seasons'] = await source.rows('seasons','id',{m['season_id'] for m in matches})
    data['teams'] = await source.rows('teams','id',{m[k] for m in matches for k in ('home_team_id','away_team_id')})
    data['referees'] = await source.rows('referees','id',{m['referee_id'] for m in matches if m['referee_id'] is not None})
    book_ids = {r['bookmaker_id'] for values in data.values() for r,_ in values if 'bookmaker_id' in r}
    data['bookmakers'] = await source.rows('bookmakers','id',book_ids)
    versions = {r['board_version'] for name in ('prediction_publications','published_match_analyses') for r,_ in data[name]}
    data['board_editions'] = await source.rows('board_editions','version',versions)
    # Board editions can refer to another league; retain their league reference.
    league_ids = {m['league_id'] for m in matches} | {r['league_id'] for r,_ in data['board_editions']}
    data['leagues'] = await source.rows('leagues','id',league_ids)
    processing = await source.processing_complete(ids, report_ids)
    attestations, gaps = {}, {}
    rows = [(name,row) for name,values in data.items() for row,_ in values]
    for match in matches:
        related = [(t,r) for t,r in rows if r.get('match_id') == match['id']]
        flags, reasons, missing = finality(match, related, now, processing)
        if reasons: raise PlanBlocked('match_'+str(match['id'])+':'+','.join(reasons))
        attestations[str(match['id'])] = flags
        gaps[str(match['id'])] = missing
    encoded = [raw for name in sorted(TABLES) for _,raw in data[name]]
    if len(encoded)>MAX_ROWS: raise PlanBlocked('row_budget')
    chunks, current, size = [], [], 2
    for raw in encoded:
        if len(raw)+2>MAX_CHUNK: raise PlanBlocked('row_chunk_budget')
        if size+len(raw)+1>MAX_CHUNK:
            chunks.append(b'['+b','.join(current)+b']'); current=[]; size=2
        current.append(raw); size+=len(raw)+1
    if current: chunks.append(b'['+b','.join(current)+b']')
    if len(chunks)>MAX_CHUNKS or sum(map(len,chunks))>MAX_TOTAL: raise PlanBlocked('generation_budget')
    generation = sha(canonical([source.namespace,sorted(ids),[sha(c) for c in chunks]]))
    teams = {r['id']:r['external_id'] for r,_ in data['teams']}
    leagues = {r['id']:r for r,_ in data['leagues']}
    seasons = {r['id']:r['season_name'] for r,_ in data['seasons']}
    index = [dict(identity=f'{source.namespace}:{m["external_match_id"]}', id=m['id'],
        external_match_id=m['external_match_id'], provider=leagues[m['league_id']]['source'],
        league=leagues[m['league_id']]['external_id'], season=seasons[m['season_id']],
        home=teams[m['home_team_id']], away=teams[m['away_team_id']], kickoff_at=m['kickoff_at']) for m in matches]
    return dict(generation=generation, chunks=chunks, counts={n:len(data[n]) for n in TABLES},
        eligibility=attestations, gaps=gaps, index=index,
        coverage=dict(from_=min(m['kickoff_at'] for m in matches), to=now.isoformat(), league_ids=sorted({m['league_id'] for m in matches}), complete=True))


async def plan(source, now):
    inventory = await source.inventory()
    groups, retained, covered = {}, [], set()
    for row in inventory:
        if row['status'] != 'finished' or row['kickoff_at'] is None or row['kickoff_at']>now-timedelta(hours=24):
            retained.append(dict(id=row['id'], reason='not_finished_or_24h'))
        else: groups.setdefault((row['league_id'],row['season_id']),[]).append(row['id'])
    for scope, ids in sorted(groups.items()):
        for start in range(0,len(ids),PARTITION_MATCHES):
            selected = set(ids[start:start+PARTITION_MATCHES])-covered
            if not selected: continue
            try: result = await bundle(source, selected, now)
            except PlanBlocked as exc:
                # A blocked group is split down to individual matches so one
                # missing provider observation cannot hide eligible neighbours.
                for mid in sorted(selected):
                    if mid in covered: continue
                    try: result = await bundle(source, [mid], now)
                    except PlanBlocked as error:
                        retained.append(dict(id=mid, reason=str(error)[:2000])); continue
                    covered.update(int(x) for x in result['eligibility'])
                    yield result
            else:
                covered.update(int(x) for x in result['eligibility'])
                yield result
    yield dict(report=True, inventory_count=len(inventory), covered_ids=sorted(covered),
        retained=[r for r in retained if r['id'] not in covered], namespace=source.namespace)

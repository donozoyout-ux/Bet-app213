"""Final-data repair runs on real tables without modifying saved predictions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import json
import os
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from src.models import Match, MatchStatistics, PredictionSnapshot, CollectorRetry
from src.db import Database
from src.analytics.performance import settle, performance
from src.jobs.live import PriorityUpdater
from src.jobs.settlement import recheck, MAX_ATTEMPTS
from src.scrapers.goaloo.live import parse_scoreboard
from src.scrapers.goaloo.client import SourceError
from tests.test_live_updater import seed_match, NOW, Source

PICK=dict(id='over_0_5_second_half_goals',market='second_half_goals',selection='over_0_5',
          label='0.5 İkinci Yarı Gol Üst',probability=.8,reliability=.85,sample_size=20,score=.85,confidence='high')
FINAL="<div class='end'><div class='score'>2</div><div>Finished <span title='Score 1st Half'>1-0</span></div><div class='score'>1</div></div>"


def test_real_production_october_13_to_15_replay():
    data=json.loads(Path('tests/fixtures/settlement_production_20261015.json').read_text(encoding='utf-8'))
    now=datetime.fromisoformat(data['observed_at'].replace('Z','+00:00'))
    counts={}
    for row in data['matches']:
        m=SimpleNamespace(**{**row,'kickoff_at':datetime.fromisoformat(row['kickoff_at'].replace('Z','+00:00'))})
        for pick in row['picks']:
            outcome,_=settle(m,m,pick,now)
            counts[outcome]=counts.get(outcome,0)+1
    assert counts==dict(won=8,lost=4,pending=10)


@pytest.mark.parametrize('market,selection,expected',[
    ('goals','over_2_5','won'),('first_half_goals','under_1_5','won'),
    ('second_half_goals','over_1_5','won'),('corners','under_8_5','lost'),
    ('yellow_cards','over_2_5','won'),('red_cards','under_0_5','won'),
    ('asian_handicap','home_minus_1_0','void'),('asian_handicap','away_plus_0_5','lost'),
    ('result','home','won'),('result','draw','lost'),('btts','yes','won'),
    ('double_chance','x2','lost'),('goals','over_3_0','void'),
    ('goals','wrong','awaiting_data'),('asian_handicap','home_plus_0_25','awaiting_data'),
])
def test_individual_markets(market,selection,expected):
    m=SimpleNamespace(status='finished',ht_home=1,ht_away=0,ft_home=2,ft_away=1)
    s=SimpleNamespace(is_final=True,home_corners=5,away_corners=4,home_yellow_cards=2,away_yellow_cards=1,home_red_cards=0,away_red_cards=0)
    assert settle(m,s,dict(market=market,selection=selection))[0]==expected


@pytest.mark.parametrize('market,field',[('goals','ft_home'),('first_half_goals','ht_home'),('second_half_goals','ht_away'),
    ('corners','home_corners'),('yellow_cards','away_yellow_cards'),('red_cards','home_red_cards'),('cards','away_red_cards')])
def test_missing_never_zero(market,field):
    data=dict(status='finished',ht_home=0,ht_away=0,ft_home=0,ft_away=0,is_final=True,
              home_corners=0,away_corners=0,home_yellow_cards=0,away_yellow_cards=0,home_red_cards=0,away_red_cards=0)
    m=SimpleNamespace(**data);p=dict(market=market,selection='under_0_5')
    assert settle(m,m,p)[0]=='won'
    if market in {'corners','cards','yellow_cards','red_cards'}:
        m.is_final=False;assert settle(m,m,p)==('awaiting_data',None)
        m.is_final=True
    setattr(m,field,None);assert settle(m,m,p)==('awaiting_data',None)
    setattr(m,field,-1);assert settle(m,m,p)==('awaiting_data',None)


def test_future_finished_is_pending_and_explicit_label_only():
    m=SimpleNamespace(status='finished',kickoff_at=NOW+timedelta(hours=1),ft_home=2,ft_away=1)
    assert settle(m,None,dict(market='result',selection='home'),NOW)==('pending',None)
    for markup,expected in [(FINAL,(1,0)),(FINAL.replace("title='Score 1st Half'","title='Other Score'"),(None,None)),
                            (FINAL.replace('1-0','4-0'),(None,None))]:
        o=parse_scoreboard(dict(ErrCode=0,Data=dict(state=-1,html=markup)))
        assert (o['ht_home'],o['ht_away'])==expected


async def saved(db,market='second_half_goals'):
    await seed_match(db)
    async with db.session() as s:
        m=await s.get(Match,5959);m.status='finished';m.ft_home=2;m.ft_away=1
        pick=deepcopy(PICK)
        if market=='red_cards':pick.update(market=market,selection='under_0_5',id='under_0_5_red_cards')
        s.add(PredictionSnapshot(match_id=m.id,league_id=m.league_id,captured_at=NOW-timedelta(days=1),model_version='unchanged',picks=[pick]))
        await s.commit()
        return pick


def final_source():
    source=Source();source.payload['Data'].update(state=-1,html=FINAL)
    return source


async def verify_repair(db,monkeypatch):
    import src.jobs.settlement as worker
    import src.jobs.storage as storage
    import src.jobs.live as live
    now=NOW+timedelta(hours=4)
    monkeypatch.setattr(worker,'utcnow',lambda:now);monkeypatch.setattr(storage,'utcnow',lambda:now)
    monkeypatch.setattr(live,'utcnow',lambda:now)
    original=await saved(db)
    async with db.session() as s:
        before=await performance(s)
        assert before['summary']['awaiting_data']==1
        assert before['items'][0]['missing_fields']==['ht_home','ht_away']
    updater=PriorityUpdater(db);source=final_source()
    await recheck(updater,source)
    async with db.session() as s:
        after=await performance(s)
        item=after['items'][0]
        assert item['outcome']=='won' and item['actual']==2
        assert item['last_checked_at'] and item['result_source']=='goaloo' and item['retry_state']=='complete'
        snap=await s.get(PredictionSnapshot,5959)
        assert snap.picks==[original] and snap.model_version=='unchanged'
        assert snap.captured_at.replace(tzinfo=timezone.utc)==NOW-timedelta(days=1)
    await recheck(updater,source);await recheck(updater,source)
    assert source.calls==1
    # Completing saved goal picks must not freeze unrelated analytics coverage.
    now+=timedelta(hours=7)
    await updater.recent_statistics(source)
    assert source.calls==2


async def test_sqlite_final_repair_and_immutable_history(db,monkeypatch):
    await verify_repair(db,monkeypatch)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_final_repair_and_immutable_history(monkeypatch):
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='settlement_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as c:await c.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await verify_repair(db,monkeypatch)
    finally:
        async with db.engine.begin() as c:await c.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


@pytest.mark.parametrize('failure',[False,True])
async def test_durable_backoff_partial_data_and_exhaustion(db,monkeypatch,failure):
    import src.jobs.settlement as worker
    now=NOW+timedelta(hours=4);monkeypatch.setattr(worker,'utcnow',lambda:now)
    original=await saved(db,'red_cards');source=final_source()
    if failure:
        async def unavailable(*args):source.calls+=1;raise SourceError('unavailable')
        monkeypatch.setattr(worker,'fetch_live',unavailable)
    for attempt in range(1,MAX_ATTEMPTS+1):
        updater=PriorityUpdater(db)  # A process restart cannot reset the budget.
        await recheck(updater,source)
        assert source.calls==attempt
        await recheck(PriorityUpdater(db),source)
        assert source.calls==attempt
        async with db.session() as s:
            retry=await s.get(CollectorRetry,'settlement:3026745')
            assert retry.failures==attempt
            m=await s.get(Match,5959)
            assert (await s.get(PredictionSnapshot,5959)).picks==[original]
            assert m.raw['_settlement_check']['state']==('exhausted' if attempt==MAX_ATTEMPTS else 'waiting')
            now=retry.next_attempt_at.replace(tzinfo=timezone.utc)+timedelta(seconds=1)
    await recheck(PriorityUpdater(db),source);assert source.calls==MAX_ATTEMPTS
    # A real zero arriving through another existing collector settles automatically.
    async with db.session() as s:
        stats=await s.get(MatchStatistics,5959)
        if stats is None:stats=MatchStatistics(match_id=5959,raw={});s.add(stats)
        stats.is_final=True;stats.home_red_cards=stats.away_red_cards=0
        stats.updated_at=now
        await s.commit()
        assert settle(await s.get(Match,5959),stats,original,now)==('won',0)
        from src.analytics.settlement import details
        m=await s.get(Match,5959)
        audit=details(m,stats,original,'won',m.raw['_settlement_check'],now)
        assert audit['retry_state']=='complete' and audit['next_check_at'] is None
        assert audit['last_checked_at']==now


async def test_no_early_network_and_no_connection_during_http(db,monkeypatch):
    import src.jobs.settlement as worker
    await saved(db);now=NOW;monkeypatch.setattr(worker,'utcnow',lambda:now)
    source=final_source();await recheck(PriorityUpdater(db),source);assert source.calls==0
    now=NOW+timedelta(hours=4)
    original=worker.fetch_live
    async def checked(*args):
        assert db.engine.pool.checkedout()==0
        return await original(*args)
    monkeypatch.setattr(worker,'fetch_live',checked)
    await recheck(PriorityUpdater(db),source);assert source.calls==1


async def test_verified_real_scoreboard_half_fallback():
    from src.scrapers.goaloo.live import fetch_live
    class RealExcerpt:
        async def get_text(self,url):return Path('tests/fixtures/final_scoreboard_3073124.html').read_text(encoding='utf-8')
        async def get(self,url,params):
            assert params==dict(type=11,id=3073124)
            return dict(ErrCode=0,Data=dict(state=-1,html="<div class='end'><div class='score'>3</div><div class='score'>0</div></div>"))
    observation,_=await fetch_live(RealExcerpt(),3073124,103,19,266)
    assert (observation['ft_home'],observation['ft_away'],observation['ht_home'],observation['ht_away'])==(3,0,0,0)


async def test_retry_budget_not_bypassed_by_other_priority_cycles(db,monkeypatch):
    import src.jobs.live as live
    import src.jobs.settlement as worker
    now=NOW+timedelta(hours=4)
    monkeypatch.setattr(worker,'utcnow',lambda:now);monkeypatch.setattr(live,'utcnow',lambda:now)
    await saved(db,'red_cards');source=final_source();updater=PriorityUpdater(db)
    await recheck(updater,source)
    await updater.recent_statistics(source)
    async with db.session() as s:
        m=await s.get(Match,5959);m.status='interrupted';await s.commit()
    await updater.live_cycle(source)
    assert source.calls==1


async def test_cooldown_does_not_starve_another_saved_match(db,monkeypatch):
    import src.jobs.settlement as worker
    now=NOW+timedelta(hours=4);monkeypatch.setattr(worker,'utcnow',lambda:now)
    await saved(db,'red_cards')
    async with db.session() as s:
        m=await s.get(Match,5959)
        other=Match(id=5960,external_match_id=3026746,league_id=m.league_id,season_id=m.season_id,round=8,
                    home_team_id=m.home_team_id,away_team_id=m.away_team_id,kickoff_at=m.kickoff_at,status='finished',ft_home=2,ft_away=1,raw={})
        s.add(other);s.add(PredictionSnapshot(match_id=5960,league_id=m.league_id,captured_at=NOW-timedelta(days=1),model_version='unchanged',picks=[PICK]))
        await s.commit()
    called=[]
    async def fetch(client,external,*args):
        called.append(external)
        raise SourceError('missing')
    monkeypatch.setattr(worker,'fetch_live',fetch)
    updater=PriorityUpdater(db)
    await recheck(updater,None);await recheck(updater,None)
    assert called==[3026745,3026746]


async def test_chromium_final_data_status_and_audit_columns(api,db,monkeypatch):
    from playwright.async_api import async_playwright, expect
    import src.jobs.settlement as worker
    monkeypatch.setattr(worker,'utcnow',lambda:NOW+timedelta(hours=4))
    await saved(db,'red_cards')
    async with db.session() as s:
        snap=await s.get(PredictionSnapshot,5959)
        snap.picks=[*snap.picks,{**PICK,'id':'home_result','market':'result','selection':'home','label':'Ev Sahibi'}]
        await s.commit()
    await recheck(PriorityUpdater(db),final_source())
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport=dict(width=1440,height=900))
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#performance')
            await expect(page.locator('#performance-picks tr')).to_have_count(2)
            await expect(page.locator('#performance-picks')).to_contain_text('Veri Bekleniyor')
            await expect(page.locator('#performance-picks')).to_contain_text('Kazandı')
            await expect(page.locator('#performance-picks')).to_contain_text('kırmızı kart sayısı')
            await expect(page.locator('#performance-picks')).to_contain_text('goaloo')
            await expect(page.locator('#performance-picks tr').first.locator('td')).to_have_count(11)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors
        finally:await page.unroute_all(behavior='ignoreErrors');await browser.close()

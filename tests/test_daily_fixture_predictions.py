"""Isolated fixture catalog tests: no production or provider/messaging calls."""
from datetime import datetime,timedelta,timezone
from copy import deepcopy
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import select,event,func
from playwright.async_api import async_playwright,expect
from src.models import League,Season,Team,Match,PredictionSnapshot,PredictionSettlement,MatchStatistics,SavedCoupon
from src.analytics.settlements import settle_snapshot,correct_settlement

NOW=datetime(2026,10,24,12,tzinfo=timezone.utc)


async def catalog(db,count=25,with_picks=True):
    async with db.session() as session:
        league=await session.scalar(select(League));league.name='Test Süper Lig'
        season=Season(league_id=league.id,season_name='2026-2027');session.add(season)
        teams=[Team(external_id=99101+i,name=name) for i,name in enumerate(['Kuzey Spor','Liman FK'])]
        session.add_all(teams);await session.flush()
        ids=[]
        for i in range(count):
            status=['finished','live','half_time','scheduled','postponed','mystery'][i%6]
            kickoff=datetime(2026,10,23,21,tzinfo=timezone.utc)+timedelta(minutes=i*5)
            if status=='scheduled':kickoff=NOW+timedelta(minutes=30+i)
            match=Match(external_match_id=990000+i,league_id=league.id,season_id=season.id,
                home_team_id=teams[i%2].id,away_team_id=teams[(i+1)%2].id,round=1,
                kickoff_at=kickoff,status=status,live_minute='67' if status=='live' else None,
                ft_home=2 if status in {'finished','live','half_time'} else None,
                ft_away=1 if status in {'finished','live','half_time'} else None,
                status_observed_at=NOW-timedelta(seconds=30) if i%6!=2 else NOW-timedelta(minutes=10),
                live_checked_at=NOW-timedelta(seconds=20),updated_at=NOW,raw={'private':'DO NOT RETURN RAW'})
            session.add(match);await session.flush();ids.append(match.id)
            if with_picks and (i<4 or i==6):
                picks=[dict(id='home',label='Ev sahibi kazanır',market='result',selection='home',probability=.78,
                    confidence='high',reliability=.85,sample_size=40)]
                if i==0:
                    picks.extend([dict(picks[0],id='away',label='Deplasman kazanır',selection='away'),
                        dict(picks[0],id='corners',label='7,5 Korner Üst',market='corners',selection='over_7_5'),
                        dict(picks[0],id='push',label='Ev -1 handikap',market='asian_handicap',selection='home_minus_1_0')])
                snapshot=PredictionSnapshot(match_id=match.id,league_id=league.id,
                    captured_at=kickoff-timedelta(hours=1),model_version='synthetic-prematch',picks=picks)
                session.add(snapshot);await session.flush()
                if status=='finished' and i!=6:await settle_snapshot(session,snapshot,match,None)
        # Invalid post-kickoff snapshots are never shown as prematch.
        if with_picks and count>5:
            match=await session.get(Match,ids[5])
            session.add(PredictionSnapshot(match_id=match.id,league_id=league.id,captured_at=match.kickoff_at+timedelta(seconds=1),model_version='invalid-late',picks=[dict(market='result',selection='home',probability=.99)]))
        await session.commit()
    return ids,league.id


@pytest.mark.parametrize('count',[0,25,105,225])
async def test_complete_daily_catalog_sql_pagination_no_pick_filter(api,db,monkeypatch,count):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids,league=await catalog(db,count,with_picks=False)
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try:first=(await api.get('/api/prediction-fixtures')).json()
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    assert first['date']=='2026-10-24' and first['timezone']=='Europe/Istanbul'
    assert first['total']==first['summary']['total']==count
    assert len(first['items'])==min(count,25) and first['summary']['with_snapshot']==0
    assert len(statements)==2 and all(s.lstrip().startswith('SELECT') for s in statements)
    assert 'LIMIT' in statements[1] and 'OFFSET' in statements[1] and 'kickoff_at >=' in statements[0]
    found=[m['id'] for m in first['items']]
    for offset in range(25,count,25):
        page=(await api.get(f'/api/prediction-fixtures?offset={offset}')).json()
        found.extend(m['id'] for m in page['items'])
    assert len(found)==len(set(found))==count and set(found)==set(ids)
    assert (await api.get('/api/prediction-fixtures?limit=201')).status_code==422


async def test_original_snapshots_truthful_status_settlements_and_bounded_reads(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids,_=await catalog(db)
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try:data=(await api.get('/api/prediction-fixtures')).json()
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    assert len(statements)==3 and all(s.lstrip().startswith('SELECT') for s in statements)
    assert 'raw' not in ' '.join(statements) and 'DO NOT RETURN RAW' not in str(data)
    items={m['id']:m for m in data['items']}
    assert [p['outcome'] for p in items[ids[0]]['snapshot']['picks']]==['won','lost','awaiting_data','void']
    assert data['page_summary']==dict(verified_outcomes=3,scope='displayed_page')
    assert items[ids[6]]['snapshot']['picks'][0]['outcome']=='awaiting_data'
    assert items[ids[1]]['score']['state']=='fresh' and items[ids[1]]['snapshot']['picks'][0]['probability']==.78
    assert items[ids[1]]['snapshot']['picks'][0]['outcome']=='pending'
    assert items[ids[2]]['score']['state']=='stale' and items[ids[2]]['snapshot']['picks'][0]['outcome']=='pending'
    assert items[ids[5]]['snapshot'] is None and items[ids[5]]['status']=='mystery'
    # Current score changes without a new decision cannot turn an old loss into a win.
    async with db.session() as session:
        match=await session.get(Match,ids[0]);match.ft_home=0;match.ft_away=3;await session.commit()
    changed=(await api.get('/api/prediction-fixtures?status=finished')).json()
    picks=next(m for m in changed['items'] if m['id']==ids[0])['snapshot']['picks']
    assert [p['outcome'] for p in picks[:2]]==['disputed','disputed'] and not any(p['verified'] for p in picks)
    async with db.session() as session:
        await correct_settlement(session,ids[0],0,expected_revision=1,actor='synthetic',reason='Synthetic correction')
        await session.commit()
    revised=(await api.get('/api/prediction-fixtures?status=finished')).json()
    pick=next(m for m in revised['items'] if m['id']==ids[0])['snapshot']['picks'][0]
    assert pick['outcome']=='lost' and pick['verified'] and pick['settlement']['revision']==2 and pick['probability']==.78


async def test_dates_enabled_leagues_status_unknown_and_live_finish(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids,league=await catalog(db)
    async with db.session() as session:
        template=await session.get(Match,ids[0])
        for i,kickoff in enumerate([datetime(2026,10,23,20,59,tzinfo=timezone.utc),datetime(2026,10,24,21,tzinfo=timezone.utc)]):
            session.add(Match(external_match_id=777001+i,league_id=league,season_id=template.season_id,
                home_team_id=template.home_team_id,away_team_id=template.away_team_id,round=1,
                status='scheduled',kickoff_at=kickoff,raw={}))
        disabled=League(external_id=999,name='Disabled',country='Test',enabled=False);session.add(disabled);await session.flush()
        session.add(Match(external_match_id=777003,league_id=disabled.id,season_id=template.season_id,
            home_team_id=template.home_team_id,away_team_id=template.away_team_id,round=1,status='finished',kickoff_at=NOW,raw={}))
        await session.commit()
    assert (await api.get('/api/prediction-fixtures')).json()['total']==25
    assert (await api.get('/api/prediction-fixtures?date=2026-10-23')).json()['total']==1
    assert (await api.get('/api/prediction-fixtures?date=2026-10-25')).json()['total']==1
    # Existing /matches today view must also honor explicit non-today dates.
    assert (await api.get('/api/matches?view=today&date=2026-10-25')).json()['total']==1
    live_page=(await api.get('/api/prediction-fixtures?status=live')).json()
    assert live_page['total']==8 and all(m['status'] in {'live','half_time'} for m in live_page['items'])
    assert live_page['summary']['total']==25
    assert (await api.get('/api/prediction-fixtures?league=99999')).json()['total']==0
    for query in ('status=invalid','offset=-1','league=bad','date=bad'):
        assert (await api.get('/api/prediction-fixtures?'+query)).status_code==422
    async with db.session() as session:
        match=await session.get(Match,ids[1]);match.status='finished';match.ft_home=3;match.ft_away=1
        match.status_observed_at=NOW;await session.flush()
        snapshot=await session.get(PredictionSnapshot,match.id);await settle_snapshot(session,snapshot,match,None)
        await session.commit()
    finished=(await api.get('/api/prediction-fixtures?status=finished')).json()
    changed=next(m for m in finished['items'] if m['id']==ids[1])
    assert changed['score']['home']==3 and changed['snapshot']['picks'][0]['outcome']=='won'
    async with db.session() as session:
        assert await session.scalar(select(func.count(SavedCoupon.id)))==0


async def test_unknown_score_times_and_actual_database_restart_fail_closed(api,db,monkeypatch):
    import src.api.routes as routes
    from src.api.main import app
    from src.api.routes import session_dependency
    from src.db import Database
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids,_=await catalog(db)
    async with db.session() as session:
        live=await session.get(Match,ids[1]);live.status_observed_at=None
        final=await session.get(Match,ids[0]);final.status_observed_at=None
        unknown=await session.get(Match,ids[2]);unknown.status_observed_at=NOW+timedelta(hours=1)
        await session.commit()
    data=(await api.get('/api/prediction-fixtures')).json();items={m['id']:m for m in data['items']}
    assert items[ids[1]]['score']['home'] is None and items[ids[1]]['score']['state']=='unknown'
    assert items[ids[0]]['score']['state']=='unknown_time' and items[ids[0]]['score']['home']==2
    assert items[ids[2]]['score']['home'] is None
    monkeypatch.setattr(routes,'database',Database(''))
    previous=app.dependency_overrides.pop(session_dependency)
    try:
        assert (await api.get('/health')).status_code==200
        assert (await api.get('/api/prediction-fixtures')).status_code==503
    finally:app.dependency_overrides[session_dependency]=previous


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='Isolated PostgreSQL supplied by CI')
async def test_postgres_paginated_catalog_repeatable_read_and_no_source_writes():
    import uuid
    from sqlalchemy import text
    from sqlalchemy.schema import CreateSchema,DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.exc import DBAPIError
    from src.db import Database
    from src.analytics.fixture_predictions import fixture_page
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='fixture_test_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await catalog(db,225)
        async with db.session() as session:
            data=await fixture_page(session,NOW.astimezone(ZoneInfo('Europe/Istanbul')).date(),NOW,limit=100)
            assert data['total']==225 and len(data['items'])==100
            assert await session.scalar(text('SHOW transaction_read_only'))=='on'
            assert await session.scalar(text('SHOW transaction_isolation'))=='repeatable read'
            with pytest.raises(DBAPIError):await session.execute(text(f'DELETE FROM {schema}.matches'))
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True))
        await db.close()


@pytest.mark.parametrize('width',[390,1536])
@pytest.mark.parametrize('mode',['populated','empty','outage'])
async def test_daily_fixture_browser_navigation_evidence_and_states(api,db,monkeypatch,width,mode):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids,league=await catalog(db,105 if mode=='populated' else 0)
    unavailable=mode=='outage';calls=[];errors=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960},has_touch=width==390,reduced_motion='reduce')
        # A wrong device day must not override the server's default Istanbul day.
        await page.clock.install(time=NOW+timedelta(days=1) if mode=='empty' and width==1536 else NOW)
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append((route.request.method,path))
            assert route.request.url.startswith('http://test/')
            if path.startswith('/api/prediction-fixtures') and unavailable:
                await route.fulfill(status=503,content_type='application/json',body='{"detail":"Unavailable"}');return
            response=await api.request(route.request.method,path,content=route.request.post_data,
                headers={k:v for k,v in route.request.headers.items() if k=='content-type'})
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions')
            await expect(page.locator('#prediction-fixtures')).to_have_attribute('aria-busy','false')
            await expect(page.locator('#fixtures-date')).to_have_value('2026-10-24')
            if mode=='outage':
                await expect(page.locator('#fixtures-total')).to_have_text('—')
                await expect(page.locator('#fixture-list')).to_contain_text('Fikstür doğrulanamadı')
            elif mode=='empty':
                await expect(page.locator('#fixtures-total')).to_have_text('0')
                await expect(page.locator('#fixture-list')).to_contain_text('Fikstür yayınlanmamış')
            else:
                await expect(page.locator('.daily-fixture')).to_have_count(25)
                await expect(page.locator('#fixtures-total')).to_have_text('105')
                await expect(page.locator('#fixture-list')).to_contain_text('Tahmin oluşturulmadı')
                await expect(page.locator('#fixture-list')).to_contain_text('Güncel skor bilinmiyor')
                summary=page.locator('.fixture-evidence > summary').first
                await summary.focus();await summary.press('Enter')
                await expect(page.locator('.fixture-evidence').first).to_have_attribute('open','')
                await expect(page.locator('.fixture-evidence').first).to_contain_text('Kayıtlı karar doğrulandı')
                await expect(page.locator('.fixture-evidence').first).to_contain_text('Maç öncesi kayıt')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            folder=os.getenv('FIXTURE_SCREENSHOT_DIR')
            if folder:
                dest=Path(folder);dest.mkdir(parents=True,exist_ok=True)
                await page.locator('#prediction-fixtures').scroll_into_view_if_needed()
                await page.evaluate('window.scrollTo(0,0)')
                await page.screenshot(path=str(dest/f'fixtures-{mode}-{width}.png'))
                if mode=='populated':
                    await page.locator('.daily-fixture').first.evaluate("el=>window.scrollTo({top:el.getBoundingClientRect().top+scrollY-100,behavior:'instant'})")
                    await page.screenshot(path=str(dest/f'fixtures-evidence-{width}.png'))
            if mode=='populated':
                await page.locator('#fixtures-next').click()
                await expect(page.locator('#fixtures-pagination')).to_contain_text('26–50 / 105')
                assert len(set(await page.locator('.daily-fixture').evaluate_all('(els)=>els.map(e=>e.dataset.fixtureId)')))==25
                await page.locator('[data-fixture-status=live]').click()
                await expect(page.locator('#fixtures-feedback')).to_contain_text('36 fikstür')
                await page.locator('[data-fixture-day="1"]').click()
                await expect(page.locator('#fixtures-date')).to_have_value('2026-10-25')
                await expect(page.locator('.daily-fixture')).to_have_count(0)
                await page.locator('[data-fixture-day="0"]').click()
                await page.locator('[data-fixture-status=all]').click()
                await expect(page.locator('.daily-fixture')).to_have_count(25)
                before=len([path for _,path in calls if path.startswith('/api/prediction-fixtures')])
                await page.clock.fast_forward(30000)
                assert len([path for _,path in calls if path.startswith('/api/prediction-fixtures')])==before
                await page.clock.fast_forward(31000)
                await expect(page.locator('#prediction-fixtures')).to_have_attribute('aria-busy','false')
                assert len([path for _,path in calls if path.startswith('/api/prediction-fixtures')])==before+1
                await page.clock.fast_forward(125000)
                await expect(page.locator(f'[data-fixture-id="{ids[1]}"] .fixture-provenance')).to_contain_text('Güncel skor bilinmiyor')
                await page.locator('[data-fixture-status=live]').click()
                await expect(page.locator('#fixtures-feedback')).to_contain_text('36 fikstür')
                await page.locator('#fixtures-next').click()
                await expect(page.locator('#fixtures-pagination')).to_contain_text('26–36 / 36')
                async with db.session() as session:
                    from sqlalchemy import update
                    await session.execute(update(Match).where(Match.status.in_(['live','half_time'])).values(status='finished'))
                    await session.commit()
                await page.locator('#fixtures-refresh').click()
                await expect(page.locator('#fixtures-pagination')).to_have_text('0 kayıt')
                await expect(page.locator('#fixtures-previous')).to_be_disabled()
                unavailable=True;await page.locator('#fixtures-refresh').click()
                await expect(page.locator('#fixtures-total')).to_have_text('—')
                await page.evaluate("location.hash='#home'")
                await expect(page.locator('#predictions-section')).to_be_hidden()
                count=len([path for _,path in calls if path.startswith('/api/prediction-fixtures')])
                await page.clock.fast_forward(120000)
                assert len([path for _,path in calls if path.startswith('/api/prediction-fixtures')])==count
            if mode=='outage':
                unavailable=False;await page.locator('#fixtures-refresh').click()
                await expect(page.locator('#fixtures-total')).to_have_text('0')
            assert not errors
            assert not any('telegram' in path or 'nvidia' in path for _,path in calls)
            assert all(method=='GET' or path=='/api/coupon/quote' for method,path in calls)
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()

from copy import deepcopy
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from src.analytics.performance import settle, performance
from src.analytics.settlements import settle_match, settle_batch, decisions, correct_settlement, resolved_predictions
from src.models import Match, MatchStatistics, PredictionSnapshot, PredictionSettlement
from tests.test_predictions import seed


@pytest.mark.parametrize('market', ['goals','first_half_goals','second_half_goals','corners','cards','yellow_cards','red_cards'])
def test_each_market_missing_final_and_zero(market):
    match=SimpleNamespace(status='finished',ft_home=0,ft_away=0,ht_home=0,ht_away=0)
    stats=SimpleNamespace(is_final=True,home_corners=0,away_corners=0,
        home_yellow_cards=0,away_yellow_cards=0,home_red_cards=0,away_red_cards=0)
    pick=dict(market=market,selection='under_0_5')
    assert settle(match,stats,pick)==('won',0)
    if market=='goals':match.ft_home=None
    elif market in {'first_half_goals','second_half_goals'}:match.ht_home=None
    elif market=='corners':stats.home_corners=None
    elif market in {'cards','red_cards'}:stats.home_red_cards=None
    else:stats.home_yellow_cards=None
    assert settle(match,stats,pick)==('awaiting_data',None)


@pytest.mark.parametrize('pick', [dict(market='goals',selection='invalid'),dict(market='goals',selection='over_2_25'),dict(market='result',selection='bogus'),dict(market='unknown',selection='yes')])
def test_invalid_import_never_settles(pick):
    assert settle(SimpleNamespace(status='finished'),None,pick)==('awaiting_data',None)


async def test_durable_idempotent_outcomes_correction_and_safe_lessons(db):
    target,*_=await seed(db)
    async with db.session() as session:
        snapshot=await session.get(PredictionSnapshot,target)
        from src.analytics.board_cache import MODEL_VERSION
        assert snapshot.model_version==MODEL_VERSION
        original=deepcopy(snapshot.picks)
        match=await session.get(Match,target)
        match.status='finished';match.ft_home=8;match.ft_away=7
        assert await settle_match(session,match)>0
        await session.commit()
    async with db.session() as session:
        match=await session.get(Match,target)
        frozen=await decisions(session,target)
        count=await session.scalar(select(func.count(PredictionSettlement.id)))
        assert await settle_match(session,match)==0
        match.ft_home=0;match.ft_away=0
        assert await settle_match(session,match)==0
        await session.commit()
        assert await session.scalar(select(func.count(PredictionSettlement.id)))==count
        assert {i:r.outcome for i,r in (await decisions(session,target)).items()}=={i:r.outcome for i,r in frozen.items()}
        report=await performance(session)
        assert all(i['settlement_basis']=='recorded' for i in report['items'])
        assert (await session.get(PredictionSnapshot,target)).picks==original
        # Pick a decision whose outcome actually changed, to exercise correction.
        snapshot=await session.get(PredictionSnapshot,target)
        index=next(i for i,r in frozen.items() if settle(match,None,snapshot.picks[i])[0]!=r.outcome)
        with pytest.raises(ValueError):
            await correct_settlement(session,target,index,expected_revision=1,actor='',reason='source correction')
        corrected=await correct_settlement(session,target,index,expected_revision=1,actor='reviewer',reason='verified source score correction')
        assert corrected.revision==2
        await session.commit()
        assert await settle_match(session,match)==0
        with pytest.raises(ValueError):
            await correct_settlement(session,target,index,expected_revision=1,actor='reviewer',reason='stale correction')
        lessons=await resolved_predictions(session,limit=100)
        item=next(i for i in lessons if i['pick_index']==index)
        assert item['revision']==2 and item['prediction']==original[index]
        item['prediction']['probability']=0
        assert (await session.get(PredictionSnapshot,target)).picks==original
        assert await resolved_predictions(session,after_id=max(i['cursor'] for i in lessons))==[]
        with pytest.raises(ValueError):await resolved_predictions(session,limit=1001)


async def test_database_guards_and_backfill(db):
    target,*_=await seed(db)
    async with db.session() as session:
        for sql in ["UPDATE prediction_snapshots SET model_version='changed'",'DELETE FROM prediction_snapshots']:
            with pytest.raises(DBAPIError):
                async with session.begin_nested():await session.execute(text(sql))
        match=await session.get(Match,target);match.status='finished';match.ft_home=2;match.ft_away=1
        await session.commit()
        assert await settle_batch(session)==target
        await session.commit()
        before=await session.scalar(select(func.count(PredictionSettlement.id)))
        assert before>0
        await settle_batch(session);await session.commit()
        assert await session.scalar(select(func.count(PredictionSettlement.id)))==before
        for sql in ["UPDATE prediction_settlements SET outcome='lost'",'DELETE FROM prediction_settlements']:
            with pytest.raises(DBAPIError):
                async with session.begin_nested():await session.execute(text(sql))


async def test_storage_records_result_in_same_transaction(db):
    from src.jobs.storage import store_live
    from src.models import utcnow
    target,*_=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,target)
        await store_live(session,match,dict(status='finished',live_minute=None,ft_home=2,ft_away=1,raw={}),utcnow())
        assert await decisions(session,target)
        await session.rollback()
    async with db.session() as session:
        assert not await decisions(session,target)


async def test_partial_statistics_wait_then_finalize_independently(db):
    from datetime import timedelta
    from src.models import Season, Team
    from tests.test_predictions import NOW
    target,*_=await seed(db)
    async with db.session() as session:
        base=await session.get(Match,target)
        match=Match(external_match_id=999992,league_id=base.league_id,season_id=base.season_id,
            home_team_id=base.home_team_id,away_team_id=base.away_team_id,round=1,
            status='finished',kickoff_at=NOW+timedelta(hours=1),ft_home=2,ft_away=0)
        session.add(match);await session.flush()
        picks=[dict(market=m,selection='over_0_5') for m in ('goals','corners','cards','yellow_cards','red_cards','first_half_goals','second_half_goals')]
        session.add(PredictionSnapshot(match_id=match.id,league_id=match.league_id,captured_at=NOW,model_version='test',picks=picks))
        stats=MatchStatistics(match_id=match.id,is_final=True,home_corners=2,away_corners=1,
            home_yellow_cards=1,away_yellow_cards=1,home_red_cards=None,away_red_cards=None)
        session.add(stats);await session.flush()
        assert await settle_match(session,match)==3
        assert set(await decisions(session,match.id))=={0,1,3}
        match.ht_home=1;match.ht_away=0;stats.home_red_cards=0;stats.away_red_cards=0
        assert await settle_match(session,match)==4
        assert await settle_match(session,match)==0
        await session.commit()


@pytest.mark.skipif(not __import__('os').getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_prediction_memory_concurrency_and_guards():
    import os, uuid, asyncio
    from sqlalchemy.schema import CreateSchema, DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    database=Database(os.environ['TEST_POSTGRES_URL'])
    schema='memory_'+uuid.uuid4().hex
    try:
        async with database.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        database.engine=database.engine.execution_options(schema_translate_map={None:schema})
        database.sessions=async_sessionmaker(database.engine,expire_on_commit=False)
        await database.initialize()
        async with database.engine.begin() as conn:
            await conn.execute(text(f'ALTER TABLE {schema}.prediction_snapshots ALTER COLUMN model_version TYPE VARCHAR(40)'))
        await database.initialize()
        target,*_=await seed(database)
        async with database.session() as session:
            match=await session.get(Match,target);match.status='finished';match.ft_home=2;match.ft_away=1
            await session.commit()
        async def run():
            async with database.session() as session:
                count=await settle_match(session,await session.get(Match,target))
                await session.commit()
                return count
        counts=await asyncio.gather(*(run() for _ in range(5)))
        async with database.session() as session:
            total=await session.scalar(select(func.count(PredictionSettlement.id)))
            assert total>0 and sum(counts)==total
            assert len(await resolved_predictions(session))==total
            for sql in ["UPDATE prediction_snapshots SET model_version='changed'",'DELETE FROM prediction_snapshots',"UPDATE prediction_settlements SET outcome='lost'",'DELETE FROM prediction_settlements']:
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(text(sql.replace('prediction_snapshots',schema+'.prediction_snapshots').replace('prediction_settlements',schema+'.prediction_settlements')))
    finally:
        async with database.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await database.close()


async def test_chromium_history_distinguishes_all_five_states(api,db):
    from playwright.async_api import async_playwright, expect
    await seed(db)
    payload=(await api.get('/api/market-performance')).json()
    original=payload['items'][0]
    payload['items']=[dict(original,outcome=state) for state in ('pending','awaiting_data','won','lost','void')]
    payload['total']=5
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        page=await browser.new_page()
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/market-performance'):
                import json
                await route.fulfill(status=200,body=json.dumps(payload),content_type='application/json')
            else:
                response=await api.get(path)
                await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        await page.goto('http://test/')
        await page.locator('.desktop-nav [data-page-link=performance]').click()
        await expect(page.locator('#performance-picks tr')).to_have_count(5)
        for state in ('pending','awaiting_data','won','lost','void'):
            await expect(page.locator('#performance-picks td.'+state)).not_to_be_empty()
        assert await page.locator('#performance-picks td.pending').inner_text()!=await page.locator('#performance-picks td.awaiting_data').inner_text()
        assert errors==[]
        await browser.close()


async def test_lessons_rejects_unverifiable_decision(db):
    target,*_=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,target);match.status='finished';match.ft_home=2;match.ft_away=1
        await settle_match(session,match);await session.commit()
        row=next(iter((await decisions(session,target)).values()))
        # Simulate an inconsistent imported object without modifying durable history.
        from sqlalchemy.orm.attributes import set_committed_value
        set_committed_value(row,'outcome','lost' if row.outcome=='won' else 'won')
        with pytest.raises(ValueError,match='does not match'):
            await resolved_predictions(session)


async def test_first_publication_keeps_model_probability_and_source_odds(db,monkeypatch):
    from datetime import timedelta
    from src.analytics.performance import capture
    import src.analytics.board_cache as board
    from tests.test_predictions import NOW
    target,*_=await seed(db)
    async with db.session() as session:
        base=await session.get(Match,target)
        match=Match(external_match_id=999993,league_id=base.league_id,season_id=base.season_id,
            home_team_id=base.home_team_id,away_team_id=base.away_team_id,round=1,
            status='scheduled',kickoff_at=NOW+timedelta(hours=1))
        session.add(match);await session.flush()
        pick=dict(market='goals',selection='over_2_5',probability=.73,
            verified_odds=dict(best_price=1.95,observed_at=NOW.isoformat(),best_bookmaker='Bet365'))
        original=deepcopy(pick)
        monkeypatch.setattr(board,'MODEL_VERSION','original-model-with-flags')
        assert await capture(session,match,[pick],NOW)
        await session.commit()
        pick['probability']=.9;pick['verified_odds']['best_price']=2.2
        monkeypatch.setattr(board,'MODEL_VERSION','replacement-model')
        assert not await capture(session,match,[pick],NOW+timedelta(minutes=1))
        stored=await session.get(PredictionSnapshot,match.id)
        assert stored.model_version=='original-model-with-flags'
        assert stored.picks==[original]

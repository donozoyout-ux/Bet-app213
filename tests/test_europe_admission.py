from datetime import datetime,timezone,timedelta
import json
import pytest
from sqlalchemy import select
from src.models import League,Season,Team,Match,Bookmaker,Odds1X2,AsianHandicap,AsianTotals,utcnow
from src.jobs.europe_admission import admission_status,activate_reviewed,source_review_passes
from src.scrapers.goaloo.competitions import apply_production_scope


async def seed_history(db):
    # Synthetic regression fixtures only; never loaded into production.
    async with db.session() as s:
        league=League(external_id=6,name='Poland',country='Poland',enabled=False)
        s.add(league);await s.flush()
        season=Season(league_id=league.id,season_name='2026-2027')
        home=Team(external_id=100001,name='Test home');away=Team(external_id=100002,name='Test away')
        s.add_all([season,home,away]);await s.flush()
        books=(await s.scalars(select(Bookmaker))).all()
        for i in range(50):
            match=Match(external_match_id=9000000+i,league_id=league.id,season_id=season.id,
                home_team_id=home.id,away_team_id=away.id,round=i+1,status='finished',
                kickoff_at=datetime(2024,1,1,tzinfo=timezone.utc)+timedelta(days=i) if i<25 else utcnow()-timedelta(days=i),
                ht_home=0,ht_away=0,ft_home=1,ft_away=0,raw={},odds_complete=False)
            s.add(match);await s.flush()
            for b in books:
                for model,fields in ((Odds1X2,('home','draw','away')),(AsianHandicap,('line','home','away')),(AsianTotals,('line','over','under'))):
                    values={f'{stage}_{field}':0 if field=='line' else 1.9 for stage in ('opening','closing') for field in fields}
                    s.add(model(match_id=match.id,bookmaker_id=b.id,raw={},**values))
        await s.commit()
        return league.id


async def test_admission_uses_real_fields_and_recent_history_then_preserves_scope(db):
    id=await seed_history(db)
    async with db.session() as s:
        league=await s.get(League,id)
        status=await admission_status(s,league)
        assert (status['finished'],status['complete_three_books'],status['recent_complete'])==(50,50,25)
        assert status['ready']  # Cached odds_complete is deliberately false.
        await activate_reviewed(s,[6]);await s.commit()
        assert league.enabled
        await apply_production_scope(s)
        assert league.enabled
        assert await s.scalar(select(League.enabled).where(League.external_id==36))
        # Freshness matters even when fifty old observations are complete.
        for m in (await s.scalars(select(Match).where(Match.league_id==id))).all():
            m.kickoff_at=datetime(2024,1,1,tzinfo=timezone.utc)
        await s.flush()
        assert not (await admission_status(s,league))['ready']


async def test_missing_bookmaker_fields_cannot_be_hidden_by_complete_flag(db):
    id=await seed_history(db)
    async with db.session() as s:
        matches=(await s.scalars(select(Match).where(Match.league_id==id).order_by(Match.id))).all()
        for m in matches:
            m.odds_complete=True
        book=await s.scalar(select(Bookmaker.id).where(Bookmaker.name=='Crown'))
        for m in matches[:3]:
            row=await s.scalar(select(Odds1X2).where(Odds1X2.match_id==m.id,Odds1X2.bookmaker_id==book))
            row.closing_home=None
        await s.flush()
        league=await s.get(League,id)
        assert not (await admission_status(s,league))['ready']
        with pytest.raises(ValueError,match='insufficient'):
            await activate_reviewed(s,[6])
        assert not league.enabled


async def test_twenty_five_ancient_matches_and_unreviewed_ids_are_not_promoted(db):
    async with db.session() as s:
        league=await s.scalar(select(League).where(League.external_id==36))
        assert not (await admission_status(s,league))['ready']
        with pytest.raises(ValueError):
            await activate_reviewed(s,[36])
        with pytest.raises(ValueError):
            await activate_reviewed(s,[6,6])


def test_corrupted_source_evidence_fails_closed(monkeypatch,tmp_path):
    import src.jobs.europe_admission as module
    assert source_review_passes(6) and source_review_passes(7)
    assert not source_review_passes(133)
    path=tmp_path/'broken.json';path.write_text('{}')
    monkeypatch.setattr(module,'EVIDENCE',path)
    assert not source_review_passes(6)


@pytest.mark.asyncio
async def test_postgres_admission_and_activation_use_persisted_fields():
    import os,uuid
    from sqlalchemy.schema import CreateSchema,DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    if not os.getenv('TEST_POSTGRES_URL'):
        pytest.skip('TEST_POSTGRES_URL is not configured')
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='admission_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        # Catalog seeding creates Poland only in production; this test owns an
        # isolated schema and adds regression fixtures without external calls.
        id=await seed_history(db)
        async with db.session() as s:
            league=await s.get(League,id)
            status=await admission_status(s,league)
            assert status['ready'] and status['complete_three_books']==50
            await activate_reviewed(s,[6]);await s.commit()
            assert league.enabled
            await apply_production_scope(s)
            assert league.enabled
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()

"""A reload or match selection must never execute a prediction engine."""
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from src.models import PublishedMatchAnalysis,Match
from tests.test_predictions import seed,NOW


async def test_detail_is_persisted_read_only_and_fails_closed(api,db,monkeypatch):
    import src.api.routes as routes
    import src.analytics.service as service
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,*_=await seed(db)
    async def forbidden(*a,**k):raise AssertionError('HTTP invoked the model')
    monkeypatch.setattr(routes,'compute_statistics',forbidden)
    service._cache.clear()
    first=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert first['publication']['status']=='current' and first['candidates']
    for _ in range(3):
        r=await api.get(f'/api/matches/{target}/statistics')
        assert r.status_code==200 and r.json()['publication']==first['publication']
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(PublishedMatchAnalysis))==1
        row=await s.get(PublishedMatchAnalysis,target);row.expires_at=NOW-timedelta(seconds=1);await s.commit()
    r=await api.get(f'/api/matches/{target}/statistics')
    assert r.status_code==503 and r.json()['detail']['code']=='analysis_publication_pending'


async def test_history_reads_actual_prematch_archive_without_hindsight_model(api,db,monkeypatch):
    import src.api.routes as routes
    target,*_=await seed(db)
    monkeypatch.setattr(routes,'utcnow',lambda:NOW+timedelta(days=2))
    async def forbidden(*a,**k):raise AssertionError('Retrospective forecast in GET')
    monkeypatch.setattr(routes,'compute_statistics',forbidden)
    async with db.session() as s:
        match=await s.get(Match,target);match.status='finished';match.ft_home=20;match.ft_away=0;await s.commit()
    data=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert data['historical'] and data['publication']['status']=='archived'
    assert data['cache_seconds']==0 and data['match']['ft_home']==20
    assert data['prediction']['expected_home_goals']<20
    assert all(not (r.get('verified_odds') or {}).get('coupon_eligible') for r in data['recommendations'])
    async with db.session() as s:
        row=await s.get(PublishedMatchAnalysis,target);await s.delete(row);await s.commit()
    r=await api.get(f'/api/matches/{target}/statistics')
    assert r.status_code==404 and r.json()['detail']['code']=='analysis_archive_missing'

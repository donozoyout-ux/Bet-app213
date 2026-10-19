"""Synthetic screen fixtures only; no production predictions are created."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace as NS
import os
from pathlib import Path
import pytest
from playwright.async_api import async_playwright, expect
from src.analytics.performance import display_result, tally, add, finish
from src.analytics.settlements import evidence, digest, snapshot_digest, VERSION
from tests.test_predictions import NOW, seed
from src.models import Match, PredictionSnapshot, MatchStatistics, PredictionSettlement
from sqlalchemy import select
from src.analytics.settlements import settle_snapshot


def test_reconciliation_preserves_frozen_evidence():
    pick=dict(market='corners',selection='over_7_5',reliability=.8,sample_size=30)
    snapshot=NS(match_id=1,league_id=1,captured_at=NOW,model_version='test',picks=[pick])
    match=NS(status='finished',ft_home=2,ft_away=1,ht_home=1,ht_away=0,status_observed_at=None)
    stats=NS(is_final=True,home_corners=3,away_corners=3,updated_at=None)
    proof=evidence(match,stats)
    saved=NS(outcome='lost',actual={'value':6},evidence=proof,snapshot_hash=snapshot_digest(snapshot),evidence_hash=digest(proof),evaluator_version=VERSION,pick_index=0)
    original=deepcopy(saved.__dict__)
    assert display_result(snapshot,match,stats,pick,saved)[:2]==('lost',6)
    stats.home_corners=4
    assert display_result(snapshot,match,stats,pick,saved)[:2]==("disputed",7)
    stats.home_corners=5
    assert display_result(snapshot,match,stats,pick,saved)[:2]==('disputed',8)
    stats.home_corners=None
    assert display_result(snapshot,match,stats,pick,saved)[0]=='awaiting_data'
    assert saved.__dict__==original
    saved.evidence_hash='invalid'
    assert display_result(snapshot,match,stats,pick,saved)[0]=='disputed'
    counts=tally()
    for outcome in ('won','lost','pending','awaiting_data','void','disputed'):add(counts,pick,outcome)
    assert finish(counts)['success_rate']==.5 and counts['settled']==2


async def fixture(db):
    ident,*_=await seed(db)
    async with db.session() as session:
        source=await session.get(Match,ident)
        match=Match(external_match_id=9999999,round=1,status="scheduled",league_id=source.league_id,season_id=source.season_id,home_team_id=source.home_team_id,away_team_id=source.away_team_id,kickoff_at=NOW+timedelta(hours=1),raw={})
        session.add(match);await session.flush();ident=match.id
        match.status='finished';match.ft_home=2;match.ft_away=1
        snapshot=PredictionSnapshot(match_id=ident,league_id=match.league_id,captured_at=NOW,model_version="test")
        session.add(snapshot)
        snapshot.picks=[dict(id=str(i),market=market,selection=selection,label=market,confidence='high',probability=.8,reliability=.8,sample_size=30) for i,(market,selection) in enumerate([
            ('result','home'),('result','away'),('corners','over_7_5'),('asian_handicap','home_minus_1_0'),('yellow_cards','over_2_5')])]
        stats=MatchStatistics(match_id=ident,is_final=True,home_yellow_cards=1,away_yellow_cards=1,source='test')
        session.add(stats);await session.flush()
        await settle_snapshot(session,snapshot,match,stats)
        stats.home_yellow_cards=3
        await session.commit()
    return ident


@pytest.mark.parametrize('width',[390,1536])
async def test_result_screen_real_api(api,db,width):
    ident=await fixture(db)
    data=(await api.get('/api/market-performance')).json()
    assert [x['outcome'] for x in data['items'] if x['match']['id']==ident]==['won','lost','awaiting_data','void','disputed']
    assert data['items'][0]['match']['id']==ident
    assert data['summary']['success_rate']==.5
    async with db.session() as session:
        before=[(r.id,r.outcome,deepcopy(r.evidence)) for r in await session.scalars(select(PredictionSettlement))]
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#performance')
            for status,label in [('won','✓ Tuttu'),('lost','✕ Kaybetti'),('awaiting_data','⚠ Veri eksik'),('void','— Geçersiz'),('disputed','⚠ İncelenmeli')]:
                await expect(page.locator('#performance-picks .'+status)).to_have_text(label)
            assert await page.locator('#performance-picks .pending').count()>0
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('UI_SCREENSHOT_DIR'):
                target=Path(os.environ['UI_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(target/f'performance-{width}.png'),full_page=True)
            assert not errors
            async with db.session() as session:
                after=[(r.id,r.outcome,r.evidence) for r in await session.scalars(select(PredictionSettlement))]
            assert after==before
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()

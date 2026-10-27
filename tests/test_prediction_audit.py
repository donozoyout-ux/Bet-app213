"""Isolated regression fixtures only; no production data or outbound services."""
from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select, func, update, delete
from sqlalchemy.exc import DBAPIError
from src.models import PredictionPublication as Publication, PredictionBoard, PredictionSnapshot, Match
from src.analytics.publications import publish_board
from src.analytics.prediction_audit import compare, problems
from tests.test_predictions import seed, NOW


async def test_journal_renewal_change_return_removal_and_snapshot_unchanged(db):
    mid, league, *_ = await seed(db)
    async with db.session() as s:
        original = deepcopy((await s.get(PredictionSnapshot, mid)).picks)
        board = await s.get(PredictionBoard, league)
        initial = deepcopy(board.payload)
        await publish_board(s, board, NOW+timedelta(seconds=1), candidate_payload=initial)
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(Publication)) == 1
        changed = deepcopy(initial)
        changed['items'][0]['recommendations'][0]['probability'] -= .01
        for i, payload in enumerate((changed, initial, dict(initial, items=[])), 2):
            await publish_board(s, board, NOW+timedelta(seconds=i), candidate_payload=payload)
            await s.commit()
        rows = list(await s.scalars(select(Publication).order_by(Publication.id)))
        assert len(rows) == 4 and rows[-1].picks == []
        assert rows[0].picks == rows[2].picks
        assert rows[2].published_at > rows[0].published_at
        assert (await s.get(PredictionSnapshot, mid)).picks == original


async def test_model_change_and_transaction_rollback(db):
    mid, league, *_ = await seed(db)
    async with db.session() as s:
        board = await s.get(PredictionBoard, league)
        await publish_board(s, board, NOW+timedelta(seconds=1), model_version='test-model-revision')
        await s.commit()
        rows = list(await s.scalars(select(Publication).order_by(Publication.id)))
        assert len(rows) == 2 and rows[0].picks == rows[1].picks
        assert rows[-1].model_version == 'test-model-revision'
        await publish_board(s, board, NOW+timedelta(seconds=2), model_version='rolled-back-test-revision')
        await s.rollback()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Publication)) == 2
        assert (await s.get(PredictionBoard, league)).model_version == 'test-model-revision'


@pytest.mark.parametrize('model', [PredictionSnapshot, Publication])
@pytest.mark.parametrize('operation', [update, delete])
async def test_history_database_rejects_update_and_delete(db, model, operation):
    await seed(db)
    async with db.session() as s:
        statement = operation(model)
        if operation is update: statement = statement.values(picks=[])
        with pytest.raises(DBAPIError, match='immutable prediction history'):
            await s.execute(statement)
        await s.rollback()


async def test_audit_readonly_diff_pagination_expiry_and_validation(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW+timedelta(seconds=5))
    mid, league, *_ = await seed(db)
    async with db.session() as s:
        board = await s.get(PredictionBoard, league)
        data = deepcopy(board.payload)
        data['items'][0]['recommendations'][0]['probability'] -= .01
        await publish_board(s, board, NOW+timedelta(seconds=2), candidate_payload=data)
        await s.commit()
    for _ in range(2):
        result = await api.get(f'/api/matches/{mid}/prediction-audit?limit=1')
        assert result.status_code == 200
        data = result.json()
        assert data['comparison']['status'] == 'different'
        assert data['comparison']['changed']
        assert data['current_model_version'] == data['original']['model_version']
        assert len(data['history']) == 1 and data['next_before']
        older = (await api.get(f'/api/matches/{mid}/prediction-audit?before={data["next_before"]}')).json()
        assert len(older['history']) == 1 and older['next_before'] is None
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Publication)) == 2
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW+timedelta(hours=1))
    data = (await api.get(f'/api/matches/{mid}/prediction-audit')).json()
    assert data['comparison']['status'] == 'unavailable'
    assert 'current_publication_unavailable' in data['issues']
    assert data['original']['picks'] and len(data['history']) == 2
    assert (await api.get(f'/api/matches/{mid}/prediction-audit?limit=101')).status_code == 422
    assert (await api.get('/api/matches/99999999/prediction-audit')).status_code == 404


async def test_missing_and_invalid_records_are_reported_without_repair(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    mid, league, _, _, history = await seed(db)
    missing = (await api.get(f'/api/matches/{history[0]}/prediction-audit')).json()
    assert {'missing_snapshot', 'publication_history_unavailable'} <= set(missing['issues'])
    async with db.session() as s:
        s.add(PredictionSnapshot(match_id=history[0], league_id=league, captured_at=NOW+timedelta(days=1),
            model_version='', picks=[None]))
        await s.commit()
    data = (await api.get(f'/api/matches/{history[0]}/prediction-audit')).json()
    assert {'not_prematch', 'future_snapshot', 'missing_model_version', 'invalid_selection'} <= set(data['issues'])
    assert data['original']['picks'] == [None] and data['comparison']['status'] == 'unavailable'


def test_comparison_reports_identity_and_probability_changes():
    p = dict(id='fixture', market='goals', selection='over_1_5', probability=.7)
    assert compare([p], [dict(p, probability=.8)])['changed']
    assert compare([p], [dict(p, selection='under_1_5')])['removed'] == [p]
    assert problems([p,p]) == ['duplicate_selection']
    assert problems([dict(p, probability=float('nan'))]) == ['invalid_probability']
    assert compare(None, [])['status'] == 'invalid'


@pytest.mark.parametrize('width', [390, 1280])
async def test_audit_screen_reads_only_and_displays_history(api, db, monkeypatch, width):
    from playwright.async_api import async_playwright, expect
    from tests.test_minimal_analysis import proxy
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    mid, *_ = await seed(db)
    calls = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=dict(width=width, height=900))
        async def forward(route):
            calls.append((route.request.method, route.request.url))
            await proxy(route, api)
        await page.route('http://test/**', forward)
        try:
            await page.goto('http://test/#audit')
            await page.locator('#audit-match-id').fill(str(mid))
            await page.locator('#audit-form button').click()
            await expect(page.locator('#audit-result')).to_contain_text('Yayın #')
            await expect(page.locator('#audit-result')).to_contain_text('quality-poisson')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert all(method == 'GET' for method, _ in calls)
        finally:
            await browser.close()


async def test_postgres_journal_and_guards():
    import os, uuid
    from sqlalchemy.schema import CreateSchema, DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    if not os.getenv('TEST_POSTGRES_URL'): pytest.skip('TEST_POSTGRES_URL not configured')
    db = Database(os.environ['TEST_POSTGRES_URL'])
    schema = 'audit_' + uuid.uuid4().hex
    try:
        async with db.engine.begin() as c: await c.execute(CreateSchema(schema))
        db.engine = db.engine.execution_options(schema_translate_map={None: schema})
        db.sessions = async_sessionmaker(db.engine, expire_on_commit=False)
        await db.initialize()
        await test_journal_renewal_change_return_removal_and_snapshot_unchanged(db)
        for model in (PredictionSnapshot, Publication):
            for operation in (update, delete):
                async with db.session() as s:
                    statement = operation(model)
                    if operation is update: statement = statement.values(picks=[])
                    with pytest.raises(DBAPIError, match='immutable prediction history'):
                        await s.execute(statement)
                    await s.rollback()
    finally:
        async with db.engine.begin() as c: await c.execute(DropSchema(schema, cascade=True, if_exists=True))
        await db.close()

"""The writer maintains a small version-bound HTTP read model."""
from copy import deepcopy
from datetime import timedelta
from sqlalchemy import select,event
from src.models import BoardReadPublication,PredictionBoard,BoardHead
from src.analytics.publications import publish_board
from tests.test_predictions import seed,NOW


async def test_split_publication_tracks_content_and_unchanged_does_not_rewrite(db):
    _,league,*_=await seed(db)
    async with db.session() as s:
        row=await s.get(BoardReadPublication,league)
        head=await s.get(BoardHead,league)
        assert row.version==head.version
        # The list reader need not deserialize coupon/engine evidence.
        assert 'coupon_records' not in row.__dict__ and 'items' not in row.__dict__
        b=await s.get(PredictionBoard,league)
        statements=[]
        def collect(conn,cursor,statement,*a):statements.append(statement)
        event.listen(db.engine.sync_engine,'before_cursor_execute',collect)
        await publish_board(s,b,NOW+timedelta(minutes=1));await s.commit()
        event.remove(db.engine.sync_engine,'before_cursor_execute',collect)
        assert not any(q.startswith('UPDATE board_read_publications') for q in statements)
        old=row.version;changed=deepcopy(b.payload)
        changed['items'][0]['recommendations'][0]['probability']-=.001
        await publish_board(s,b,NOW+timedelta(minutes=2),candidate_payload=changed);await s.commit()
        assert row.version!=old and row.version==(await s.get(BoardHead,league)).version
        actual=await s.scalar(select(BoardReadPublication.items).where(BoardReadPublication.league_id==league))
        assert actual==changed['items']


async def test_existing_edition_is_warmed_without_new_version(db):
    _,league,*_=await seed(db)
    async with db.session() as s:
        projection=await s.get(BoardReadPublication,league)
        version=projection.version
        await s.delete(projection);await s.commit()
    async with db.session() as s:
        b=await s.get(PredictionBoard,league)
        generated=b.generated_at
        assert await publish_board(s,b,NOW+timedelta(minutes=1))==version
        await s.commit()
        assert (await s.get(BoardReadPublication,league)).version==version
        assert b.generated_at==generated


async def test_worker_warms_missing_projection_before_lease_expiry(db):
    from src.analytics.board_cache import refresh_one
    _,league,*_=await seed(db)
    async with db.session() as s:
        row=await s.get(BoardReadPublication,league)
        version=row.version
        await s.delete(row);await s.commit()
    assert await refresh_one(db,now=NOW+timedelta(minutes=1))
    async with db.session() as s:
        assert (await s.get(BoardReadPublication,league)).version==version

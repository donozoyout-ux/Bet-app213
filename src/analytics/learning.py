"""Transactional, revision-aware report materialization outside request handlers."""
import asyncio
import hashlib
import logging
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text, func
from src.models import (PredictionSnapshot, Match, LearningReport, LearningSlice,
                        LearningLesson, LearningHead, utcnow)
from src.analytics.settlements import resolved_predictions, digest
from src.analytics.learning_metrics import analyze, METHOD

MAX_RECORDS = 50_000
log = logging.getLogger(__name__)


async def source_data(session):
    """Read one consistent source edition, revisiting old IDs for new revisions."""
    from src.jobs.worker import aware
    if session.bind.dialect.name == 'postgresql':
        await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    observed_at=aware(await session.scalar(select(func.current_timestamp()))) if session.bind.dialect.name=='postgresql' else utcnow()
    if await session.scalar(select(func.count(PredictionSnapshot.match_id))) > MAX_RECORDS:
        raise ValueError('Learning source exceeds bounded capacity; no partial report published')
    items, cursor = [], 0
    while True:
        page = await resolved_predictions(session, after_id=cursor, limit=500)
        if not page:
            break
        items.extend(page)
        if len(items) > MAX_RECORDS:
            raise ValueError('Learning source exceeds bounded capacity; no partial report published')
        cursor = page[-1]['cursor']
    keys = {(r['match_id'],r['pick_index']) for r in items}
    coverage = dict(snapshot_matches=0, saved_predictions=0, recorded_terminal=len(items),
        match_pending=0, result_data_pending=0, invalid_capture=0)
    manifest = hashlib.sha256()
    query = select(PredictionSnapshot,Match.status,Match.kickoff_at).join(
        Match,Match.id==PredictionSnapshot.match_id).order_by(PredictionSnapshot.match_id).execution_options(yield_per=500)
    async for snapshot,status,kickoff in await session.stream(query):
        coverage['snapshot_matches'] += 1
        coverage['saved_predictions'] += len(snapshot.picks)
        if coverage['saved_predictions'] > MAX_RECORDS:
            raise ValueError('Learning source exceeds bounded capacity; no partial report published')
        valid = kickoff is not None and aware(snapshot.captured_at) < aware(kickoff)
        if not valid:
            coverage['invalid_capture'] += len(snapshot.picks)
        for index in range(len(snapshot.picks)):
            if valid and (snapshot.match_id,index) not in keys:
                field = 'result_data_pending' if status in {'finished','cancelled','abandoned'} else 'match_pending'
                coverage[field] += 1
        manifest.update(digest(dict(match_id=snapshot.match_id, picks=snapshot.picks,
            league_id=snapshot.league_id, model_version=snapshot.model_version,
            captured_at=aware(snapshot.captured_at).isoformat(),
            kickoff_at=aware(kickoff).isoformat() if kickoff else None, status=status)).encode())
    fingerprint = digest(dict(manifest=manifest.hexdigest(), terminal=jsonable_encoder(items), coverage=coverage))
    return items, coverage, fingerprint, observed_at


async def publish_head(session, report_id, observed_at):
    if session.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement=insert(LearningHead).values(method_version=METHOD,report_id=report_id,source_observed_at=observed_at)
    await session.execute(statement.on_conflict_do_update(index_elements=['method_version'],
        set_=dict(report_id=statement.excluded.report_id,source_observed_at=statement.excluded.source_observed_at),
        where=LearningHead.source_observed_at <= statement.excluded.source_observed_at))


async def refresh(db):
    # Release the scarce writer connection before doing any CPU calculations.
    async with db.session() as session:
        items, coverage, fingerprint, observed_at = await source_data(session)
        existing = await session.scalar(select(LearningReport.id).where(
            LearningReport.method_version==METHOD, LearningReport.data_fingerprint==fingerprint))
    if existing is not None:
        async with db.session() as session:
            await publish_head(session, existing, observed_at)
            await session.commit()
        return existing
    calculated = await asyncio.to_thread(analyze, items)
    coverage = dict(coverage, exclusions=calculated['excluded'], eligible_predictions=calculated['summary']['predictions'])
    async with db.session() as session:
        if session.bind.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        inserted = await session.scalar(insert(LearningReport).values(method_version=METHOD,
            data_fingerprint=fingerprint, source_observed_at=observed_at,
            summary=calculated['summary'], coverage=coverage,
            source_manifest=jsonable_encoder([{key:r[key] for key in ('match_id','pick_index','settlement_id',
                'revision','snapshot_hash','evidence_hash','kickoff_at','recorded_at','league_id','model_version')} for r in items])).on_conflict_do_nothing(
                index_elements=['method_version','data_fingerprint']).returning(LearningReport.id))
        if inserted is None:
            existing = await session.scalar(select(LearningReport.id).where(
                LearningReport.method_version==METHOD, LearningReport.data_fingerprint==fingerprint))
            await publish_head(session,existing,observed_at)
            await session.commit()
            return existing
        for model, values in ((LearningSlice,calculated['slices']), (LearningLesson,calculated['lessons'])):
            records = [dict(report_id=inserted, **value) for value in values]
            for record in records:
                if model is LearningLesson:
                    record['payload'] = dict(record['payload'], date=observed_at.isoformat(),
                        method_version=METHOD, data_fingerprint=fingerprint)
            for start in range(0,len(records),200):
                await session.execute(insert(model).values(records[start:start+200]))
        await publish_head(session,inserted,observed_at)
        await session.commit()
        return inserted


async def loop(db):
    # Independent cooperative task; never change Telegram's clocks or dispatch.
    await asyncio.sleep(30)
    while True:
        try:
            async with asyncio.timeout(120):
                await refresh(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('[LEARNING] refresh deferred error=%s',type(exc).__name__)
        await asyncio.sleep(900)

"""Read-only stored editions. No request calls the statistical engine."""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal
from sqlalchemy import select, func
from src.models import LearningReport, LearningSlice, LearningLesson, LearningHead, utcnow
from src.api.routes import session_dependency
from src.jobs.worker import aware

router = APIRouter(prefix='/learning',tags=['learning'])


def metadata(report, session=None):
    checked = session.info.get('learning_checked_at', report.source_observed_at) if session else report.source_observed_at
    age = max(0,(utcnow()-aware(checked)).total_seconds())
    return dict(report_id=report.id, method_version=report.method_version,
        data_fingerprint=report.data_fingerprint, created_at=report.created_at,
        source_observed_at=report.source_observed_at, checked_at=checked, age_seconds=age, stale=age>1800,
        automatic_model_change=False)


async def edition(session, report_id):
    if report_id is not None:
        report = await session.get(LearningReport,report_id)
        if report is None:
            raise HTTPException(404,'Learning report not found')
    else:
        from src.analytics.learning_metrics import METHOD
        head=await session.get(LearningHead,METHOD)
        report=await session.get(LearningReport,head.report_id) if head else None
        if head:session.info['learning_checked_at']=head.source_observed_at
    return report


@router.get('/summary')
async def summary(report_id:int|None=Query(None,ge=1),session=Depends(session_dependency)):
    report=await edition(session,report_id)
    return dict(status='ready',**metadata(report,session),summary=report.summary) if report else dict(status='warming',summary=None)


@router.get('/coverage')
async def coverage(report_id:int|None=Query(None,ge=1),session=Depends(session_dependency)):
    report=await edition(session,report_id)
    return dict(status='ready',**metadata(report,session),coverage=report.coverage) if report else dict(status='warming',coverage=None)


@router.get('/reports')
async def reports(limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),session=Depends(session_dependency)):
    rows=await session.scalars(select(LearningReport).order_by(LearningReport.id.desc()).offset(offset).limit(limit))
    return dict(items=[metadata(r) for r in rows],total=await session.scalar(select(func.count(LearningReport.id))),limit=limit,offset=offset)


@router.get('/statistics')
async def statistics(kind:Literal['league','market','model','league_market','calibration']='market',
        report_id:int|None=Query(None,ge=1),limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),session=Depends(session_dependency)):
    report=await edition(session,report_id)
    if report is None:
        return dict(status='warming',items=[],total=0,limit=limit,offset=offset)
    conditions=(LearningSlice.report_id==report.id,LearningSlice.kind==kind)
    rows=await session.scalars(select(LearningSlice).where(*conditions).order_by(LearningSlice.scope_key).offset(offset).limit(limit))
    return dict(status='ready',**metadata(report,session),items=[dict(scope_key=r.scope_key,**r.payload) for r in rows],
        total=await session.scalar(select(func.count(LearningSlice.id)).where(*conditions)),limit=limit,offset=offset)


@router.get('/lessons')
async def lessons(report_id:int|None=Query(None,ge=1),limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),session=Depends(session_dependency)):
    report=await edition(session,report_id)
    if report is None:
        return dict(status='warming',items=[],total=0,limit=limit,offset=offset)
    query=select(LearningLesson).where(LearningLesson.report_id==report.id)
    rows=await session.scalars(query.order_by(LearningLesson.scope_key).offset(offset).limit(limit))
    rows=list(rows)
    from src.analytics.learning_ai import config, PROMPT_VERSION
    from src.models import LearningAIExplanation
    cfg=config()
    explanations={r.lesson_id:r for r in await session.scalars(select(LearningAIExplanation).where(
        LearningAIExplanation.report_id==report.id,LearningAIExplanation.lesson_id.in_([r.id for r in rows]),
        LearningAIExplanation.prompt_version==PROMPT_VERSION))} if cfg.active and rows else {}
    def narrative(row):
        value=explanations.get(row.id)
        if value is None:return dict(status='not_prepared' if cfg.active else 'disabled',report_id=report.id)
        return dict(status='ready',report_id=value.report_id,lesson_id=value.lesson_id,model=value.model,
            prompt_version=value.prompt_version,created_at=value.created_at,sections=value.sections,facts=value.facts)
    return dict(status='ready',**metadata(report,session),items=[dict(scope_key=r.scope_key,**r.payload,ai=narrative(r)) for r in rows],
        total=await session.scalar(select(func.count(LearningLesson.id)).where(LearningLesson.report_id==report.id)),limit=limit,offset=offset)

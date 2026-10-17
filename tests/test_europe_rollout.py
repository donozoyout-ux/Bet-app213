import json
from dataclasses import asdict
import pytest
import src.scrapers.goaloo.europe_rollout as rollout
from src.scrapers.goaloo.client import SourceError


class Catalog:
    async def get_text(self,url):
        assert url==rollout.CATALOG_URL
        return 'arr[0] = '+json.dumps([1,'Albania',0,0,['315,ALB D1,1','348,ALB D2,1','999,ALB D3,1','1000,ALB D4,1']])+';'


async def test_bounded_resumable_verification_never_activates(monkeypatch):
    calls=[]
    async def verify(client,item,include_overlapping=False):
        assert include_overlapping
        calls.append(item.external_id)
        item.verified=True;item.verified_at='2026-10-16T00:00:00+00:00'
        item.season_counts={'2023-2024':1,'2024-2025':2}
        item.sample_match_ids={'2023-2024':123}
        return item
    monkeypatch.setattr(rollout,'verify_competition',verify)
    first=await rollout.verify_batch(Catalog())
    assert calls==[999,1000] and first['remaining']==0
    second=await rollout.verify_batch(Catalog(),first,batch_size=3)
    assert len(calls)==2 and second['remaining']==0
    assert not second['production_activation']
    assert all(x['activation']=='disabled_pending_review' and not x['coupon_eligible'] for x in second['competitions'])
    assert not (await rollout.verify_batch(Catalog(),second))['attempted']


async def test_budget_failure_and_explicit_retry(monkeypatch):
    async def timeout(*a,**k):raise TimeoutError
    monkeypatch.setattr(rollout,'verify_competition',timeout)
    report=await rollout.verify_batch(Catalog(),league_ids=[999],batch_size=1)
    assert report['remaining']==2 and report['attempted']==[999]
    assert not report['competitions'][0]['verified']
    assert 'time budget' in report['competitions'][0]['error']
    with pytest.raises(ValueError,match='absent'):await rollout.verify_batch(Catalog(),league_ids=[99999])
    with pytest.raises(ValueError,match='Batch size'):await rollout.verify_batch(Catalog(),batch_size=4)


async def test_catalog_change_or_resume_identity_mismatch_is_rejected(monkeypatch):
    async def noop(*a,**k):pass
    monkeypatch.setattr(rollout,'verify_competition',noop)
    report=await rollout.verify_batch(Catalog(),batch_size=1)
    report['catalog_revision']='outdated'
    with pytest.raises(SourceError,match='Catalog changed'):await rollout.verify_batch(Catalog(),report)
    report=await rollout.verify_batch(Catalog(),batch_size=1)
    report['competitions'][0]['country']='wrong country'
    with pytest.raises(SourceError,match='identity'):await rollout.verify_batch(Catalog(),report)


async def test_schedule_parse_errors_do_not_verify_partial_league(monkeypatch):
    import src.scrapers.goaloo.competitions as c
    async def seasons(*a,**k):return ['2023-2024']
    async def payload(*a,**k):return {}
    monkeypatch.setattr(c,'discover_seasons',seasons)
    monkeypatch.setattr(c,'season_data',payload)
    monkeypatch.setattr(c,'validate_identity',lambda *a:'Source league')
    monkeypatch.setattr(c,'discover_rounds',lambda *a:[1])
    def broken(payload,section,errors):
        errors.append({'error':'bad source row'})
        return []
    monkeypatch.setattr(c,'parse_matches',broken)
    target=c.Competition(315,'ALB D1','Albania','club','league',501)
    await c.verify_competition(None,target,include_overlapping=True)
    assert not target.verified and 'unparsed' in target.error


def test_provider_bosnia_and_faroe_labels_are_discovered_without_renaming():
    from src.scrapers.goaloo.competitions import select_european_leagues
    rows=[dict(external_id=70001,catalog_name='fixture league',country=country,schedule_format='league') for country in ('Bosnia','Faroe')]
    rows[1]['external_id']=70002
    found=select_european_leagues(rows)
    assert [x.country for x in found]==['Bosnia','Faroe']
    assert all(not x.verified for x in found)

from copy import deepcopy
import pytest
from src.scrapers.goaloo import competitions

def test_approved_europe_pilot_is_verified_and_strictly_scoped():
    rows=competitions.approved_europe_snapshot()
    assert [r['external_id'] for r in rows] == [315,348]
    assert {r['catalog_name'] for r in rows} == {'ALB D1','ALB D2'}
    assert all(r['verified'] and r['verified_at'] for r in rows)
    assert all(r['external_id'] in competitions.PRODUCTION_LEAGUE_IDS for r in rows)

def test_unverified_europe_report_is_rejected(monkeypatch,tmp_path):
    from pathlib import Path
    original=Path(__file__).resolve().parents[1] / 'docs' / 'europe-first-batch-live.json'
    import json
    report=json.loads(original.read_text(encoding='utf-8'))
    report['competitions'][0]['verified']=False
    fake=tmp_path/'report.json'
    fake.write_text(json.dumps(report),encoding='utf-8')
    original_read=Path.read_text
    def read_text(path,*args,**kwargs):
        return original_read(fake,*args,**kwargs) if path.name=='europe-first-batch-live.json' else original_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read_text)
    with pytest.raises(competitions.SourceError):
        competitions.approved_europe_snapshot()

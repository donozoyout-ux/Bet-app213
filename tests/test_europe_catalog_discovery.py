from src.scrapers.goaloo.competitions import select_european_leagues
from src.scrapers.goaloo.client import SourceError
import pytest

def test_discovers_domestic_european_leagues_without_cups_or_non_european():
    rows=[
      dict(external_id=57,catalog_name="POL D1",country="Poland",schedule_format="league"),
      dict(external_id=15,catalog_name="ENG PR",country="England",schedule_format="league"),
      dict(external_id=19,catalog_name="ENG Cup",country="England",schedule_format="cup"),
      dict(external_id=25,catalog_name="USA MLS",country="United States",schedule_format="league"),
    ]
    found=select_european_leagues(rows)
    assert [x.external_id for x in found]==[15,57]
    assert all(not x.verified and x.competition_type=="club" for x in found)

def test_catalog_duplicate_id_cannot_change_identity():
    rows=[
       dict(external_id=15,catalog_name="ENG PR",country="England",schedule_format="league"),
       dict(external_id=15,catalog_name="ENG D2",country="England",schedule_format="league"),
    ]
    with pytest.raises(SourceError,match="Ambiguous"):select_european_leagues(rows)

def test_identical_catalog_entries_are_deduplicated():
    row=dict(external_id=15,catalog_name="ENG PR",country="England",schedule_format="league")
    assert len(select_european_leagues([row,row]))==1

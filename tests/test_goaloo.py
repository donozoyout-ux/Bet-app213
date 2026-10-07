import json
from pathlib import Path
from datetime import datetime, timezone
import httpx
import pytest
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.results import parse_score, match_status
from src.scrapers.goaloo.handicap import parse_line, home_handicap
from src.scrapers.goaloo.totals import total_line
from src.scrapers.goaloo.odds import parse_odds, normalize_market, bookmaker_name, complete_odds, price
from src.scrapers.goaloo.seasons import discover_seasons

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def test_real_goaloo_league_fixture():
    payload = fixture('goaloo_league.json')
    assert discover_rounds(payload) == [1]
    matches = list(parse_matches(payload, 1))
    assert len(matches) == 10
    first = matches[0]
    assert first['external_match_id'] == 2590898
    assert first['home_name'] == 'Manchester United'
    assert first['away_name'] == 'Fulham'
    assert first['kickoff_at'] == datetime(2024, 8, 16, 19, tzinfo=timezone.utc)
    assert (first['ht_home'], first['ht_away'], first['ft_home'], first['ft_away']) == (0, 0, 1, 0)
    assert first['status'] == 'finished'


def test_results_unknown_and_missing():
    assert parse_score('') == (None, None)
    assert parse_score('0-0') == (0, 0)
    assert match_status(-14) == 'postponed'
    assert match_status(99) == 'unknown'
    with pytest.raises(ValueError):
        parse_score('1:0 extra')


@pytest.mark.parametrize('raw,expected', [('0',0),('0/0.5',0.25),('0.5/1',0.75),('-0.5/1',-0.75),('-0.5/-1',-0.75),('3/3.5',3.25),('',None)])
def test_split_lines(raw, expected):
    assert parse_line(raw) == expected


def test_handicap_and_totals_conventions():
    assert home_handicap('1') == -1
    assert home_handicap('-0.5') == 0.5
    assert total_line('2.5/3') == 2.75
    with pytest.raises(ValueError):
        total_line('-1')
    with pytest.raises(ValueError):
        parse_line('nan')


def test_bookmaker_matching():
    assert bookmaker_name(' BET 365 ') == 'Bet365'
    assert bookmaker_name('SBOBET') == 'Sbobet'
    assert bookmaker_name('Crow') is None
    assert bookmaker_name('Unibet') is None


def test_verified_initial_latest_and_inplay_are_separate():
    odds = parse_odds(fixture('goaloo_odds.json'), final=True)
    assert set(odds) == {'Crown','Bet365','Sbobet'}
    crown = odds['Crown']
    assert crown['1x2']['opening_home'] == 1.53
    assert crown['1x2']['closing_home'] == 1.66
    assert crown['1x2']['raw']['r']['u'] == '1.02'
    assert crown['ah']['opening_line'] == -1
    assert crown['ah']['closing_line'] == -0.75
    assert crown['ah']['opening_home'] == 1.83
    assert crown['ou']['opening_line'] == 3.25
    assert crown['ou']['closing_line'] == 3
    assert crown['ou']['opening_over'] == 1.9
    assert complete_odds(odds)
    odds.pop('Sbobet')
    assert not complete_odds(odds)


def test_no_premature_closing_or_fabricated_missing_prices():
    odds = parse_odds(fixture('goaloo_odds.json'), final=False)
    assert odds['Crown']['1x2']['closing_home'] is None
    assert odds['Crown']['1x2']['latest_home'] == 1.66
    assert complete_odds(odds, False)
    assert not complete_odds(odds, True)
    assert price('0', True) is None
    assert price('nan') is None
    assert price('0.8', True) == 1.8
    missing = normalize_market({}, 'ou', True)
    assert missing['closing_line'] is None


def test_schema_changes_fail_loudly():
    with pytest.raises(SourceError):
        parse_odds({'Data': []})
    payload = fixture('goaloo_odds.json')
    payload['Data']['mixodds'][0]['cn'] = 'Crown'
    with pytest.raises(SourceError, match='mismatch'):
        parse_odds(payload)


async def test_http_bom_headers_and_discovery():
    def handler(request):
        assert request.headers['X-Requested-With'] == 'XMLHttpRequest'
        assert request.headers['Referer'].startswith('https://www.goaloo.com')
        return httpx.Response(200, text='\ufeff' + json.dumps(fixture('goaloo_seasons.json')))
    async with GoalooClient(httpx.MockTransport(handler)) as client:
        assert await discover_seasons(client, 36, 2024) == ['2024-2025','2025-2026','2026-2027']


async def test_http_retry_and_provider_application_error(monkeypatch):
    from dataclasses import replace
    import src.scrapers.goaloo.client as module
    monkeypatch.setattr(module, 'settings', replace(module.settings, request_interval=0, retries=2))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={'SeasonList': ['2024-2025']})
    async with GoalooClient(httpx.MockTransport(handler)) as client:
        assert await discover_seasons(client,36) == ['2024-2025']
    assert len(calls) == 2
    def rejection(request):
        return httpx.Response(200,json={'code':1002})
    async with GoalooClient(httpx.MockTransport(rejection)) as client:
        with pytest.raises(SourceError):
            await client.get('https://www.goaloo.com/ajax')

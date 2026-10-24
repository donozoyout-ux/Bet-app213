"""Private, bounded history snapshots. No fixtures are written to disk or GitHub.

CSV totals are deliberately separate: they cannot reconstruct form, H2H or an
as-of training set. A cache is a replacement for a SQL read, never extra rows.
"""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import time
from types import SimpleNamespace
from weakref import WeakKeyDictionary

from sqlalchemy import select, func, true
from sqlalchemy.orm import aliased
from src.models import Match, MatchStatistics, Team

TTL = 600
MAX_ROWS = 2000
MAX_LEAGUES = 8
HISTORY_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
_databases = WeakKeyDictionary()


def enabled():
    return os.getenv('HYBRID_HISTORY_ENABLED', 'false').lower() == 'true'


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


@dataclass
class Snapshot:
    revision: tuple
    upper: datetime
    expires: float
    rows: tuple


async def fingerprint(session, league_id):
    """One small result; finished history alone, excluding live quote churn."""
    matches = select(func.count(Match.id), func.max(Match.updated_at),
                     func.max(Match.last_scraped_at)).where(
        Match.league_id == league_id, Match.status == 'finished').subquery()
    stats = select(func.count(MatchStatistics.match_id), func.max(MatchStatistics.updated_at)).join(
        Match, Match.id == MatchStatistics.match_id).where(
        Match.league_id == league_id, Match.status == 'finished').subquery()
    # Names are display evidence as well. This tiny query remains bounded on wire.
    participants = select(Match.home_team_id).where(Match.league_id == league_id, Match.status == 'finished').union(
        select(Match.away_team_id).where(Match.league_id == league_id, Match.status == 'finished'))
    teams = select(func.count(Team.id), func.max(Team.name), func.sum(func.length(Team.name))).where(
        Team.id.in_(participants)).subquery()
    return tuple((await session.execute(select(*matches.c, *stats.c, *teams.c)
        .select_from(matches.join(stats, true()).join(teams, true())))).one())


async def historical_rows(session, league_id, cutoff, exclude_id):
    """Return an as-of replacement, or None for the original bounded SQL path.

    Load a ten-minute envelope, then apply observation times and the completion
    buffer at EVERY read. Time advancing across the buffer needs no DB transfer.
    Truncated envelopes fall back rather than silently dropping eligible rows.
    """
    if not enabled() or cutoff < HISTORY_START:
        return None
    if session.new or session.dirty or session.deleted or session.info.get('model_changed'):
        return None  # Never publish uncommitted writer state into a shared cache.
    local = session.info.setdefault('hybrid_history', {})
    key = (league_id, cutoff)
    if key not in local:
        revision = await fingerprint(session, league_id)
        entries = _databases.setdefault(getattr(session.bind, 'sync_engine', session.bind), OrderedDict())
        cached = entries.get(league_id)
        if not (cached and cached.revision == revision and cutoff <= cached.upper
                and time.monotonic() < cached.expires):
            upper = cutoff + timedelta(seconds=TTL)
            home, away = aliased(Team), aliased(Team)
            mc = [c for c in Match.__table__.c if c.name != 'raw']
            sc = [c for c in MatchStatistics.__table__.c if c.name != 'raw']
            query = select(*[c.label('m_' + c.name) for c in mc],
                           *[c.label('s_' + c.name) for c in sc],
                           home.name.label('home_name'), away.name.label('away_name')).join(
                home, Match.home_team_id == home.id).join(away, Match.away_team_id == away.id).outerjoin(
                MatchStatistics, MatchStatistics.match_id == Match.id).where(
                Match.league_id == league_id, Match.status == 'finished',
                Match.kickoff_at >= HISTORY_START, Match.kickoff_at <= upper - timedelta(hours=3))\
                .order_by(Match.kickoff_at.desc(), Match.id.desc()).limit(MAX_ROWS + 1)
            records = (await session.execute(query)).mappings().all()
            if len(records) > MAX_ROWS:
                local[key] = None
                return None
            rows = []
            for row in records:
                match = SimpleNamespace(**{c.name: row['m_' + c.name] for c in mc})
                stats = SimpleNamespace(**{c.name: row['s_' + c.name] for c in sc}) if row['s_match_id'] else None
                rows.append((match, stats, row['home_name'], row['away_name']))
            cached = Snapshot(revision, upper, time.monotonic() + TTL, tuple(rows))
            entries[league_id] = cached
            while len(entries) > MAX_LEAGUES:
                entries.popitem(last=False)
        entries.move_to_end(league_id)
        local[key] = cached.rows
    rows = local[key]
    if rows is None:
        return None
    lower = max(HISTORY_START, cutoff - timedelta(days=3 * 366))
    result = []
    for match, stats, home, away in rows:
        if match.id == exclude_id or not lower <= aware(match.kickoff_at) <= cutoff - timedelta(hours=3):
            continue
        if any(aware(value) > cutoff for value in (match.created_at, match.updated_at, match.last_scraped_at) if value):
            continue
        if stats and (not stats.is_final or aware(stats.updated_at) > cutoff):
            stats = None
        result.append((match, stats, home, away))
    return result


def invalidate(engine):
    _databases.pop(engine, None)

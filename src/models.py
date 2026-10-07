from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, JSON, UniqueConstraint, Index, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class League(Base):
    __tablename__ = 'leagues'
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(150))
    country: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(30), default='goaloo')


class Season(Base):
    __tablename__ = 'seasons'
    __table_args__ = (UniqueConstraint('league_id', 'season_name'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'))
    season_name: Mapped[str] = mapped_column(String(20))


class Team(Base):
    __tablename__ = 'teams'
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(150))


class Match(Base):
    __tablename__ = 'matches'
    __table_args__ = (Index('ix_matches_season_round', 'season_id', 'round'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    external_match_id: Mapped[int] = mapped_column(unique=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'))
    season_id: Mapped[int] = mapped_column(ForeignKey('seasons.id'))
    round: Mapped[int] = mapped_column(Integer)
    kickoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    home_team_id: Mapped[int] = mapped_column(ForeignKey('teams.id'))
    away_team_id: Mapped[int] = mapped_column(ForeignKey('teams.id'))
    status: Mapped[str] = mapped_column(String(30), index=True)
    ht_home: Mapped[int | None]
    ht_away: Mapped[int | None]
    ft_home: Mapped[int | None]
    ft_away: Mapped[int | None]
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    odds_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Bookmaker(Base):
    __tablename__ = 'bookmakers'
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class OddsBase:
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'), index=True)
    bookmaker_id: Mapped[int] = mapped_column(ForeignKey('bookmakers.id'))
    raw: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Odds1X2(OddsBase, Base):
    __tablename__ = 'odds_1x2'
    __table_args__ = (UniqueConstraint('match_id', 'bookmaker_id'),)
    opening_home: Mapped[float | None]
    opening_draw: Mapped[float | None]
    opening_away: Mapped[float | None]
    closing_home: Mapped[float | None]
    closing_draw: Mapped[float | None]
    closing_away: Mapped[float | None]
    latest_home: Mapped[float | None]
    latest_draw: Mapped[float | None]
    latest_away: Mapped[float | None]


class AsianHandicap(OddsBase, Base):
    __tablename__ = 'asian_handicap'
    __table_args__ = (UniqueConstraint('match_id', 'bookmaker_id'),)
    opening_line: Mapped[float | None]
    opening_home: Mapped[float | None]
    opening_away: Mapped[float | None]
    closing_line: Mapped[float | None]
    closing_home: Mapped[float | None]
    closing_away: Mapped[float | None]
    latest_line: Mapped[float | None]
    latest_home: Mapped[float | None]
    latest_away: Mapped[float | None]


class AsianTotals(OddsBase, Base):
    __tablename__ = 'asian_totals'
    __table_args__ = (UniqueConstraint('match_id', 'bookmaker_id'),)
    opening_line: Mapped[float | None]
    opening_over: Mapped[float | None]
    opening_under: Mapped[float | None]
    closing_line: Mapped[float | None]
    closing_over: Mapped[float | None]
    closing_under: Mapped[float | None]
    latest_line: Mapped[float | None]
    latest_over: Mapped[float | None]
    latest_under: Mapped[float | None]


class OddsSnapshot(Base):
    __tablename__ = 'odds_snapshots'
    __table_args__ = (UniqueConstraint('match_id', 'bookmaker_id', 'market', 'source_timestamp'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'))
    bookmaker_id: Mapped[int] = mapped_column(ForeignKey('bookmakers.id'))
    market: Mapped[str] = mapped_column(String(20))
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw: Mapped[dict] = mapped_column(JSON)


class ScraperJob(Base):
    __tablename__ = 'scraper_jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'))
    start_year: Mapped[int] = mapped_column(default=2024)
    match_id: Mapped[int | None] = mapped_column(ForeignKey('matches.id'))
    current_season: Mapped[str | None] = mapped_column(String(20))
    current_round: Mapped[int | None]
    total_matches: Mapped[int] = mapped_column(default=0)
    processed_matches: Mapped[int] = mapped_column(default=0)
    failed_matches: Mapped[int] = mapped_column(default=0)
    discovery_complete: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobItem(Base):
    __tablename__ = 'scraper_job_items'
    __table_args__ = (UniqueConstraint('job_id', 'match_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('scraper_jobs.id'), index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'))
    status: Mapped[str] = mapped_column(String(20), default='queued')
    error: Mapped[str | None] = mapped_column(String(500))


class ScraperIssue(Base):
    __tablename__ = 'scraper_issues'
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('scraper_jobs.id'), index=True)
    external_match_id: Mapped[str] = mapped_column(String(80))
    season: Mapped[str] = mapped_column(String(20))
    round: Mapped[int]
    error: Mapped[str] = mapped_column(String(500))
    raw: Mapped[dict] = mapped_column(JSON)

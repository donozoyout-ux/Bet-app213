from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, JSON, UniqueConstraint, Index, Boolean, CheckConstraint
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
    competition_type: Mapped[str] = mapped_column(String(16), default='club')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    backfill_from_year: Mapped[int] = mapped_column(default=2024)
    priority: Mapped[int] = mapped_column(default=100)
    schedule_format: Mapped[str] = mapped_column(String(8), default='league')
    latest_season: Mapped[str | None] = mapped_column(String(20))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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
    __table_args__ = (Index('ix_matches_season_round', 'season_id', 'round'),
                      Index('ix_matches_league_status_kickoff', 'league_id', 'status', 'kickoff_at', 'id'))
    id: Mapped[int] = mapped_column(primary_key=True)
    external_match_id: Mapped[int] = mapped_column(unique=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'))
    season_id: Mapped[int] = mapped_column(ForeignKey('seasons.id'))
    round: Mapped[int] = mapped_column(Integer)
    round_label: Mapped[str | None] = mapped_column(String(160))
    stage_key: Mapped[str | None] = mapped_column(String(80))
    kickoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    home_team_id: Mapped[int] = mapped_column(ForeignKey('teams.id'))
    away_team_id: Mapped[int] = mapped_column(ForeignKey('teams.id'))
    status: Mapped[str] = mapped_column(String(30), index=True)
    live_minute: Mapped[str | None] = mapped_column(String(12))
    live_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referee_id: Mapped[int | None] = mapped_column(ForeignKey('referees.id'), index=True)
    referee_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class Referee(Base):
    __tablename__='referees'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64),unique=True)
    external_id: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(150))
    source: Mapped[str] = mapped_column(String(30),default='goaloo')


class MatchStatistics(Base):
    __tablename__='match_statistics'
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'),primary_key=True)
    home_corners: Mapped[int | None]
    away_corners: Mapped[int | None]
    home_corners_ht: Mapped[int | None]
    away_corners_ht: Mapped[int | None]
    home_yellow_cards: Mapped[int | None]
    away_yellow_cards: Mapped[int | None]
    home_red_cards: Mapped[int | None]
    away_red_cards: Mapped[int | None]
    home_shots: Mapped[int | None]
    away_shots: Mapped[int | None]
    home_shots_on_target: Mapped[int | None]
    away_shots_on_target: Mapped[int | None]
    home_fouls: Mapped[int | None]
    away_fouls: Mapped[int | None]
    home_offsides: Mapped[int | None]
    away_offsides: Mapped[int | None]
    home_possession: Mapped[float | None]
    away_possession: Mapped[float | None]
    is_final: Mapped[bool] = mapped_column(Boolean,default=False)
    collection_status: Mapped[str] = mapped_column(String(20),default='unavailable')
    source: Mapped[str] = mapped_column(String(30),default='goaloo')
    raw: Mapped[dict] = mapped_column(JSON,default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)


class MatchEvent(Base):
    __tablename__='match_events'
    __table_args__=(UniqueConstraint('match_id','source_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'),index=True)
    source_key: Mapped[str] = mapped_column(String(64))
    minute: Mapped[int | None]
    stoppage_minute: Mapped[int | None]
    team_side: Mapped[str | None] = mapped_column(String(8))
    event_type: Mapped[str] = mapped_column(String(30))
    player_name: Mapped[str | None] = mapped_column(String(150))
    secondary_player_name: Mapped[str | None] = mapped_column(String(150))
    raw: Mapped[dict] = mapped_column(JSON,default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)


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


class MarketOdds(OddsBase, Base):
    """Period- and market-tagged odds from verified Goaloo endpoints.

    Covers half-time goals OU (t=1,h=1), corner OU (t=4), and any future
    verified market that is not in the legacy 1x2/AH/OU full-time tables.
    Each row is uniquely identified by match + bookmaker + market + period.
    """
    __tablename__ = 'market_odds'
    __table_args__ = (
        UniqueConstraint('match_id', 'bookmaker_id', 'market', 'period'),
        Index('ix_market_odds_match_market', 'match_id', 'market'),
    )
    market: Mapped[str] = mapped_column(String(30))
    period: Mapped[str] = mapped_column(String(20))
    opening_line: Mapped[float | None]
    opening_over: Mapped[float | None]
    opening_under: Mapped[float | None]
    closing_line: Mapped[float | None]
    closing_over: Mapped[float | None]
    closing_under: Mapped[float | None]
    latest_line: Mapped[float | None]
    latest_over: Mapped[float | None]
    latest_under: Mapped[float | None]


class ScraperJob(Base):
    __tablename__ = 'scraper_jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    priority: Mapped[int] = mapped_column(default=100)
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'))
    start_year: Mapped[int] = mapped_column(default=2024)
    match_id: Mapped[int | None] = mapped_column(ForeignKey('matches.id'))
    current_season: Mapped[str | None] = mapped_column(String(20))
    current_round: Mapped[int | None]
    total_matches: Mapped[int] = mapped_column(default=0)
    processed_matches: Mapped[int] = mapped_column(default=0)
    failed_matches: Mapped[int] = mapped_column(default=0)
    no_odds_matches: Mapped[int] = mapped_column(default=0)
    discovery_complete: Mapped[bool] = mapped_column(default=False)
    discovery_metrics: Mapped[dict | None] = mapped_column(JSON)
    discovery_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class PredictionSnapshot(Base):
    """First qualified prematch selection, never rewritten after seeing results."""
    __tablename__ = 'prediction_snapshots'
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'), primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    model_version: Mapped[str] = mapped_column(String(100), default='quality-poisson-v1')
    picks: Mapped[list] = mapped_column(JSON)


class PredictionBoard(Base):
    """Replaceable league cache; immutable published picks remain separate."""
    __tablename__ = 'prediction_boards'
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(60))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_revision: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)


class CollectorRetry(Base):
    """Source cooldown survives scraper and free-instance restarts."""
    __tablename__ = 'collector_retries'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    failures: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(80))


class SavedCoupon(Base):
    """Immutable version; anonymous browser edits fork automatic coupons."""
    __tablename__ = 'saved_coupons'
    __table_args__ = (UniqueConstraint('root_id','revision'),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    root_id: Mapped[str] = mapped_column(String(36),index=True)
    revision: Mapped[int]
    owner_hash: Mapped[str | None] = mapped_column(String(64),index=True)
    kind: Mapped[str] = mapped_column(String(20))
    automatic_fingerprint: Mapped[str | None] = mapped_column(String(64),unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON)


class TelegramDelivery(Base):
    """At most one network attempt per version and automatic window."""
    __tablename__ = 'telegram_deliveries'
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    coupon_id: Mapped[str] = mapped_column(ForeignKey('saved_coupons.id'),unique=True)
    slot: Mapped[str | None] = mapped_column(String(40),unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20),index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    message_id: Mapped[int | None]


class PredictionSettlement(Base):
    """Append-only terminal decisions; revisions require an explicit correction."""
    __tablename__ = 'prediction_settlements'
    __table_args__ = (UniqueConstraint('match_id', 'pick_index', 'revision'),
        CheckConstraint("outcome IN ('won','lost','void')"),
        CheckConstraint('revision >= 1 AND pick_index >= 0'))
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey('prediction_snapshots.match_id'), index=True)
    pick_index: Mapped[int]
    revision: Mapped[int] = mapped_column(default=1)
    outcome: Mapped[str] = mapped_column(String(16))
    actual: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    evaluator_version: Mapped[str] = mapped_column(String(40))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(500))


class LearningReport(Base):
    """Immutable complete analysis edition; source changes publish another edition."""
    __tablename__ = 'learning_reports'
    __table_args__ = (UniqueConstraint('method_version', 'data_fingerprint'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    method_version: Mapped[str] = mapped_column(String(80))
    data_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_manifest: Mapped[list] = mapped_column(JSON, deferred=True)
    summary: Mapped[dict] = mapped_column(JSON)
    coverage: Mapped[dict] = mapped_column(JSON)


class LearningSlice(Base):
    __tablename__ = 'learning_slices'
    __table_args__ = (UniqueConstraint('report_id', 'kind', 'scope_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('learning_reports.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    scope_key: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict] = mapped_column(JSON)


class LearningLesson(Base):
    __tablename__ = 'learning_lessons'
    __table_args__ = (UniqueConstraint('report_id', 'scope_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('learning_reports.id'), index=True)
    scope_key: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict] = mapped_column(JSON)


class LearningHead(Base):
    """Mutable publication pointer; report and lesson editions stay immutable."""
    __tablename__ = 'learning_heads'
    method_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('learning_reports.id'))
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class LearningAIExplanation(Base):
    """Append-only accepted narrative, distinct from statistical evidence."""
    __tablename__ = 'learning_ai_explanations'
    __table_args__ = (UniqueConstraint('lesson_id', 'prompt_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('learning_reports.id'), index=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey('learning_lessons.id'), index=True)
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    sections: Mapped[dict] = mapped_column(JSON)
    facts: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningAITask(Base):
    __tablename__ = 'learning_ai_tasks'
    __table_args__ = (UniqueConstraint('lesson_id', 'prompt_version'),
        CheckConstraint('attempts >= 0 AND attempts <= 3'),
        Index('ix_learning_ai_task_queue', 'report_id', 'prompt_version', 'status', 'next_attempt_at'),
        Index('ix_learning_ai_task_recovery', 'status', 'updated_at'))
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('learning_reports.id'), index=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey('learning_lessons.id'), index=True)
    prompt_version: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(32))


class LearningAIQuota(Base):
    __tablename__ = 'learning_ai_quotas'
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    requests: Mapped[int] = mapped_column(default=0)
    __table_args__ = (CheckConstraint('requests >= 0'),)


class LearningAIGate(Base):
    __tablename__ = 'learning_ai_gates'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BoardEdition(Base):
    """Content-addressed publication history; never rewritten by an HTTP read."""
    __tablename__ = 'board_editions'
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, deferred=True)


class PredictionPublication(Base):
    """Append-only match selections at each changed league publication."""
    __tablename__ = 'prediction_publications'
    __table_args__ = (Index('ix_prediction_publications_match_id_id', 'match_id', 'id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'))
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'), index=True)
    board_version: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(100))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    picks: Mapped[list] = mapped_column(JSON)


class BoardHead(Base):
    __tablename__ = 'board_heads'
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'), primary_key=True)
    version: Mapped[str] = mapped_column(ForeignKey('board_editions.version'))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow)


class DailyCouponPublication(Base):
    __tablename__ = 'daily_coupon_publications'
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(16), primary_key=True)
    input_revision: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, deferred=True)


class PublishedMatchAnalysis(Base):
    """Worker-built current analysis; separate from the lightweight strip JSON."""
    __tablename__ = 'published_match_analyses'
    match_id: Mapped[int] = mapped_column(ForeignKey('matches.id'),primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    board_version: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(100))
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON,deferred=True)


class BoardReadPublication(Base):
    """Worker-built split read model; atomically follows one immutable edition."""
    __tablename__ = 'board_read_publications'
    league_id: Mapped[int] = mapped_column(ForeignKey('leagues.id'), primary_key=True)
    version: Mapped[str] = mapped_column(ForeignKey('board_editions.version'))
    items: Mapped[list] = mapped_column(JSON, deferred=True)
    availability: Mapped[list] = mapped_column(JSON, deferred=True)
    evaluated: Mapped[int] = mapped_column()
    coupon_records: Mapped[list] = mapped_column(JSON, deferred=True)
    engine_analysis: Mapped[list] = mapped_column(JSON, deferred=True)


class WorkerCheckpoint(Base):
    """Additive runtime checkpoint v1; source records and decisions are untouched."""
    __tablename__ = 'worker_checkpoints'
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[int] = mapped_column(default=0)
    examined: Mapped[int] = mapped_column(default=0)
    written: Mapped[int] = mapped_column(default=0)
    failures: Mapped[int] = mapped_column(default=0)
    error_type: Mapped[str | None] = mapped_column(String(80))

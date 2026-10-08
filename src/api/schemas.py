from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LeagueResponse(ORMResponse):
    id: int
    external_id: int
    name: str
    country: str
    source: str
    competition_type: str
    enabled: bool
    backfill_from_year: int
    priority: int
    latest_season: str | None = None


class SeasonResponse(ORMResponse):
    id: int
    league_id: int
    season_name: str
    rounds: list[int] = Field(default_factory=list)
    round_labels: dict[int, str] = Field(default_factory=dict)


class BookmakerResponse(ORMResponse):
    id: int
    external_id: int
    name: str


class JobResponse(ORMResponse):
    id: int
    kind: str
    status: str
    league_id: int
    current_league: str | None = None
    current_season: str | None
    current_round: int | None
    total_matches: int
    processed_matches: int
    failed_matches: int
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    discovery_complete: bool


class JobRequest(BaseModel):
    league_id: int = Field(default=36, gt=0, description='Goaloo external league ID')
    start_year: int = Field(default=2024, ge=2024, le=2100)
    resume_job_id: int | None = Field(default=None, gt=0)


class OddsResponse(BaseModel):
    bookmaker: str
    bookmaker_id: int
    market: str
    opening: dict[str, float | None]
    latest: dict[str, float | None]
    closing: dict[str, float | None]
    format: str = 'decimal'
    line_perspective: str | None = None
    updated_at: datetime
    raw: dict


class MatchResponse(BaseModel):
    id: int
    external_match_id: int
    league_id: int
    external_league_id: int
    league: str
    season: str
    round: int
    round_label: str | None = None
    stage_key: str | None = None
    competition_type: str = 'club'
    kickoff_at: datetime | None
    home_team: str
    away_team: str
    status: str
    ht_home: int | None
    ht_away: int | None
    ft_home: int | None
    ft_away: int | None
    odds_complete: bool
    updated_at: datetime
    odds: list[OddsResponse] = Field(default_factory=list)


class MatchDetail(MatchResponse):
    odds: list[OddsResponse]
    raw: dict


class MatchPage(BaseModel):
    items: list[MatchResponse]
    total: int
    limit: int
    offset: int
    upcoming_expanded: bool = False


class StatusResponse(BaseModel):
    service: str = 'BetApp213'
    database: str
    database_engine: str | None = None
    total_matches: int | None = None
    total_odds: int | None = None
    last_scraped_at: datetime | None = None
    app_env: str


class ScraperStatus(BaseModel):
    status: str
    jobs: list[JobResponse]

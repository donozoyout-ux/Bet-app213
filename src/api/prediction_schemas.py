from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
from .schemas import MatchResponse


class Prediction(BaseModel):
    status: Literal['ok','insufficient_data']
    home_probability: float | None = None
    draw_probability: float | None = None
    away_probability: float | None = None
    over_25_probability: float | None = None
    under_25_probability: float | None = None
    over_15_probability: float | None = None
    under_15_probability: float | None = None
    over_35_probability: float | None = None
    under_35_probability: float | None = None
    btts_probability: float | None = None
    no_btts_probability: float | None = None
    expected_home_goals: float | None = None
    expected_away_goals: float | None = None
    confidence: Literal['low','medium','high']
    sample_size: int
    league_sample_size: int
    home_sample_size: int
    away_sample_size: int
    home_venue_sample_size: int
    away_venue_sample_size: int
    model_version: str
    confidence_rules: dict
    model_market_difference: dict[str,float] | None = None
    corners_status: str = 'insufficient_data'
    yellow_cards_status: str = 'insufficient_data'
    expected_home_yellow_cards: float | None = None
    expected_away_yellow_cards: float | None = None
    expected_total_yellow_cards: float | None = None
    over_3_5_yellow_cards_probability: float | None = None
    over_4_5_yellow_cards_probability: float | None = None
    over_5_5_yellow_cards_probability: float | None = None
    red_cards_status: str = 'insufficient_data'
    expected_home_red_cards: float | None = None
    expected_away_red_cards: float | None = None
    expected_total_red_cards: float | None = None
    over_0_5_red_cards_probability: float | None = None
    over_1_5_red_cards_probability: float | None = None
    over_2_5_red_cards_probability: float | None = None
    cards_status: str = 'insufficient_data'
    card_basis: str = 'yellow_plus_red'
    expected_home_corners: float | None = None
    expected_away_corners: float | None = None
    expected_total_corners: float | None = None
    expected_home_cards: float | None = None
    expected_away_cards: float | None = None
    expected_total_cards: float | None = None
    over_8_5_corners_probability: float | None = None
    over_9_5_corners_probability: float | None = None
    over_10_5_corners_probability: float | None = None
    over_3_5_cards_probability: float | None = None
    over_4_5_cards_probability: float | None = None
    over_5_5_cards_probability: float | None = None
    first_half_goals_status: str = 'insufficient_data'
    expected_home_first_half_goals: float | None = None
    expected_away_first_half_goals: float | None = None
    expected_total_first_half_goals: float | None = None
    over_0_5_first_half_goals_probability: float | None = None
    over_1_5_first_half_goals_probability: float | None = None
    over_2_5_first_half_goals_probability: float | None = None
    second_half_goals_status: str = 'insufficient_data'
    expected_home_second_half_goals: float | None = None
    expected_away_second_half_goals: float | None = None
    expected_total_second_half_goals: float | None = None
    over_0_5_second_half_goals_probability: float | None = None
    over_1_5_second_half_goals_probability: float | None = None
    over_2_5_second_half_goals_probability: float | None = None


class FormMatch(BaseModel):
    id: int
    date: datetime
    home: str
    away: str
    home_goals: int
    away_goals: int
    goals_for: int
    goals_against: int
    result: Literal['G','B','M']


class TeamStats(BaseModel):
    matches: list[FormMatch]
    sample_size: int
    form: list[str]
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    avg_goals_for: float | None
    avg_goals_against: float | None
    win_rate: float | None
    draw_rate: float | None
    loss_rate: float | None
    clean_sheet_rate: float | None
    btts_rate: float | None
    over_15_rate: float | None
    over_25_rate: float | None
    over_35_rate: float | None
    avg_total_goals: float | None


class BookmakerStats(BaseModel):
    bookmaker: str
    opening: dict[str,float | None]
    prices: dict[str,float | None]
    stage: str
    observed_at: datetime
    available_as_of: bool
    implied_probabilities: dict[str,float] | None


class Recommendation(BaseModel):
    id: str
    market: str
    selection: str
    label: str
    probability: float
    confidence: Literal['low','medium','high']
    score: float
    reliability: float
    sample_size: int
    correlation_group: str
    evidence: dict
    score_components: dict[str,float]
    penalties: dict[str,float]
    rejection_reasons: list[str] = Field(default_factory=list)
    qualifies: bool
    market_probability: float | None
    bookmaker_count: int
    model_market_difference: float | None


class Statistics(BaseModel):
    market_availability: dict = Field(default_factory=dict)
    match: MatchResponse
    prediction: Prediction
    home_form: TeamStats
    away_form: TeamStats
    home_last_10: TeamStats
    away_last_10: TeamStats
    home_split: TeamStats
    away_split: TeamStats
    h2h: list[FormMatch]
    h2h_summary: dict
    bookmakers: list[BookmakerStats]
    bookmaker_consensus: dict[str,float] | None
    as_of: datetime
    historical: bool
    cache_seconds: int
    methodology: str
    additional_statistics: dict = Field(default_factory=dict)
    candidates: list[Recommendation] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    strongest_prediction: str | None = None
    ranking_evidence: dict = Field(default_factory=dict)
    asian_handicap_candidates: list[dict] = Field(default_factory=list)


class PredictionCard(BaseModel):
    match: MatchResponse
    prediction: Prediction
    bookmaker_consensus: dict[str,float] | None
    as_of: datetime
    match_id: int
    recommendations: list[Recommendation]
    strongest_prediction: str


class PredictionPage(BaseModel):
    availability: dict = Field(default_factory=dict)
    items: list[PredictionCard]
    window_hours: int
    generated_at: datetime | None
    cache: dict = Field(default_factory=dict)


class GlobalPick(BaseModel):
    match: MatchResponse
    recommendation: Recommendation
    as_of: datetime


class GlobalPicks(BaseModel):
    availability: dict = Field(default_factory=dict)
    items: list[GlobalPick]
    generated_at: datetime | None
    cache: dict = Field(default_factory=dict)
    evaluated_matches: int

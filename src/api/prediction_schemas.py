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


class Statistics(BaseModel):
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


class PredictionCard(BaseModel):
    match: MatchResponse
    prediction: Prediction
    bookmaker_consensus: dict[str,float] | None
    as_of: datetime


class PredictionPage(BaseModel):
    items: list[PredictionCard]
    window_hours: int
    generated_at: datetime

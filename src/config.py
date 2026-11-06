import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv('DATABASE_URL', '')
    app_env: str = os.getenv('APP_ENV', 'development')
    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    scraper_token: str = os.getenv('SCRAPER_API_TOKEN', '')
    worker_enabled: bool = os.getenv('SCRAPER_WORKER_ENABLED', 'true').lower() == 'true'
    auto_backfill_on_empty: bool = os.getenv('AUTO_BACKFILL_ON_EMPTY', 'true').lower() == 'true'
    request_interval: float = max(0.2, float(os.getenv('GOALOO_REQUEST_INTERVAL', '1')))
    request_timeout: float = max(1, float(os.getenv('GOALOO_TIMEOUT', '25')))
    retries: int = max(1, int(os.getenv('GOALOO_RETRIES', '3')))
    concurrency: int = max(1, min(8, int(os.getenv('GOALOO_CONCURRENCY', '2'))))
    save_snapshots: bool = os.getenv('GOALOO_SAVE_SNAPSHOTS', 'false').lower() == 'true'
    market_odds_pilot_enabled: bool = os.getenv('MARKET_ODDS_PILOT_ENABLED', 'true').lower() == 'true'
    market_odds_pilot_league: int = int(os.getenv('MARKET_ODDS_PILOT_LEAGUE', '36'))
    market_odds_pilot_max_matches: int = int(os.getenv('MARKET_ODDS_PILOT_MAX_MATCHES', '10'))
    r2_enabled: bool = os.getenv('R2_ENABLED', 'false').lower() == 'true'
    r2_required: bool = os.getenv('R2_REQUIRED', 'false').lower() == 'true'
    r2_raw_capture_enabled: bool = os.getenv('R2_RAW_CAPTURE_ENABLED', 'false').lower() == 'true'
    r2_endpoint: str = os.getenv('R2_ENDPOINT', '')
    r2_access_key_id: str = os.getenv('R2_ACCESS_KEY_ID', '')
    r2_secret_access_key: str = os.getenv('R2_SECRET_ACCESS_KEY', '')
    r2_bucket: str = os.getenv('R2_BUCKET', '')


settings = Settings()



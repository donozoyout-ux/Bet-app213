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


settings = Settings()

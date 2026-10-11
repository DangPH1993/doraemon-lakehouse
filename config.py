import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv('DATABASE_URL', '')
    admin_username: str = os.getenv('ADMIN_USERNAME', 'admin')
    admin_password: str = os.getenv('ADMIN_PASSWORD', '')
    session_secret: str = os.getenv('SESSION_SECRET', '')
    cors_origins: tuple[str, ...] = tuple(x.strip() for x in os.getenv('CORS_ORIGINS', 'http://localhost:8000').split(',') if x.strip())
    timezone: str = os.getenv('TIMEZONE', 'Asia/Ho_Chi_Minh')
    pipeline_lookback_days: int = int(os.getenv('PIPELINE_LOOKBACK_DAYS', '14'))
    max_feed_entries_per_source: int = int(os.getenv('MAX_FEED_ENTRIES_PER_SOURCE', '250'))
    request_timeout_seconds: int = int(os.getenv('REQUEST_TIMEOUT_SECONDS', '25'))

settings = Settings()

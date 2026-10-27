import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    # Use Neon pooled URL for ordinary query traffic; use direct URL for session-level advisory locks.
    database_url: str = os.getenv('DATABASE_URL', '')
    database_url_unpooled: str = os.getenv('DATABASE_URL_UNPOOLED', '')
    admin_username: str = os.getenv('ADMIN_USERNAME', 'admin')
    admin_password: str = os.getenv('ADMIN_PASSWORD', '')
    session_secret: str = os.getenv('SESSION_SECRET', '')
    cors_origins: tuple[str, ...] = tuple(x.strip() for x in os.getenv('CORS_ORIGINS', 'http://localhost:8000').split(',') if x.strip())
    timezone: str = os.getenv('TIMEZONE', 'Asia/Ho_Chi_Minh')
    pipeline_lookback_days: int = int(os.getenv('PIPELINE_LOOKBACK_DAYS', '14'))
    pipeline_lookback_hours: int = int(os.getenv('PIPELINE_LOOKBACK_HOURS', '24'))
    max_feed_entries_per_source: int = int(os.getenv('MAX_FEED_ENTRIES_PER_SOURCE', '250'))
    request_timeout_seconds: int = int(os.getenv('REQUEST_TIMEOUT_SECONDS', '25'))
    event_topic_backfill_batch_size: int = int(os.getenv('EVENT_TOPIC_BACKFILL_BATCH_SIZE', '250'))
    topic_discovery_article_limit: int = int(os.getenv('TOPIC_DISCOVERY_ARTICLE_LIMIT', '800'))
    openai_api_key: str = os.getenv('OPENAI_API_KEY', '')
    openai_model: str = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
    ai_classification_batch_size: int = int(os.getenv('AI_CLASSIFICATION_BATCH_SIZE', '12'))
    ai_classification_run_limit: int = int(os.getenv('AI_CLASSIFICATION_RUN_LIMIT', '48'))
    # Historical backlog processing is opt-in. Scheduled runs classify fresh articles only.
    ai_classification_backfill_enabled: bool = os.getenv('AI_CLASSIFICATION_BACKFILL_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
    # Failed articles are retried only after this cooldown unless explicitly queued for repair.
    ai_classification_retry_cooldown_hours: int = max(1, int(os.getenv('AI_CLASSIFICATION_RETRY_COOLDOWN_HOURS', '6')))
    ai_classification_max_retries: int = max(1, int(os.getenv('AI_CLASSIFICATION_MAX_RETRIES', '3')))
    ai_request_timeout_seconds: int = int(os.getenv('AI_REQUEST_TIMEOUT_SECONDS', '35'))
    # Telegram notifications: token is needed on both Render (webhook/link) and GitHub Actions (pipeline sender).
    telegram_bot_token: str = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    telegram_bot_username: str = os.getenv('TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
    telegram_webhook_secret: str = os.getenv('TELEGRAM_WEBHOOK_SECRET', '').strip()
    telegram_digest_max_articles: int = int(os.getenv('TELEGRAM_DIGEST_MAX_ARTICLES', '12'))
    # Subscription / payment configuration. The QR image must be publicly reachable via HTTPS.
    payment_qr_image_url: str = os.getenv('PAYMENT_QR_IMAGE_URL', '').strip()
    payment_instructions: str = os.getenv('PAYMENT_INSTRUCTIONS', 'Quét mã QR, hoàn tất thanh toán và tải ảnh chụp giao dịch để admin xác nhận.').strip()
    pro_plan_price_label: str = os.getenv('PRO_PLAN_PRICE_LABEL', '').strip()
    public_app_url: str = os.getenv('PUBLIC_APP_URL', '').strip().rstrip('/')
    # Brevo transactional email API uses HTTPS (port 443), suitable for Render Free.
    brevo_api_key: str = os.getenv('BREVO_API_KEY', '').strip()
    smtp_host: str = os.getenv('SMTP_HOST', '').strip()
    smtp_port: int = int(os.getenv('SMTP_PORT', '587'))
    smtp_username: str = os.getenv('SMTP_USERNAME', '').strip()
    smtp_password: str = os.getenv('SMTP_PASSWORD', '')
    smtp_from_email: str = os.getenv('SMTP_FROM_EMAIL', '').strip()
    smtp_starttls: bool = os.getenv('SMTP_STARTTLS', 'true').strip().lower() in {'1','true','yes','on'}
    # Cloudflare R2 S3-compatible storage. Keep the bucket private; the API streams images.
    cloudflare_r2_account_id: str = os.getenv('CLOUDFLARE_R2_ACCOUNT_ID', '').strip()
    cloudflare_r2_access_key_id: str = os.getenv('CLOUDFLARE_R2_ACCESS_KEY_ID', '').strip()
    cloudflare_r2_secret_access_key: str = os.getenv('CLOUDFLARE_R2_SECRET_ACCESS_KEY', '')
    cloudflare_r2_bucket_name: str = os.getenv('CLOUDFLARE_R2_BUCKET_NAME', '').strip()
    cloudflare_r2_key_prefix: str = os.getenv('CLOUDFLARE_R2_KEY_PREFIX', 'news-radar').strip('/')

settings = Settings()

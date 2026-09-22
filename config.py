from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "PokeMarket API"
    environment: str = "development"
    ai_provider: str = "stub"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_fallback_models: str = ""
    gemini_job_max_attempts: int = 3
    gemini_retry_base_seconds: int = 12
    cors_origins: str = "*"
    # Preserve high-quality camera uploads while bounding pathological requests.
    max_image_bytes: int = 12_000_000
    max_images: int = 9
    tcgdex_base_url: str = "https://api.tcgdex.net/v2"
    database_url: str | None = None
    database_pool_size: int = 3
    database_max_overflow: int = 2
    database_pool_timeout_seconds: int = 15
    auth_secret: str | None = None
    legacy_claim_code: str | None = None
    mailjet_api_key: str | None = None
    mailjet_secret_key: str | None = None
    email_from: str | None = None
    public_base_url: str = "https://pokemarket-4jwi.onrender.com"
    access_token_ttl_seconds: int = 2_592_000
    password_hash_iterations: int = 600_000
    r2_bucket_name: str | None = None
    r2_endpoint: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_presigned_url_expiry_seconds: int = 3600
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    marketplace_commission_percent: int = 4
    ebay_commission_percent: int = 1
    seller_hold_days: int = 10
    admin_emails: str = ""
    firebase_project_id: str | None = None
    firebase_service_account_json: str | None = None
    scan_worker_poll_seconds: float = 1.0
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @property
    def cors_origin_list(self):
        return ["*"] if self.cors_origins.strip() == "*" else [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def r2_configured(self):
        return all(
            (
                self.r2_bucket_name,
                self.r2_endpoint,
                self.r2_access_key_id,
                self.r2_secret_access_key,
            )
        )

    @property
    def auth_configured(self):
        return bool(self.auth_secret and len(self.auth_secret.strip()) >= 32)

    @property
    def email_configured(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.public_base_url)
        from email.utils import parseaddr
        sender = parseaddr(self.email_from or "")[1]
        return bool((self.mailjet_api_key or "").strip() and (self.mailjet_secret_key or "").strip() and "@" in sender and not any(c in (self.email_from or "") for c in "\r\n") and url.scheme == "https" and url.netloc and not url.username and not url.query and not url.fragment)

    @property
    def stripe_configured(self):
        return bool((self.stripe_secret_key or "").strip() and (self.stripe_publishable_key or "").strip())

    @property
    def admin_email_list(self):
        return {email.strip().lower() for email in self.admin_emails.split(",") if email.strip()}

    @property
    def firebase_configured(self):
        return bool((self.firebase_project_id or "").strip() and (self.firebase_service_account_json or "").strip())

@lru_cache
def get_settings():
    return Settings()

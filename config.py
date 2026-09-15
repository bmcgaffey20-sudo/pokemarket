from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "PokeMarket API"
    environment: str = "development"
    ai_provider: str = "stub"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    cors_origins: str = "*"
    # Preserve high-quality camera uploads while bounding pathological requests.
    max_image_bytes: int = 12_000_000
    max_images: int = 9
    tcgdex_base_url: str = "https://api.tcgdex.net/v2"
    database_url: str | None = None
    auth_secret: str | None = None
    legacy_claim_code: str | None = None
    access_token_ttl_seconds: int = 2_592_000
    password_hash_iterations: int = 600_000
    r2_bucket_name: str | None = None
    r2_endpoint: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_presigned_url_expiry_seconds: int = 3600
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

@lru_cache
def get_settings():
    return Settings()

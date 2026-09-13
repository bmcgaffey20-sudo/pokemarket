from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    app_name:str="PokeMarket API"
    environment:str="development"
    ai_provider:str="stub"
    gemini_api_key:str|None=None
    gemini_model:str="gemini-3.8-flash"
    cors_origins:str="*"
    max_image_bytes:int=12_000_000
    max_images:int=10
    tcgdex_base_url:str="https://api.tcgdex.net/v2"
    model_config=SettingsConfigDict(env_file=".env",extra="ignore",case_sensitive=False)
    @property
    def cors_origin_list(self):
        return ["*"] if self.cors_origins.strip()=="*" else [x.strip() for x in self.cors_origins.split(",") if x.strip()]
@lru_cache
def get_settings(): return Settings()

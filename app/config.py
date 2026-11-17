from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Konfigurasi dari environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str = Field(min_length=32)
    bi_base_url: str = "https://www.bi.go.id/hargapangan"
    bi_timeout_seconds: float = 90.0
    bi_price_type_id: int = 1
    max_range_days: int = 400
    min_forecast_points: int = 10
    max_horizon_days: int = 90
    commodity_cache_seconds: int = 86_400


@lru_cache
def get_settings() -> Settings:
    return Settings()

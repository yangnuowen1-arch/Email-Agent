"""Application settings loaded from environment variables or `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration shared by the agent and its integrations."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "email-agent"
    log_level: str = "INFO"
    openai_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings instance for the current process."""
    return Settings()

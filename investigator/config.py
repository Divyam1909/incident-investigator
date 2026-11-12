"""Runtime settings, read from environment variables and an optional .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./investigator.db"

    llm_provider: Literal["mock", "gemini"] = "mock"
    google_api_key: str = ""
    # Google retires model ids often; override with GEMINI_MODEL.
    gemini_model: str = "gemini-3.1-flash-lite"
    llm_temperature: float = 0.0

    # Hard ceiling on tool calls per investigation (the max-steps guard).
    max_steps: int = 12
    # How many times verify may send the agent back to fix unsupported claims.
    max_revisions: int = 1

    # USD per 1M tokens, used only for the cost estimate.
    llm_input_price_per_mtok: float = 0.30
    llm_output_price_per_mtok: float = 2.50


@lru_cache
def get_settings() -> Settings:
    return Settings()

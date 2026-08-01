"""Typed backend configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMMUTE_HELP_",
        extra="ignore",
    )

    environment: str = "development"
    database_path: Path = Path("data/app.db")
    graph_path: Path = Path("data/graphs/portland-vancouver.graphml")
    graph_manifest_path: Path = Path("data/graphs/graph-manifest.json")
    traffic_path: Path = Path("data/traffic")


@lru_cache
def get_settings() -> Settings:
    """Return one immutable configuration snapshot per process."""

    return Settings()

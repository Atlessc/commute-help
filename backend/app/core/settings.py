"""Typed backend configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
    closure_preset_path: Path = Path("data/presets/closure-presets.json")
    traffic_path: Path = Path("data/traffic")

    sumo_path: Path = Path("data/sumo")
    sumo_networks_path: Path = Path("data/sumo/networks")
    sumo_demand_path: Path = Path("data/sumo/demand")
    sumo_baselines_path: Path = Path("data/sumo/baselines")
    sumo_runs_path: Path = Path("data/sumo/runs")
    sumo_models_path: Path = Path("data/sumo/models")
    sumo_network_manifest_path: Path = Path("data/sumo/networks/active/network-manifest.json")
    sumo_active_model_manifest_path: Path = Path("data/sumo/models/active/model-manifest.json")

    sumo_binary: Path | None = None
    netconvert_binary: Path | None = None
    sumo_runtime_mode: Literal["auto", "libsumo", "subprocess"] = "auto"
    sumo_offline_only: bool = True
    sumo_max_parallel_runs: int = Field(default=1, ge=1, le=4)
    sumo_max_run_seconds: int = Field(default=3600, ge=60)
    sumo_max_run_disk_mb: int = Field(default=2048, ge=128)
    sumo_max_calibration_experiments: int = Field(default=100, ge=1)
    sumo_max_area_expansions: int = Field(default=3, ge=0, le=10)
    sumo_max_detailed_edges: int = Field(default=50_000, ge=100)
    sumo_max_coupling_iterations: int = Field(default=3, ge=1, le=10)
    sumo_default_warmup_minutes: int = Field(default=45, ge=0)
    sumo_default_analysis_minutes: int = Field(default=90, ge=1)
    sumo_default_ensemble_runs: int = Field(default=12, ge=1)
    sumo_max_ensemble_runs: int = Field(default=50, ge=1)
    sumo_micro_real_vehicles_per_sim_vehicle: float = Field(default=1.0, ge=1.0)
    sumo_playback_max_visible_vehicles: int = Field(default=900, ge=1)


@lru_cache
def get_settings() -> Settings:
    """Return one immutable configuration snapshot per process."""

    return Settings()

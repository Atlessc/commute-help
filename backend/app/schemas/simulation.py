"""Typed contracts for the local physical-simulation subsystem."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SimulationCapabilities(BaseModel):
    """Safe, path-free readiness information for the local SUMO runtime."""

    available: bool
    sumo_version: str | None = None
    runtime_mode: Literal["libsumo", "subprocess"] | None = None
    sumo_available: bool
    netconvert_available: bool
    sumolib_available: bool
    libsumo_available: bool
    network_ready: bool
    model_ready: bool
    active_run_id: str | None = None
    max_parallel_runs: int = Field(ge=1)
    offline_only: bool
    warnings: list[str] = Field(default_factory=list)


class SimulationRunRequest(BaseModel):
    """Phase-2 validation run; Portland scenario contracts arrive in later phases."""

    run_kind: Literal["validation"] = "validation"
    fixture: Literal["tiny_closure"] = "tiny_closure"
    seed: int = Field(default=842901, ge=0, le=2_147_483_647)
    step_delay_ms: int = Field(default=0, ge=0, le=250, exclude=True)


class SimulationRunCreated(BaseModel):
    id: UUID
    status: Literal["queued", "running"]


class SimulationRunStatus(BaseModel):
    id: UUID
    run_kind: str
    status: Literal[
        "queued", "running", "completed", "failed", "cancel_requested", "cancelled"
    ]
    seed: int
    progress: float = Field(ge=0, le=1)
    current_sim_second: float | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: dict | None = None
    error_message: str | None = None

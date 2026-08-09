"""Readiness API for the local physical-simulation subsystem."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.schemas.simulation import (
    SimulationCapabilities,
    SimulationRunCreated,
    SimulationRunRequest,
    SimulationRunStatus,
)
from backend.app.services.sumo.environment import SumoEnvironmentService
from backend.app.services.sumo.run_service import (
    SimulationRunConflictError,
    SimulationRunNotFoundError,
    SimulationRunService,
)

router = APIRouter(prefix="/simulation", tags=["simulation"])


def _environment(request: Request) -> SumoEnvironmentService:
    return request.app.state.sumo_environment_service


def _runs(request: Request) -> SimulationRunService:
    return request.app.state.simulation_run_service


@router.get("/status", response_model=SimulationCapabilities)
def simulation_status(request: Request) -> SimulationCapabilities:
    """Report runtime readiness without loading a network or starting SUMO."""

    capabilities = _environment(request).capabilities()
    capabilities.active_run_id = _runs(request).active_run_id()
    return capabilities


@router.post(
    "/runs", response_model=SimulationRunCreated, status_code=status.HTTP_202_ACCEPTED
)
def create_simulation_run(
    payload: SimulationRunRequest, request: Request
) -> SimulationRunCreated:
    try:
        return _runs(request).create(payload)
    except SimulationRunConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/runs/{run_id}", response_model=SimulationRunStatus)
def get_simulation_run(run_id: UUID, request: Request) -> SimulationRunStatus:
    try:
        return _runs(request).get(run_id)
    except SimulationRunNotFoundError as error:
        raise HTTPException(status_code=404, detail="Simulation run not found") from error


@router.post("/runs/{run_id}/cancel", response_model=SimulationRunStatus)
def cancel_simulation_run(run_id: UUID, request: Request) -> SimulationRunStatus:
    try:
        return _runs(request).cancel(run_id)
    except SimulationRunNotFoundError as error:
        raise HTTPException(status_code=404, detail="Simulation run not found") from error

"""System health and readiness endpoints."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.app.db.database import DatabaseManager

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Small response used by launch and proxy health checks."""

    status: Literal["ok"]
    application: str
    database: Literal["ok"]


class GraphStatus(BaseModel):
    """Graph readiness without implying that Phase 1 is complete."""

    status: Literal["not_configured"]
    version: None = None


class StatusResponse(BaseModel):
    """Current readiness of Phase 0 application services."""

    status: Literal["ok"]
    application: str
    version: str
    environment: str
    database: Literal["ok"]
    graph: GraphStatus


def _database(request: Request) -> DatabaseManager:
    return request.app.state.database


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Confirm that the API and its authoritative database are available."""

    database = _database(request)
    database.check_health()
    return HealthResponse(
        status="ok",
        application=request.app.title,
        database="ok",
    )


@router.get("/status", response_model=StatusResponse)
def status(request: Request) -> StatusResponse:
    """Report readiness while clearly identifying unfinished graph setup."""

    database = _database(request)
    database.check_health()
    settings = request.app.state.settings
    return StatusResponse(
        status="ok",
        application=request.app.title,
        version=request.app.version,
        environment=settings.environment,
        database="ok",
        graph=GraphStatus(status="not_configured"),
    )

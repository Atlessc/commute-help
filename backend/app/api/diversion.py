"""Process-local network diversion jobs backed by a persistent result cache."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.schemas.diversion import (
    DiversionJobResponse,
    DiversionRequest,
)
from backend.app.services.diversion_service import DiversionJobNotFoundError
from backend.app.services.graph_service import GraphUnavailableError
from backend.app.services.routing_service import InvalidClosureError

router = APIRouter(tags=["diversion"])


@router.post(
    "/diversions",
    response_model=DiversionJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_diversion(
    payload: DiversionRequest,
    request: Request,
) -> DiversionJobResponse:
    """Start a bounded background assignment or return its cached result."""

    try:
        return request.app.state.diversion_service.start(payload)
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error
    except InvalidClosureError as error:
        raise _api_error(409, "closure_edge_mismatch", str(error)) from error


@router.get("/diversions/{job_id}", response_model=DiversionJobResponse)
def get_diversion(job_id: UUID, request: Request) -> DiversionJobResponse:
    """Poll progress or retrieve the final small spillover result."""

    try:
        return request.app.state.diversion_service.get(job_id)
    except DiversionJobNotFoundError as error:
        raise _api_error(404, "diversion_job_not_found", str(error)) from error


@router.delete("/diversions/{job_id}", response_model=DiversionJobResponse)
def cancel_diversion(job_id: UUID, request: Request) -> DiversionJobResponse:
    """Cooperatively cancel an unfinished local assignment job."""

    try:
        return request.app.state.diversion_service.cancel(job_id)
    except DiversionJobNotFoundError as error:
        raise _api_error(404, "diversion_job_not_found", str(error)) from error


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )

"""Local traffic import and reliability simulation endpoints."""

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from backend.app.schemas.traffic import (
    ReliabilityRequest,
    ReliabilityResponse,
    TrafficImportResponse,
    TrafficProfilesResponse,
)
from backend.app.services.graph_service import GraphUnavailableError
from backend.app.services.traffic_service import (
    MAX_IMPORT_BYTES,
    TrafficImportError,
    TrafficProfileMismatchError,
    TrafficProfileNotFoundError,
)

router = APIRouter(tags=["traffic"])


@router.get("/traffic/profiles", response_model=TrafficProfilesResponse)
def traffic_profiles(request: Request) -> TrafficProfilesResponse:
    """List locally imported, versioned calibration profiles."""

    return request.app.state.traffic_service.list_profiles()


@router.post("/traffic/import", response_model=TrafficImportResponse)
async def import_traffic(
    request: Request,
    file: UploadFile = File(...),
    source_name: str = Form(...),
) -> TrafficImportResponse:
    """Preserve and normalize a bounded local CSV or Parquet upload."""

    contents = await file.read(MAX_IMPORT_BYTES + 1)
    try:
        return await run_in_threadpool(
            request.app.state.traffic_service.import_observations,
            filename=file.filename or "traffic-observations.csv",
            source_name=source_name,
            contents=contents,
        )
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error
    except TrafficImportError as error:
        raise _api_error(422, "invalid_traffic_import", str(error)) from error


@router.post("/simulations", response_model=ReliabilityResponse)
def simulate_reliability(
    payload: ReliabilityRequest,
    request: Request,
) -> ReliabilityResponse:
    """Sample a route using a versioned historical profile or honest fallback."""

    try:
        return request.app.state.traffic_service.simulate(payload)
    except TrafficProfileNotFoundError as error:
        raise _api_error(404, "traffic_profile_not_found", str(error)) from error
    except TrafficProfileMismatchError as error:
        raise _api_error(409, "traffic_profile_graph_mismatch", str(error)) from error
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )

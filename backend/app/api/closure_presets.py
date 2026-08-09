"""Read-only endpoints for curated closure plans."""

from fastapi import APIRouter, HTTPException, Request

from backend.app.schemas.closure_presets import (
    ClosurePresetListResponse,
    ClosurePresetRecord,
)
from backend.app.services.closure_preset_service import (
    ClosurePresetCatalogError,
    ClosurePresetNotFoundError,
)

router = APIRouter(prefix="/closure-presets", tags=["closure presets"])


@router.get("", response_model=ClosurePresetListResponse)
def list_closure_presets(request: Request) -> ClosurePresetListResponse:
    try:
        return ClosurePresetListResponse(
            presets=request.app.state.closure_preset_service.list()
        )
    except ClosurePresetCatalogError as error:
        raise _api_error(503, "closure_presets_unavailable", str(error)) from error


@router.get("/{preset_id}", response_model=ClosurePresetRecord)
def get_closure_preset(preset_id: str, request: Request) -> ClosurePresetRecord:
    try:
        return request.app.state.closure_preset_service.get(preset_id)
    except ClosurePresetNotFoundError as error:
        raise _api_error(404, "closure_preset_not_found", "Closure preset not found.") from error
    except ClosurePresetCatalogError as error:
        raise _api_error(503, "closure_presets_unavailable", str(error)) from error


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )

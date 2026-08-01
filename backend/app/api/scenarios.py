"""Shared scenario CRUD, revision, and import/export endpoints."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.app.schemas.scenarios import (
    ScenarioCreateRequest,
    ScenarioDuplicateRequest,
    ScenarioImportRequest,
    ScenarioListResponse,
    ScenarioRecord,
    ScenarioUpdateRequest,
)
from backend.app.services.scenario_service import (
    InvalidScenarioNameError,
    ScenarioNotFoundError,
    ScenarioRevisionConflictError,
)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("", response_model=ScenarioListResponse)
def list_scenarios(
    request: Request,
    include_archived: bool = Query(default=False),
) -> ScenarioListResponse:
    return ScenarioListResponse(
        scenarios=request.app.state.scenario_service.list(include_archived)
    )


@router.post("", response_model=ScenarioRecord, status_code=201)
def create_scenario(
    payload: ScenarioCreateRequest,
    request: Request,
) -> ScenarioRecord:
    try:
        return request.app.state.scenario_service.create(payload)
    except InvalidScenarioNameError as error:
        raise _api_error(400, "invalid_scenario_name", "Enter a scenario name.") from error


@router.post("/import", response_model=ScenarioRecord, status_code=201)
def import_scenario(
    payload: ScenarioImportRequest,
    request: Request,
) -> ScenarioRecord:
    try:
        return request.app.state.scenario_service.import_scenario(payload)
    except InvalidScenarioNameError as error:
        raise _api_error(400, "invalid_scenario_name", "Enter a scenario name.") from error


@router.get("/{scenario_id}", response_model=ScenarioRecord)
def get_scenario(scenario_id: UUID, request: Request) -> ScenarioRecord:
    try:
        return request.app.state.scenario_service.get(scenario_id)
    except ScenarioNotFoundError as error:
        raise _api_error(404, "scenario_not_found", "Scenario not found.") from error


@router.put("/{scenario_id}", response_model=ScenarioRecord)
def update_scenario(
    scenario_id: UUID,
    payload: ScenarioUpdateRequest,
    request: Request,
) -> ScenarioRecord:
    try:
        return request.app.state.scenario_service.update(scenario_id, payload)
    except ScenarioNotFoundError as error:
        raise _api_error(404, "scenario_not_found", "Scenario not found.") from error
    except ScenarioRevisionConflictError as error:
        raise _api_error(
            409,
            "scenario_revision_conflict",
            f"This scenario is now revision {error.actual_revision}. Reload it before saving.",
        ) from error
    except InvalidScenarioNameError as error:
        raise _api_error(400, "invalid_scenario_name", "Enter a scenario name.") from error


@router.post("/{scenario_id}/duplicate", response_model=ScenarioRecord, status_code=201)
def duplicate_scenario(
    scenario_id: UUID,
    payload: ScenarioDuplicateRequest,
    request: Request,
) -> ScenarioRecord:
    try:
        return request.app.state.scenario_service.duplicate(scenario_id, payload)
    except ScenarioNotFoundError as error:
        raise _api_error(404, "scenario_not_found", "Scenario not found.") from error
    except InvalidScenarioNameError as error:
        raise _api_error(400, "invalid_scenario_name", "Enter a scenario name.") from error


@router.delete("/{scenario_id}", status_code=204)
def archive_scenario(scenario_id: UUID, request: Request) -> Response:
    try:
        request.app.state.scenario_service.archive(scenario_id)
    except ScenarioNotFoundError as error:
        raise _api_error(404, "scenario_not_found", "Scenario not found.") from error
    return Response(status_code=204)


@router.get("/{scenario_id}/export", response_model=ScenarioRecord)
def export_scenario(
    scenario_id: UUID,
    request: Request,
    response: Response,
) -> ScenarioRecord:
    try:
        scenario = request.app.state.scenario_service.get(scenario_id)
    except ScenarioNotFoundError as error:
        raise _api_error(404, "scenario_not_found", "Scenario not found.") from error
    response.headers["Content-Disposition"] = (
        f'attachment; filename="commute-help-{scenario.id}.json"'
    )
    return scenario


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )

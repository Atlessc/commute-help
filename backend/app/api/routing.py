"""Interactive road selection and baseline route endpoints."""

from fastapi import APIRouter, HTTPException, Request

from backend.app.schemas.routing import (
    ClosureRoadSelectionResponse,
    GoogleMapsUrlRequest,
    GoogleMapsUrlResponse,
    RoadSelectionRequest,
    RoadSelectionResponse,
    RouteAlternativesResponse,
    RouteCompareRequest,
    RouteCompareResponse,
)
from backend.app.services.graph_service import GraphUnavailableError
from backend.app.services.routing_service import (
    InvalidClosureError,
    NoRouteError,
    SameLocationError,
)
from backend.app.services.spatial_service import InvalidGraphNodeError, NoNearbyRoadError

router = APIRouter(tags=["routing"])


@router.post("/roads/select", response_model=RoadSelectionResponse)
def select_road(payload: RoadSelectionRequest, request: Request) -> RoadSelectionResponse:
    """Snap one map click to the local graph without exposing the graph itself."""

    try:
        location = request.app.state.spatial_service.select_road(
            payload.lat,
            payload.lng,
        )
        manifest = request.app.state.graph_service.require_manifest()
        return RoadSelectionResponse(
            graph_version=manifest.graph_version,
            location=location,
        )
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error
    except NoNearbyRoadError as error:
        raise _api_error(404, "no_nearby_road", str(error)) from error


@router.post("/roads/select-closure", response_model=ClosureRoadSelectionResponse)
def select_closure_road(
    payload: RoadSelectionRequest,
    request: Request,
) -> ClosureRoadSelectionResponse:
    """Resolve a map click to independently selectable road directions."""

    try:
        return request.app.state.spatial_service.select_closure_road(
            payload.lat,
            payload.lng,
        )
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error
    except NoNearbyRoadError as error:
        raise _api_error(404, "no_nearby_road", str(error)) from error


@router.post("/routes/compare", response_model=RouteCompareResponse)
def compare_routes(
    payload: RouteCompareRequest,
    request: Request,
) -> RouteCompareResponse:
    """Calculate the free-flow baseline and active road-impact comparison."""

    try:
        return request.app.state.routing_service.compare(payload)
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error
    except NoNearbyRoadError as error:
        raise _api_error(404, "no_nearby_road", str(error)) from error
    except InvalidGraphNodeError as error:
        raise _api_error(409, "graph_node_mismatch", str(error)) from error
    except InvalidClosureError as error:
        raise _api_error(409, "closure_edge_mismatch", str(error)) from error
    except SameLocationError as error:
        raise _api_error(400, "same_location", "Choose two different trip points.") from error
    except NoRouteError as error:
        raise _api_error(
            404,
            "no_legal_route",
            "No legal directed route connects those points.",
        ) from error


@router.post("/routes/alternatives", response_model=RouteAlternativesResponse)
def route_alternatives(
    payload: RouteCompareRequest,
    request: Request,
) -> RouteAlternativesResponse:
    """Return distinct closure-aware corridors and transparent rankings."""

    try:
        return request.app.state.routing_service.alternatives(payload)
    except GraphUnavailableError as error:
        raise _api_error(503, "graph_unavailable", str(error)) from error
    except NoNearbyRoadError as error:
        raise _api_error(404, "no_nearby_road", str(error)) from error
    except InvalidGraphNodeError as error:
        raise _api_error(409, "graph_node_mismatch", str(error)) from error
    except InvalidClosureError as error:
        raise _api_error(409, "closure_edge_mismatch", str(error)) from error
    except SameLocationError as error:
        raise _api_error(400, "same_location", "Choose two different trip points.") from error
    except NoRouteError as error:
        raise _api_error(
            404,
            "no_legal_route",
            "No legal directed route connects those points under the active restrictions.",
        ) from error


@router.post("/routes/google-maps-url", response_model=GoogleMapsUrlResponse)
def google_maps_url(
    payload: GoogleMapsUrlRequest,
    request: Request,
) -> GoogleMapsUrlResponse:
    """Create a standard Google Maps URL without an API key."""

    return request.app.state.routing_service.google_maps_url(payload)


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )

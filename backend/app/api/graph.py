"""Routing graph metadata endpoints."""

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.schemas.graph import GraphManifest
from backend.app.services.graph_service import GraphService, GraphUnavailableError

router = APIRouter(prefix="/graph", tags=["graph"])


def _graph_service(request: Request) -> GraphService:
    return request.app.state.graph_service


@router.get("/manifest", response_model=GraphManifest)
def graph_manifest(request: Request) -> GraphManifest:
    """Return the verified build manifest without local filesystem paths."""

    try:
        return _graph_service(request).require_manifest()
    except GraphUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "graph_unavailable",
                "message": str(error) or "Routing graph has not been built yet.",
            },
        ) from error

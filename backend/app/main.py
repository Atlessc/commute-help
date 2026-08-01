"""FastAPI application factory for Commute Help."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.diversion import router as diversion_router
from backend.app.api.graph import router as graph_router
from backend.app.api.routing import router as routing_router
from backend.app.api.scenarios import router as scenarios_router
from backend.app.api.system import router as system_router
from backend.app.api.traffic import router as traffic_router
from backend.app.core.settings import Settings, get_settings
from backend.app.db.database import DatabaseManager
from backend.app.services.graph_service import GraphService
from backend.app.services.diversion_service import DiversionService
from backend.app.services.routing_service import RoutingService
from backend.app.services.scenario_service import ScenarioService
from backend.app.services.spatial_service import SpatialService
from backend.app.services.traffic_service import TrafficService
from backend.app.services.portal_service import PortalService


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with injectable settings for isolated tests."""

    active_settings = settings or get_settings()
    database = DatabaseManager(active_settings.database_path)
    graph_service = GraphService(
        active_settings.graph_path,
        active_settings.graph_manifest_path,
    )
    spatial_service = SpatialService(graph_service)
    routing_service = RoutingService(graph_service, spatial_service)
    scenario_service = ScenarioService(database, graph_service, spatial_service)
    traffic_service = TrafficService(
        database,
        graph_service,
        active_settings.traffic_path,
    )
    portal_service = PortalService(graph_service, traffic_service)
    diversion_service = DiversionService(
        database,
        graph_service,
        spatial_service,
        routing_service,
        traffic_service,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        graph_service.load()
        application.state.database = database
        application.state.graph_service = graph_service
        application.state.spatial_service = spatial_service
        application.state.routing_service = routing_service
        application.state.scenario_service = scenario_service
        application.state.traffic_service = traffic_service
        application.state.portal_service = portal_service
        application.state.diversion_service = diversion_service
        application.state.settings = active_settings
        try:
            yield
        finally:
            diversion_service.shutdown()
            portal_service.close()

    application = FastAPI(
        title="Commute Help API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(system_router, prefix="/api")
    application.include_router(graph_router, prefix="/api")
    application.include_router(routing_router, prefix="/api")
    application.include_router(scenarios_router, prefix="/api")
    application.include_router(traffic_router, prefix="/api")
    application.include_router(diversion_router, prefix="/api")
    return application


app = create_app()

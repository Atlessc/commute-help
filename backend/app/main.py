"""FastAPI application factory for Commute Help."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.diversion import router as diversion_router
from backend.app.api.closure_presets import router as closure_presets_router
from backend.app.api.graph import router as graph_router
from backend.app.api.routing import router as routing_router
from backend.app.api.scenarios import router as scenarios_router
from backend.app.api.simulation import router as simulation_router
from backend.app.api.system import router as system_router
from backend.app.api.traffic import router as traffic_router
from backend.app.core.settings import Settings, get_settings
from backend.app.db.database import DatabaseManager
from backend.app.services.graph_service import GraphService
from backend.app.services.closure_preset_service import ClosurePresetService
from backend.app.services.diversion_service import DiversionService
from backend.app.services.routing_service import RoutingService
from backend.app.services.scenario_service import ScenarioService
from backend.app.services.spatial_service import SpatialService
from backend.app.services.sumo.environment import SumoEnvironmentService
from backend.app.services.sumo.run_service import SimulationRunService
from backend.app.services.traffic_service import TrafficService


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
    closure_preset_service = ClosurePresetService(
        active_settings.closure_preset_path,
        graph_service,
    )
    traffic_service = TrafficService(
        database,
        graph_service,
        active_settings.traffic_path,
    )
    diversion_service = DiversionService(
        database,
        graph_service,
        spatial_service,
        routing_service,
        traffic_service,
    )
    sumo_environment_service = SumoEnvironmentService(active_settings)
    simulation_run_service = SimulationRunService(database, active_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        graph_service.load()
        application.state.database = database
        application.state.graph_service = graph_service
        application.state.spatial_service = spatial_service
        application.state.routing_service = routing_service
        application.state.scenario_service = scenario_service
        application.state.closure_preset_service = closure_preset_service
        application.state.traffic_service = traffic_service
        application.state.diversion_service = diversion_service
        application.state.sumo_environment_service = sumo_environment_service
        application.state.simulation_run_service = simulation_run_service
        application.state.settings = active_settings
        try:
            yield
        finally:
            simulation_run_service.shutdown()
            diversion_service.shutdown()

    application = FastAPI(
        title="Commute Help API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(system_router, prefix="/api")
    application.include_router(graph_router, prefix="/api")
    application.include_router(routing_router, prefix="/api")
    application.include_router(scenarios_router, prefix="/api")
    application.include_router(closure_presets_router, prefix="/api")
    application.include_router(traffic_router, prefix="/api")
    application.include_router(diversion_router, prefix="/api")
    application.include_router(simulation_router, prefix="/api")
    return application


app = create_app()

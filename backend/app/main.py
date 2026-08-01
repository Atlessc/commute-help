"""FastAPI application factory for Commute Help."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.graph import router as graph_router
from backend.app.api.system import router as system_router
from backend.app.core.settings import Settings, get_settings
from backend.app.db.database import DatabaseManager
from backend.app.services.graph_service import GraphService


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with injectable settings for isolated tests."""

    active_settings = settings or get_settings()
    database = DatabaseManager(active_settings.database_path)
    graph_service = GraphService(
        active_settings.graph_path,
        active_settings.graph_manifest_path,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        graph_service.load()
        application.state.database = database
        application.state.graph_service = graph_service
        application.state.settings = active_settings
        yield

    application = FastAPI(
        title="Commute Help API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(system_router, prefix="/api")
    application.include_router(graph_router, prefix="/api")
    return application


app = create_app()

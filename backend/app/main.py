"""FastAPI application factory for Commute Help."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.system import router as system_router
from backend.app.core.settings import Settings, get_settings
from backend.app.db.database import DatabaseManager


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with injectable settings for isolated tests."""

    active_settings = settings or get_settings()
    database = DatabaseManager(active_settings.database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        application.state.database = database
        application.state.settings = active_settings
        yield

    application = FastAPI(
        title="Commute Help API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(system_router, prefix="/api")
    return application


app = create_app()

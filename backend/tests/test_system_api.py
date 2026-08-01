"""Phase 0 API and SQLite startup tests."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.core.settings import Settings
from backend.app.main import create_app


def _client(database_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=database_path,
    )
    return TestClient(create_app(settings))


def test_health_reports_api_and_database_ready(tmp_path: Path) -> None:
    database_path = tmp_path / "commute-help.db"

    with _client(database_path) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "application": "Commute Help API",
        "database": "ok",
    }
    assert database_path.exists()


def test_status_discloses_that_the_graph_is_not_configured(tmp_path: Path) -> None:
    with _client(tmp_path / "commute-help.db") as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["environment"] == "test"
    assert response.json()["graph"] == {
        "status": "not_configured",
        "version": None,
    }


def test_database_uses_wal_and_initializes_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "commute-help.db"

    with _client(database_path):
        pass

    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        schema_version = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert journal_mode == ("wal",)
    assert schema_version == ("0",)

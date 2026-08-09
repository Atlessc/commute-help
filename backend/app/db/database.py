"""SQLite initialization and connectivity checks."""

import sqlite3
from pathlib import Path


class DatabaseManager:
    """Own short SQLite connections to the authoritative local database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        """Open a configured connection suitable for short transactions."""

        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        """Create the database directory and current application schema."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                INSERT INTO app_metadata (key, value)
                VALUES ('schema_version', '2')
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenarios (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    graph_version TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_scenarios_updated_at
                ON scenarios (updated_at DESC);

                CREATE TABLE IF NOT EXISTS scenario_revisions (
                    scenario_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    name TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    editor_name TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scenario_id, revision),
                    FOREIGN KEY (scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS scenario_imports (
                    id TEXT PRIMARY KEY,
                    imported_scenario_id TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    FOREIGN KEY (imported_scenario_id) REFERENCES scenarios(id)
                );

                CREATE TABLE IF NOT EXISTS traffic_imports (
                    id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    raw_path TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    normalized_path TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    accepted_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    quality_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS traffic_profiles (
                    id TEXT PRIMARY KEY,
                    import_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    source_window TEXT NOT NULL,
                    period TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    stats_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (import_id) REFERENCES traffic_imports(id)
                );

                CREATE INDEX IF NOT EXISTS idx_traffic_profiles_period
                ON traffic_profiles (period, created_at DESC);

                CREATE TABLE IF NOT EXISTS diversion_cache (
                    cache_key TEXT PRIMARY KEY,
                    graph_version TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_diversion_cache_created_at
                ON diversion_cache (created_at DESC);

                CREATE TABLE IF NOT EXISTS simulation_runs (
                    id TEXT PRIMARY KEY,
                    run_kind TEXT NOT NULL CHECK (run_kind IN ('validation')),
                    status TEXT NOT NULL CHECK (status IN (
                        'queued', 'running', 'completed', 'failed',
                        'cancel_requested', 'cancelled'
                    )),
                    run_key TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    graph_version TEXT NOT NULL,
                    sumo_version TEXT NOT NULL,
                    sumo_network_version TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    summary_json TEXT,
                    artifact_dir TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0.0,
                    current_sim_second REAL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_simulation_runs_status
                ON simulation_runs(status, created_at DESC);
                """
            )

    def check_health(self) -> None:
        """Raise a safe server-side error when SQLite is unavailable."""

        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

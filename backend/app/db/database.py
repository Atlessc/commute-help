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
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        """Create the database directory and Phase 0 metadata table."""

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
                VALUES ('schema_version', '0')
                ON CONFLICT(key) DO NOTHING
                """
            )

    def check_health(self) -> None:
        """Raise a safe server-side error when SQLite is unavailable."""

        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

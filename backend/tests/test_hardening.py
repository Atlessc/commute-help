"""Phase 8 diagnostics and backup recovery acceptance fixtures."""

import json
import sqlite3
from pathlib import Path

import pytest

from backend.app.core.settings import Settings
from scripts import backup
from scripts.backup import BackupVerificationError
from scripts.doctor import run_checks


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "data" / "app.db",
        graph_path=tmp_path / "data" / "graphs" / "fixture.graphml",
        graph_manifest_path=tmp_path / "data" / "graphs" / "graph-manifest.json",
        traffic_path=tmp_path / "data" / "traffic",
    )


def test_backup_round_trip_detects_tampering(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.database_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("CREATE TABLE fixture (value TEXT NOT NULL)")
        connection.execute("INSERT INTO fixture VALUES ('preserved')")
    settings.graph_manifest_path.parent.mkdir(parents=True)
    settings.graph_manifest_path.write_text('{"graph_version":"fixture"}\n')
    monkeypatch.setattr(backup, "get_settings", lambda: settings)

    backup_dir = backup.create_backup(tmp_path / "backups")
    manifest = backup.verify_backup(backup_dir)

    assert set(manifest["files"]) == {"app.db", "graph-manifest.json"}
    with sqlite3.connect(backup_dir / "app.db") as connection:
        assert connection.execute("SELECT value FROM fixture").fetchone() == ("preserved",)

    (backup_dir / "graph-manifest.json").write_text("tampered")
    with pytest.raises(BackupVerificationError, match="mismatch"):
        backup.verify_backup(backup_dir)


def test_backup_verification_rejects_paths_outside_backup(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "backup-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": backup.BACKUP_SCHEMA_VERSION,
                "files": {"../outside.db": {"size_bytes": 0, "sha256": ""}},
            }
        )
    )

    with pytest.raises(BackupVerificationError, match="unsafe artifact path"):
        backup.verify_backup(backup_dir)


def test_doctor_reports_actionable_missing_artifacts(tmp_path: Path) -> None:
    checks = run_checks(_settings(tmp_path), root=tmp_path)
    by_label = {check.label: check for check in checks}

    assert by_label["Python environment"].status == "fail"
    assert "npm run setup" in by_label["Python environment"].detail
    assert by_label["SQLite database"].status == "warn"
    assert by_label["Road graph"].status == "fail"
    assert "npm run graph:build" in by_label["Road graph"].detail

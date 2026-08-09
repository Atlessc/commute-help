"""Read-only local readiness checks with actionable recovery guidance."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.app.core.settings import Settings, get_settings
from backend.app.schemas.graph import GraphManifest
from backend.app.services.sumo.environment import SumoEnvironmentService


@dataclass(frozen=True)
class Check:
    label: str
    status: str
    detail: str


def run_checks(settings: Settings, root: Path = Path(".")) -> list[Check]:
    """Inspect dependencies and local artifacts without changing them."""

    checks = [
        _path_check(root / ".venv/bin/python", "Python environment", executable=True),
        _path_check(
            root / "node_modules/.bin/concurrently",
            "Root Node dependencies",
            executable=True,
        ),
        _path_check(root / "frontend/node_modules", "Frontend dependencies"),
        _database_check(settings.database_path),
        _graph_check(settings.graph_path, settings.graph_manifest_path),
        _sumo_check(settings),
    ]
    return checks


def main() -> None:
    checks = run_checks(get_settings())
    print("\nCommute Help doctor\n")
    for check in checks:
        print(f"[{check.status.upper():4}] {check.label}: {check.detail}")
    failures = [check for check in checks if check.status == "fail"]
    if failures:
        print("\nOne or more required checks failed. Follow the guidance above, then rerun `npm run doctor`.")
        raise SystemExit(1)
    print("\nReady. Start Commute Help with `npm run dev` or double-click `Commute Help.command`.")


def _path_check(path: Path, label: str, *, executable: bool = False) -> Check:
    if not path.exists():
        return Check(label, "fail", "missing; run `npm run setup`")
    if executable and not path.stat().st_mode & 0o111:
        return Check(label, "fail", "not executable; rerun `npm run setup`")
    return Check(label, "ok", "available")


def _database_check(path: Path) -> Check:
    if not path.exists():
        return Check(
            "SQLite database",
            "warn",
            f"{path} does not exist yet; it will be created at first startup",
        )
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            return Check("SQLite database", "fail", "integrity check failed; restore a verified backup")
    except sqlite3.Error:
        return Check("SQLite database", "fail", "could not be read; restore a verified backup")
    return Check("SQLite database", "ok", f"{path} passed SQLite quick_check")


def _graph_check(graph_path: Path, manifest_path: Path) -> Check:
    if not graph_path.exists() and not manifest_path.exists():
        return Check("Road graph", "fail", "missing; run `npm run graph:build`")
    if not graph_path.exists() or not manifest_path.exists():
        return Check("Road graph", "fail", "artifacts are incomplete; rerun `npm run graph:build -- --force`")
    try:
        manifest = GraphManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        artifact = manifest.artifacts.get("graphml")
        if not manifest.validation_passed or artifact is None:
            raise ValueError
        if graph_path.stat().st_size != artifact.size_bytes:
            return Check("Road graph", "fail", "size differs from the manifest; run `npm run graph:validate`")
        if _sha256(graph_path) != artifact.sha256:
            return Check("Road graph", "fail", "checksum differs from the manifest; rebuild the graph")
    except (OSError, ValueError, json.JSONDecodeError):
        return Check("Road graph", "fail", "manifest is invalid; run `npm run graph:validate`")
    return Check("Road graph", "ok", f"{manifest.graph_version} passed checksum verification")


def _sumo_check(settings: Settings) -> Check:
    capabilities = SumoEnvironmentService(settings).capabilities()
    if not capabilities.available or not capabilities.netconvert_available:
        return Check("SUMO runtime", "fail", "missing or incomplete; run `npm run setup`")
    model = "model ready" if capabilities.model_ready else "no frozen model bundle yet"
    return Check(
        "SUMO runtime",
        "ok",
        f"{capabilities.sumo_version or 'unknown version'} via "
        f"{capabilities.runtime_mode}; {model}",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()

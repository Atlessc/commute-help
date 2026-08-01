"""Create and verify recoverable local backups without modifying application data."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.core.settings import get_settings

PACIFIC = ZoneInfo("America/Los_Angeles")
BACKUP_SCHEMA_VERSION = 1


class BackupVerificationError(ValueError):
    """A backup is incomplete, corrupt, or does not match its manifest."""


def create_backup(backup_root: Path = Path("data/backups")) -> Path:
    """Create a consistent SQLite backup and checksum every copied artifact."""

    settings = get_settings()
    timestamp = datetime.now(PACIFIC).strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)

    if settings.database_path.exists():
        destination = backup_dir / "app.db"
        with sqlite3.connect(settings.database_path) as source:
            with sqlite3.connect(destination) as target:
                source.backup(target)

    if settings.graph_manifest_path.exists():
        shutil.copy2(
            settings.graph_manifest_path,
            backup_dir / settings.graph_manifest_path.name,
        )

    traffic_manifest = settings.traffic_path / "traffic-profile-manifest.json"
    if traffic_manifest.exists():
        shutil.copy2(traffic_manifest, backup_dir / traffic_manifest.name)

    exports_dir = Path("data/exports")
    if exports_dir.exists():
        shutil.copytree(exports_dir, backup_dir / "exports")

    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now(PACIFIC).isoformat(),
        "sources": {
            "database": str(settings.database_path),
            "graph_manifest": str(settings.graph_manifest_path),
            "traffic_profile_manifest": str(traffic_manifest),
        },
        "files": {
            str(path.relative_to(backup_dir)): _file_record(path)
            for path in sorted(backup_dir.rglob("*"))
            if path.is_file()
        },
    }
    (backup_dir / "backup-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    verify_backup(backup_dir)
    return backup_dir


def verify_backup(backup_dir: Path) -> dict:
    """Validate manifest checksums and SQLite consistency for one backup."""

    manifest_path = backup_dir / "backup-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupVerificationError("backup-manifest.json is missing or invalid") from error
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupVerificationError("unsupported backup manifest schema")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise BackupVerificationError("backup manifest does not list any artifacts")
    for relative_name, expected in files.items():
        if not isinstance(relative_name, str) or not isinstance(expected, dict):
            raise BackupVerificationError("backup manifest contains an invalid artifact record")
        relative_path = Path(relative_name)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise BackupVerificationError("backup manifest contains an unsafe artifact path")
        path = backup_dir / relative_path
        if not path.is_file():
            raise BackupVerificationError(f"missing backup artifact: {relative_name}")
        if path.stat().st_size != expected.get("size_bytes"):
            raise BackupVerificationError(f"size mismatch: {relative_name}")
        if _sha256(path) != expected.get("sha256"):
            raise BackupVerificationError(f"checksum mismatch: {relative_name}")
    database_path = backup_dir / "app.db"
    if database_path.exists():
        try:
            with sqlite3.connect(database_path) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as error:
            raise BackupVerificationError("backup database could not be opened") from error
        if result != ("ok",):
            raise BackupVerificationError("backup database failed SQLite quick_check")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="BACKUP_DIR",
        help="verify an existing backup instead of creating one",
    )
    args = parser.parse_args()
    try:
        if args.verify:
            manifest = verify_backup(args.verify)
            print(
                f"Backup verified: {args.verify.resolve()} "
                f"({len(manifest['files'])} artifact(s))"
            )
            return
        backup_dir = create_backup()
        print(f"Backup created and verified: {backup_dir.resolve()}")
    except BackupVerificationError as error:
        parser.error(str(error))


def _file_record(path: Path) -> dict[str, int | str]:
    return {"size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()

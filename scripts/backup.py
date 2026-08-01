"""Create a recoverable local backup without modifying application data."""

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.core.settings import get_settings

PACIFIC = ZoneInfo("America/Los_Angeles")


def main() -> None:
    settings = get_settings()
    timestamp = datetime.now(PACIFIC).strftime("%Y%m%d-%H%M%S")
    backup_dir = Path("data/backups") / timestamp
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
        "created_at": datetime.now(PACIFIC).isoformat(),
        "database": str(settings.database_path),
        "graph_manifest": str(settings.graph_manifest_path),
        "traffic_profile_manifest": str(traffic_manifest),
    }
    (backup_dir / "backup-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Backup created: {backup_dir.resolve()}")


if __name__ == "__main__":
    main()

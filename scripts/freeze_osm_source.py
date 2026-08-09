"""Freeze complete OSMnx Overpass cache responses into one reproducible OSM XML source."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, TextIO
from xml.sax.saxutils import quoteattr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge verified local OSMnx cache responses into a frozen OSM XML source."
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-version", required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.output_dir / "freeze.log"
    started_at = datetime.now(UTC)
    started_clock = monotonic()

    def log(message: str) -> None:
        elapsed = monotonic() - started_clock
        line = f"[{datetime.now(UTC).isoformat()}] [+{elapsed:0.3f}s] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    cache_files = sorted(args.cache_dir.glob("*.json"))
    if not cache_files:
        parser.error(f"No JSON cache responses found in {args.cache_dir}")

    output_path = args.output_dir / "portland-vancouver.osm.xml"
    manifest_path = args.output_dir / "source-manifest.json"
    if output_path.exists() or manifest_path.exists():
        parser.error("Output already exists; use a new source version instead of overwriting it")

    log(f"START source={args.source_version} cache_files={len(cache_files)}")
    with tempfile.TemporaryDirectory(prefix="osm-freeze-", dir=args.output_dir.parent) as work:
        database_path = Path(work) / "elements.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE elements (
                    element_type TEXT NOT NULL,
                    element_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (element_type, element_id)
                ) WITHOUT ROWID
                """
            )
            source_records: list[dict[str, Any]] = []
            duplicate_count = 0
            timestamps: set[str] = set()
            copyright_values: set[str] = set()

            for index, cache_path in enumerate(cache_files, start=1):
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                if payload.get("remark"):
                    raise ValueError(f"{cache_path.name} contains an Overpass error remark")
                elements = payload.get("elements")
                if not isinstance(elements, list):
                    raise ValueError(f"{cache_path.name} does not contain an elements list")
                osm3s = payload.get("osm3s", {})
                timestamp = str(osm3s.get("timestamp_osm_base", ""))
                copyright_text = str(osm3s.get("copyright", ""))
                if timestamp:
                    timestamps.add(timestamp)
                if copyright_text:
                    copyright_values.add(copyright_text)

                for element in elements:
                    element_type = str(element.get("type"))
                    element_id = element.get("id")
                    if element_type not in {"node", "way", "relation"} or not isinstance(
                        element_id,
                        int,
                    ):
                        raise ValueError(f"{cache_path.name} contains an invalid OSM element")
                    canonical = json.dumps(element, sort_keys=True, separators=(",", ":"))
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO elements VALUES (?, ?, ?)",
                        (element_type, element_id, canonical),
                    ).rowcount
                    if not inserted:
                        duplicate_count += 1
                        existing = connection.execute(
                            "SELECT payload FROM elements WHERE element_type = ? AND element_id = ?",
                            (element_type, element_id),
                        ).fetchone()
                        if existing is None or existing[0] != canonical:
                            raise ValueError(
                                f"Conflicting duplicate {element_type} {element_id} in {cache_path.name}"
                            )
                connection.commit()
                source_records.append(
                    {
                        "filename": cache_path.name,
                        "sha256": _sha256(cache_path),
                        "size_bytes": cache_path.stat().st_size,
                        "element_rows": len(elements),
                        "timestamp_osm_base": timestamp or None,
                        "generator": payload.get("generator"),
                    }
                )
                log(f"INGEST {index}/{len(cache_files)} {cache_path.name} rows={len(elements):,}")

            counts = {
                element_type: connection.execute(
                    "SELECT COUNT(*) FROM elements WHERE element_type = ?",
                    (element_type,),
                ).fetchone()[0]
                for element_type in ("node", "way", "relation")
            }
            missing_refs = _missing_way_node_refs(connection)
            if missing_refs:
                raise ValueError(f"Frozen source is incomplete: {missing_refs} way node refs are missing")

            part_path = output_path.with_suffix(output_path.suffix + ".part")
            with part_path.open("w", encoding="utf-8", newline="\n") as target:
                _write_osm_xml(connection, target, args.source_version)
            part_path.replace(output_path)

    source_sha256 = _sha256(output_path)
    manifest = {
        "schema_version": 1,
        "source_version": args.source_version,
        "created_at": started_at.isoformat(),
        "source_kind": "recovered_complete_osmnx_overpass_cache",
        "offline_recovery": True,
        "network_access_used": False,
        "osm_base_timestamps": sorted(timestamps),
        "license": "OpenStreetMap data under ODbL 1.0",
        "attribution": "OpenStreetMap contributors",
        "redistribution_class": "public_with_attribution",
        "copyright_notices": sorted(copyright_values),
        "input_cache_files": source_records,
        "validation": {
            "cache_file_count": len(cache_files),
            "duplicate_rows_identical": duplicate_count,
            "conflicting_duplicates": 0,
            "missing_way_node_references": 0,
            "unique_nodes": counts["node"],
            "unique_ways": counts["way"],
            "unique_relations": counts["relation"],
        },
        "artifact": {
            "filename": output_path.name,
            "sha256": source_sha256,
            "size_bytes": output_path.stat().st_size,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(
        f"COMPLETE nodes={counts['node']:,} ways={counts['way']:,} "
        f"duplicates={duplicate_count:,} bytes={output_path.stat().st_size:,} sha256={source_sha256}"
    )
    return 0


def _missing_way_node_refs(connection: sqlite3.Connection) -> int:
    node_ids = {
        row[0]
        for row in connection.execute(
            "SELECT element_id FROM elements WHERE element_type = 'node'"
        )
    }
    missing: set[int] = set()
    for (payload,) in connection.execute(
        "SELECT payload FROM elements WHERE element_type = 'way'"
    ):
        way = json.loads(payload)
        missing.update(ref for ref in way.get("nodes", []) if ref not in node_ids)
    return len(missing)


def _write_osm_xml(
    connection: sqlite3.Connection,
    target: TextIO,
    source_version: str,
) -> None:
    target.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    target.write(f"<osm version=\"0.6\" generator={quoteattr('Commute Help ' + source_version)}>\n")
    for element_type in ("node", "way", "relation"):
        rows = connection.execute(
            "SELECT payload FROM elements WHERE element_type = ? ORDER BY element_id",
            (element_type,),
        )
        for (payload,) in rows:
            element = json.loads(payload)
            _write_element(target, element)
    target.write("</osm>\n")


def _write_element(target: TextIO, element: dict[str, Any]) -> None:
    element_type = element["type"]
    attributes: dict[str, Any] = {"id": element["id"]}
    if element_type == "node":
        attributes.update({"lat": element["lat"], "lon": element["lon"]})
    for key in ("version", "timestamp", "changeset", "uid", "user", "visible"):
        if key in element:
            attributes[key] = element[key]
    attribute_text = " ".join(f"{key}={quoteattr(str(value))}" for key, value in attributes.items())
    tags = element.get("tags", {})
    children = bool(tags or element_type in {"way", "relation"})
    if not children:
        target.write(f"  <{element_type} {attribute_text}/>\n")
        return

    target.write(f"  <{element_type} {attribute_text}>\n")
    if element_type == "way":
        for node_ref in element.get("nodes", []):
            target.write(f"    <nd ref={quoteattr(str(node_ref))}/>\n")
    elif element_type == "relation":
        for member in element.get("members", []):
            target.write(
                "    <member "
                f"type={quoteattr(str(member['type']))} "
                f"ref={quoteattr(str(member['ref']))} "
                f"role={quoteattr(str(member.get('role', '')))}" 
                "/>\n"
            )
    for key, value in sorted(tags.items()):
        target.write(f"    <tag k={quoteattr(str(key))} v={quoteattr(str(value))}/>\n")
    target.write(f"  </{element_type}>\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

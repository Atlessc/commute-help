"""Build and validate a versioned SUMO network from a frozen local OSM source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import sumolib


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a local SUMO network from a checksummed OSM XML source."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--network-version", required=True)
    args = parser.parse_args()

    source_manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    if source_manifest.get("source_version") is None:
        parser.error("Source manifest is missing source_version")
    expected_sha = source_manifest.get("artifact", {}).get("sha256")
    if expected_sha != _sha256(args.source):
        parser.error("Frozen OSM source checksum does not match its manifest")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    network_path = args.output_dir / "metro.net.xml"
    manifest_path = args.output_dir / "network-manifest.json"
    if network_path.exists() or manifest_path.exists():
        parser.error("Output already exists; use a new SUMO network version")

    netconvert = _binary("netconvert")
    sumo = _binary("sumo")
    if netconvert is None or sumo is None:
        parser.error("SUMO and netconvert must be installed; run `npm run setup`")

    log_path = args.output_dir / "build.log"
    stdout_path = args.output_dir / "netconvert.stdout.log"
    stderr_path = args.output_dir / "netconvert.stderr.log"
    started = monotonic()

    def log(message: str) -> None:
        elapsed = monotonic() - started
        line = f"[{datetime.now(UTC).isoformat()}] [+{elapsed:0.3f}s] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    config_path = args.output_dir / "netconvert.cfg"
    part_path = args.output_dir / "metro.net.xml.part"
    config_args = [
        "--osm-files",
        str(args.source),
        "--output-file",
        str(part_path),
        "--geometry.remove",
        "--ramps.guess",
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--tls.default-type",
        "actuated",
        "--output.original-names",
        "true",
        "--output.street-names",
        "true",
        "--save-configuration",
        str(config_path),
    ]
    log(f"START network={args.network_version} source={source_manifest['source_version']}")
    saved = subprocess.run(
        [netconvert, *config_args],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    if saved.returncode != 0 or not config_path.is_file():
        raise RuntimeError(f"Could not write netconvert configuration: {saved.stderr.strip()}")

    log("RUN netconvert")
    built = subprocess.run(
        [netconvert, "--configuration-file", str(config_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=3600,
    )
    stdout_path.write_text(built.stdout, encoding="utf-8")
    stderr_path.write_text(built.stderr, encoding="utf-8")
    if built.returncode != 0 or not part_path.is_file():
        raise RuntimeError(
            f"netconvert failed with exit code {built.returncode}; inspect {stderr_path}"
        )
    part_path.replace(network_path)
    log(f"FINISH netconvert bytes={network_path.stat().st_size:,}")

    log("RUN SUMO load validation")
    load = subprocess.run(
        [
            sumo,
            "--net-file",
            str(network_path),
            "--begin",
            "0",
            "--end",
            "1",
            "--no-step-log",
            "true",
            "--duration-log.disable",
            "true",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    if load.returncode != 0:
        raise RuntimeError(f"SUMO could not load the built network: {load.stderr.strip()}")

    network = sumolib.net.readNet(str(network_path), withInternal=True)
    edges = network.getEdges(withInternal=False)
    internal_edges = network.getEdges(withInternal=True)
    nodes = network.getNodes()
    traffic_lights = network.getTrafficLights()
    sumo_version = _version(sumo)
    netconvert_version = _version(netconvert)
    network_sha = _sha256(network_path)
    warnings = [line for line in built.stderr.splitlines() if line.startswith("Warning:")]
    report = {
        "schema_version": 1,
        "network_version": args.network_version,
        "gate_passed": True,
        "nodes": len(nodes),
        "edges": len(edges),
        "internal_edges": len(internal_edges) - len(edges),
        "traffic_lights": len(traffic_lights),
        "netconvert_warning_count": len(warnings),
        "sumo_load_passed": True,
    }
    (args.output_dir / "validation-report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "validation-report.md").write_text(
        "\n".join(
            [
                f"# SUMO network validation - {args.network_version}",
                "",
                "- Gate: PASS",
                f"- Nodes: {len(nodes):,}",
                f"- Non-internal edges: {len(edges):,}",
                f"- Internal edges: {len(internal_edges) - len(edges):,}",
                f"- Traffic lights: {len(traffic_lights):,}",
                f"- netconvert warnings: {len(warnings):,}",
                "- SUMO load: PASS",
                "",
                "Topology and edge-mapping review remain separate Phase 1 gates.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "network_version": args.network_version,
        "created_at": datetime.now(UTC).isoformat(),
        "sumo_version": sumo_version,
        "netconvert_version": netconvert_version,
        "osm_source_version": source_manifest["source_version"],
        "osm_source_sha256": expected_sha,
        "offline_only": True,
        "network_access_used": False,
        "artifact": {
            "filename": network_path.name,
            "sha256": network_sha,
            "size_bytes": network_path.stat().st_size,
        },
        "configuration": {
            "filename": config_path.name,
            "sha256": _sha256(config_path),
        },
        "validation": report,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(
        f"COMPLETE nodes={len(nodes):,} edges={len(edges):,} "
        f"tls={len(traffic_lights):,} sha256={network_sha}"
    )
    return 0


def _binary(command: str) -> str | None:
    discovered = shutil.which(command)
    if discovered:
        return discovered
    candidate = Path(sys.executable).parent / command
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _version(binary: str) -> str | None:
    result = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(
        r"\b(?:sumo|netconvert)\s+([0-9]+(?:\.[0-9]+){1,2})\b",
        output,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

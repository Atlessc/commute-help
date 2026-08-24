"""Materialize versioned projected WKB for three comparison-v2 synthetic members."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.synthetic_boundary_geometry_service import (
    build_synthetic_boundary_geometries_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-directory", type=Path, required=True)
    parser.add_argument("--comparison-validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    network = args.network_directory.resolve()
    manifest = build_synthetic_boundary_geometries_v1(
        comparison_directory=network / "comparison-relations-v2",
        comparison_validation_directory=args.comparison_validation.resolve(),
        sumo_network_path=network / "metro.net.xml",
        sumo_network_manifest_path=network / "network-manifest.json",
        output_directory=args.output.resolve(),
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

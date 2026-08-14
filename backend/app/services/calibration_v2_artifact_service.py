"""Parsing and deterministic serialization for calibration-v2 artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.app.schemas.calibration_v2 import CalibrationArtifactV2


def parse_calibration_artifact(
    value: str | bytes | bytearray,
) -> CalibrationArtifactV2:
    """Validate one JSON artifact without reading or mutating production data."""

    return CalibrationArtifactV2.model_validate_json(value)


def load_calibration_artifact(path: Path) -> CalibrationArtifactV2:
    """Load and validate one explicitly selected local calibration artifact."""

    return parse_calibration_artifact(path.read_bytes())


def canonical_calibration_artifact_json(artifact: CalibrationArtifactV2) -> str:
    """Return stable UTF-8 JSON for a validated, semantically equivalent artifact."""

    payload = artifact.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def calibration_artifact_sha256(artifact: CalibrationArtifactV2) -> str:
    """Hash the canonical serialization used by later provenance manifests."""

    encoded = canonical_calibration_artifact_json(artifact).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

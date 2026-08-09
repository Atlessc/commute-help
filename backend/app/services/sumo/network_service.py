"""Checksum-verified access to one immutable SUMO network and its edge bridge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import sumolib


class SumoNetworkError(RuntimeError):
    pass


class UnsafeClosureMappingError(SumoNetworkError):
    def __init__(self, edge_ids: list[str]) -> None:
        self.edge_ids = edge_ids
        super().__init__(
            "Closure simulation blocked because these app edges lack an accepted "
            f"SUMO mapping: {', '.join(edge_ids)}"
        )


class SumoNetworkService:
    """Load immutable network metadata and translate only accepted closures."""

    def __init__(self, network_path: Path, manifest_path: Path, edge_map_path: Path) -> None:
        self.network_path = network_path
        self.manifest_path = manifest_path
        self.edge_map_path = edge_map_path
        self.manifest: dict[str, Any] | None = None
        self.network: Any | None = None
        self.edge_map: pd.DataFrame | None = None

    def load(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if _sha256(self.network_path) != manifest["artifact"]["sha256"]:
            raise SumoNetworkError("SUMO network checksum does not match its manifest")
        mapping = pd.read_parquet(self.edge_map_path)
        if set(mapping["sumo_network_version"].unique()) != {
            manifest["network_version"]
        }:
            raise SumoNetworkError("Edge map does not belong to the SUMO network manifest")
        if set(mapping["osm_source_sha256"].unique()) != {
            manifest["osm_source_sha256"]
        }:
            raise SumoNetworkError("Edge map and SUMO network source lineage differ")
        self.network = sumolib.net.readNet(str(self.network_path), withInternal=True)
        self.manifest = manifest
        self.edge_map = mapping

    def translate_closure_edges(self, app_edge_ids: list[str]) -> dict[str, list[str]]:
        if self.edge_map is None:
            raise SumoNetworkError("SUMO network service has not been loaded")
        result: dict[str, list[str]] = {}
        blocked: list[str] = []
        for app_edge_id in dict.fromkeys(app_edge_ids):
            candidates = self.edge_map[self.edge_map["app_edge_id"] == app_edge_id]
            statuses = set(candidates["status"].astype(str))
            sumo_ids = sorted(
                set(candidates.loc[candidates["status"] == "accepted", "sumo_edge_id"])
                - {None}
            )
            if statuses != {"accepted"} or not sumo_ids:
                blocked.append(app_edge_id)
            else:
                result[app_edge_id] = [str(edge_id) for edge_id in sumo_ids]
        if blocked:
            raise UnsafeClosureMappingError(blocked)
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

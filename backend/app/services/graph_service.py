"""Load and retain the versioned routing graph once per backend process."""

import hashlib
import json
import logging
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
from pydantic import ValidationError

from backend.app.schemas.graph import GraphManifest, GraphRuntimeStatus

logger = logging.getLogger(__name__)


class GraphService:
    """Own the immutable shared graph, edge table, and spatial index."""

    def __init__(self, graph_path: Path, manifest_path: Path) -> None:
        self.graph_path = graph_path
        self.manifest_path = manifest_path
        self.graph: nx.MultiDiGraph | None = None
        self.edges: gpd.GeoDataFrame | None = None
        self.manifest: GraphManifest | None = None
        self._status = GraphRuntimeStatus(status="not_configured")

    @property
    def ready(self) -> bool:
        return self._status.status == "ready"

    def load(self) -> None:
        """Load and verify configured artifacts, leaving safe error status."""

        if not self.graph_path.exists() and not self.manifest_path.exists():
            self._status = GraphRuntimeStatus(status="not_configured")
            return

        if not self.graph_path.exists() or not self.manifest_path.exists():
            self._set_error("Routing graph artifacts are incomplete. Rebuild the graph.")
            return

        try:
            manifest = GraphManifest.model_validate_json(
                self.manifest_path.read_text(encoding="utf-8")
            )
            if not manifest.validation_passed:
                raise ValueError("graph manifest records a failed validation gate")
            graph_artifact = manifest.artifacts.get("graphml")
            if graph_artifact is None:
                raise ValueError("manifest does not identify the GraphML artifact")
            if self._sha256(self.graph_path) != graph_artifact.sha256:
                raise ValueError("GraphML checksum does not match its manifest")

            graph = ox.io.load_graphml(self.graph_path)
            if graph.graph.get("graph_version") != manifest.graph_version:
                raise ValueError("graph version does not match its manifest")

            edges = ox.convert.graph_to_gdfs(graph, nodes=False, fill_edge_geometry=True)
            _ = edges.sindex

            self.graph = graph
            self.edges = edges
            self.manifest = manifest
            self._status = GraphRuntimeStatus(
                status="ready",
                version=manifest.graph_version,
                nodes=manifest.metrics.nodes,
                directed_edges=manifest.metrics.directed_edges,
            )
        except (OSError, ValueError, ValidationError) as error:
            logger.exception("Routing graph failed to load: %s", error)
            self._set_error("Routing graph could not be loaded. Rebuild or validate it.")

    def runtime_status(self) -> GraphRuntimeStatus:
        """Return a detached, browser-safe status snapshot."""

        return self._status.model_copy()

    def require_manifest(self) -> GraphManifest:
        """Return the validated manifest or signal that the graph is unavailable."""

        if not self.ready or self.manifest is None:
            raise GraphUnavailableError(self._status.message)
        return self.manifest

    def _set_error(self, message: str) -> None:
        self.graph = None
        self.edges = None
        self.manifest = None
        self._status = GraphRuntimeStatus(status="error", message=message)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class GraphUnavailableError(RuntimeError):
    """The generated routing graph is not ready for use."""

"""Schemas for graph manifests and runtime readiness."""

from typing import Literal

from pydantic import BaseModel, Field


class GraphArtifact(BaseModel):
    """A generated graph artifact identified without exposing local paths."""

    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class GraphRegion(BaseModel):
    """Versioned geographic build boundary."""

    id: str
    name: str
    bounds: tuple[float, float, float, float]


class GraphMetrics(BaseModel):
    """High-level graph counts recorded at build time."""

    nodes: int = Field(ge=0)
    directed_edges: int = Field(ge=0)
    weakly_connected_components: int = Field(ge=0)
    largest_weak_component_percent: float = Field(ge=0, le=100)


class GraphManifest(BaseModel):
    """Reproducibility and integrity metadata for one routing graph."""

    schema_version: Literal[1]
    graph_version: str
    source: str
    osm_source_version: str | None = None
    osm_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    built_at: str
    region: GraphRegion
    network_type: Literal["drive"]
    build_tool: str
    validation_passed: bool
    metrics: GraphMetrics
    artifacts: dict[str, GraphArtifact]


class GraphRuntimeStatus(BaseModel):
    """Safe graph readiness information returned to browsers."""

    status: Literal["not_configured", "ready", "error"]
    version: str | None = None
    nodes: int | None = None
    directed_edges: int | None = None
    message: str | None = None

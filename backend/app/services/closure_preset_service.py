"""Load and verify curated closure presets against the active road graph."""

from pathlib import Path

from pydantic import ValidationError

from backend.app.schemas.closure_presets import (
    ClosurePresetCatalog,
    ClosurePresetDefinition,
    ClosurePresetRecord,
)
from backend.app.services.graph_service import GraphService


class ClosurePresetService:
    """Own the local preset catalog and its graph-identity safety gate."""

    def __init__(self, catalog_path: Path, graph_service: GraphService) -> None:
        self.catalog_path = catalog_path
        self.graph_service = graph_service
        self._catalog: ClosurePresetCatalog | None = None

    def list(self) -> list[ClosurePresetRecord]:
        return [self._record(preset) for preset in self._load().presets]

    def get(self, preset_id: str) -> ClosurePresetRecord:
        preset = next(
            (candidate for candidate in self._load().presets if candidate.id == preset_id),
            None,
        )
        if preset is None:
            raise ClosurePresetNotFoundError(preset_id)
        return self._record(preset)

    def _load(self) -> ClosurePresetCatalog:
        if self._catalog is not None:
            return self._catalog
        try:
            self._catalog = ClosurePresetCatalog.model_validate_json(
                self.catalog_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise ClosurePresetCatalogError(
                f"Closure preset catalog could not be loaded: {error}"
            ) from error
        return self._catalog

    def _record(self, preset: ClosurePresetDefinition) -> ClosurePresetRecord:
        manifest = self.graph_service.manifest
        if manifest is None:
            return ClosurePresetRecord(
                **preset.model_dump(),
                graph_status="review_required",
                warnings=["The routing graph is unavailable, so preset roads could not be checked."],
            )
        if manifest.graph_version != preset.graph_version:
            return ClosurePresetRecord(
                **preset.model_dump(),
                graph_status="review_required",
                warnings=[
                    "This preset was marked on an older road graph. Review and rebuild it before applying."
                ],
            )
        if not self._sections_match_graph(preset):
            return ClosurePresetRecord(
                **preset.model_dump(),
                graph_status="review_required",
                warnings=[
                    "At least one preset road no longer matches its saved directed edge identity."
                ],
            )
        return ClosurePresetRecord(
            **preset.model_dump(),
            graph_status="current",
            warnings=[],
        )

    def _sections_match_graph(self, preset: ClosurePresetDefinition) -> bool:
        graph = self.graph_service.graph
        if graph is None:
            return False
        for section in preset.sections:
            selected = set(section.selected_edge_ids)
            snapshots = {
                direction.edge.edge_id: direction.edge
                for direction in section.selection.directions
                if direction.edge.edge_id in selected
            }
            if set(snapshots) != selected:
                return False
            for edge_id, snapshot in snapshots.items():
                node_u = self.graph_service.node_lookup.get(snapshot.u)
                node_v = self.graph_service.node_lookup.get(snapshot.v)
                if node_u is None or node_v is None:
                    return False
                edge_data = graph.get_edge_data(node_u, node_v, snapshot.key)
                if edge_data is None:
                    return False
                if str(edge_data.get("edge_id")) != edge_id:
                    return False
                if (
                    snapshot.geometry_fingerprint
                    and str(edge_data.get("geometry_fingerprint", ""))
                    != snapshot.geometry_fingerprint
                ):
                    return False
        return True


class ClosurePresetNotFoundError(LookupError):
    pass


class ClosurePresetCatalogError(RuntimeError):
    pass

"""SQLite-backed shared scenarios with immutable revision history."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from backend.app.db.database import DatabaseManager
from backend.app.schemas.scenarios import (
    SavedClosureSection,
    ScenarioContent,
    ScenarioCreateRequest,
    ScenarioDuplicateRequest,
    ScenarioImportRequest,
    ScenarioListItem,
    ScenarioRecord,
    ScenarioUpdateRequest,
)
from backend.app.services.graph_service import GraphService
from backend.app.services.spatial_service import SpatialService

PACIFIC = ZoneInfo("America/Los_Angeles")


class ScenarioService:
    """Persist validated scenario state through short SQLite transactions."""

    def __init__(
        self,
        database: DatabaseManager,
        graph_service: GraphService,
        spatial_service: SpatialService,
    ) -> None:
        self.database = database
        self.graph_service = graph_service
        self.spatial_service = spatial_service

    def list(self, include_archived: bool = False) -> list[ScenarioListItem]:
        query = "SELECT * FROM scenarios"
        parameters: tuple[Any, ...] = ()
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY updated_at DESC LIMIT 200"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._list_item(row) for row in rows]

    def get(self, scenario_id: UUID) -> ScenarioRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scenarios WHERE id = ?", (str(scenario_id),)
            ).fetchone()
        if row is None:
            raise ScenarioNotFoundError()
        return self._record(row)

    def create(self, request: ScenarioCreateRequest) -> ScenarioRecord:
        return self._insert(
            uuid4(),
            request.name,
            request.content,
            request.editor_name,
        )

    def update(self, scenario_id: UUID, request: ScenarioUpdateRequest) -> ScenarioRecord:
        now = datetime.now(PACIFIC).isoformat()
        content_json = request.content.model_dump_json()
        name = _clean_name(request.name)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT revision FROM scenarios WHERE id = ?", (str(scenario_id),)
            ).fetchone()
            if current is None:
                raise ScenarioNotFoundError()
            actual_revision = int(current["revision"])
            if actual_revision != request.expected_revision:
                raise ScenarioRevisionConflictError(actual_revision)
            revision = actual_revision + 1
            connection.execute(
                """
                UPDATE scenarios
                SET name = ?, revision = ?, graph_version = ?, content_json = ?,
                    archived = 0, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    revision,
                    request.content.graph_version,
                    content_json,
                    now,
                    str(scenario_id),
                ),
            )
            self._insert_revision(
                connection,
                str(scenario_id),
                revision,
                name,
                request.content,
                request.editor_name,
                now,
            )
        return self.get(scenario_id)

    def duplicate(
        self, scenario_id: UUID, request: ScenarioDuplicateRequest
    ) -> ScenarioRecord:
        source = self.get(scenario_id)
        name = request.name or f"{source.name} copy"
        return self._insert(uuid4(), name, source.content, request.editor_name)

    def archive(self, scenario_id: UUID) -> None:
        now = datetime.now(PACIFIC).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE scenarios SET archived = 1, updated_at = ? WHERE id = ?",
                (now, str(scenario_id)),
            )
        if cursor.rowcount == 0:
            raise ScenarioNotFoundError()

    def import_scenario(self, request: ScenarioImportRequest) -> ScenarioRecord:
        imported = self._insert(
            uuid4(),
            request.name or f"{request.scenario.name} imported",
            request.scenario.content,
            request.editor_name,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO scenario_imports
                    (id, imported_scenario_id, source_json, imported_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    str(imported.id),
                    request.scenario.model_dump_json(),
                    datetime.now(PACIFIC).isoformat(),
                ),
            )
        return imported

    def _insert(
        self,
        scenario_id: UUID,
        name: str,
        content: ScenarioContent,
        editor_name: str | None,
    ) -> ScenarioRecord:
        clean_name = _clean_name(name)
        now = datetime.now(PACIFIC).isoformat()
        content_json = content.model_dump_json()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO scenarios
                    (id, name, revision, graph_version, content_json, archived,
                     created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, 0, ?, ?)
                """,
                (
                    str(scenario_id),
                    clean_name,
                    content.graph_version,
                    content_json,
                    now,
                    now,
                ),
            )
            self._insert_revision(
                connection,
                str(scenario_id),
                1,
                clean_name,
                content,
                editor_name,
                now,
            )
        return self.get(scenario_id)

    @staticmethod
    def _insert_revision(
        connection: Any,
        scenario_id: str,
        revision: int,
        name: str,
        content: ScenarioContent,
        editor_name: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO scenario_revisions
                (scenario_id, revision, name, graph_version, content_json,
                 editor_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scenario_id,
                revision,
                name,
                content.graph_version,
                content.model_dump_json(),
                editor_name.strip() if editor_name else None,
                created_at,
            ),
        )

    def _record(self, row: Any) -> ScenarioRecord:
        content = ScenarioContent.model_validate_json(row["content_json"])
        content, graph_status, warnings = self._review_graph(content)
        return ScenarioRecord(
            id=row["id"],
            name=row["name"],
            revision=row["revision"],
            graph_version=row["graph_version"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content=content,
            graph_status=graph_status,
            warnings=warnings,
        )

    @staticmethod
    def _list_item(row: Any) -> ScenarioListItem:
        return ScenarioListItem(
            id=row["id"],
            name=row["name"],
            revision=row["revision"],
            graph_version=row["graph_version"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _review_graph(
        self, content: ScenarioContent
    ) -> tuple[ScenarioContent, str, list[str]]:
        manifest = self.graph_service.manifest
        if manifest is None:
            return (
                content,
                "review_required",
                ["The routing graph is unavailable, so saved roads could not be checked."],
            )
        if content.graph_version == manifest.graph_version:
            return content, "current", []

        try:
            origin = self.spatial_service.select_road(content.origin.lat, content.origin.lng)
            destination = self.spatial_service.select_road(
                content.destination.lat, content.destination.lng
            )
            rematched_sections = [
                self._rematch_section(section) for section in content.closures
            ]
        except (ValueError, ScenarioRematchError):
            return (
                content,
                "review_required",
                [
                    "This scenario uses an older road graph and at least one location or closure could not be rematched with high confidence. Review it before routing."
                ],
            )

        rematched = content.model_copy(
            update={
                "graph_version": manifest.graph_version,
                "origin": origin,
                "destination": destination,
                "closures": rematched_sections,
            }
        )
        return (
            rematched,
            "rematched",
            [
                "Saved locations and closure directions were rematched by OSM identity and geometry. Review them before saving a new revision."
            ],
        )

    def _rematch_section(self, section: SavedClosureSection) -> SavedClosureSection:
        geometry = section.selection.directions[0].geometry.coordinates
        midpoint = geometry[len(geometry) // 2]
        selection = self.spatial_service.select_closure_road(midpoint[1], midpoint[0])
        selected_ids: list[str] = []
        saved_selected = [
            direction
            for direction in section.selection.directions
            if direction.edge.edge_id in section.selected_edge_ids
        ]
        for saved in saved_selected:
            matches = [
                candidate
                for candidate in selection.directions
                if candidate.direction_label == saved.direction_label
                and candidate.edge.geometry_fingerprint
                == saved.edge.geometry_fingerprint
                and set(candidate.edge.osm_way_ids) == set(saved.edge.osm_way_ids)
            ]
            if len(matches) != 1:
                raise ScenarioRematchError()
            selected_ids.append(matches[0].edge.edge_id)
        return SavedClosureSection(
            selection=selection,
            selected_edge_ids=selected_ids,
            restriction=section.restriction,
        )


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise InvalidScenarioNameError()
    return name


class ScenarioNotFoundError(LookupError):
    pass


class ScenarioRevisionConflictError(RuntimeError):
    def __init__(self, actual_revision: int) -> None:
        self.actual_revision = actual_revision
        super().__init__("The scenario was changed in another browser.")


class InvalidScenarioNameError(ValueError):
    pass


class ScenarioRematchError(RuntimeError):
    pass

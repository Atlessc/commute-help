"""Read-only consumer for portable WorldPack traffic-state artifacts."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

import numpy as np
import pandas as pd


WORLDPACK_FORMAT = "commute-help-worldpack"
WORLDPACK_VERSION = 0

SpeedSource = Literal["observed", "free_flow", "floor"]


class WorldPackError(RuntimeError):
    """The WorldPack is missing, malformed, or incompatible."""


class WorldPackTimeError(WorldPackError):
    """No causally valid WorldPack state exists for the requested time."""


@dataclass(frozen=True)
class WorldPackCoverage:
    """Exact accepted mapping coverage between the app and WorldPack."""

    worldpack_road_count: int
    worldpack_source_count: int
    accepted_relation_count: int
    inside_relation_count: int
    worldpack_sources_reached: int
    worldpack_sources_unreached: int
    worldpack_source_coverage_percent: float
    accepted_app_edge_count: int
    app_edges_touching_worldpack: int


@dataclass(frozen=True)
class WorldPackRoadSample:
    """Traffic state for one MOSS road at one causal snapshot."""

    moss_road_id: int
    source_sumo_edge_id: str
    snapshot_time_s: float
    road_length_m: float
    free_flow_speed_mps: float
    raw_mean_speed_mps: float | None
    effective_speed_mps: float
    vehicle_count: int
    waiting_vehicle_count: int
    speed_source: SpeedSource


@dataclass(frozen=True)
class WorldPackAppEdgeSample:
    """WorldPack state associated with one canonical Commute Help edge."""

    app_edge_id: str
    requested_time_s: float
    snapshot_time_s: float | None
    mapped: bool
    moss_road_ids: tuple[int, ...]
    road_samples: tuple[WorldPackRoadSample, ...]
    equivalent_speed_mps: float | None


class WorldPackService:
    """Load one immutable WorldPack and expose causal traffic-state lookups.

    WorldPack v0 does not become a second routing graph. Commute Help's
    canonical graph remains authoritative.

    Mapping path:

        app edge ID
          -> accepted existing SUMO edge relation
          -> WorldPack source SUMO edge ID
          -> MOSS road ID
          -> time-indexed road state
    """

    def __init__(
        self,
        worldpack_path: Path,
        edge_map_path: Path,
        *,
        technical_speed_floor_mps: float = 0.1,
    ) -> None:
        if not math.isfinite(technical_speed_floor_mps):
            raise ValueError("technical_speed_floor_mps must be finite")
        if technical_speed_floor_mps <= 0:
            raise ValueError("technical_speed_floor_mps must be positive")

        self.worldpack_path = worldpack_path
        self.edge_map_path = edge_map_path
        self.technical_speed_floor_mps = technical_speed_floor_mps

        self.world_id: str | None = None
        self.classification: str | None = None
        self.manifest: Mapping[str, object] | None = None

        self._road_ids: np.ndarray | None = None
        self._road_lengths_m: np.ndarray | None = None
        self._free_flow_speed_mps: np.ndarray | None = None
        self._sample_times_s: np.ndarray | None = None
        self._mean_speed_mps: np.ndarray | None = None
        self._vehicle_count: np.ndarray | None = None
        self._waiting_vehicle_count: np.ndarray | None = None

        self._road_position: Mapping[int, int] = MappingProxyType({})
        self._moss_to_source: Mapping[int, str] = MappingProxyType({})
        self._source_to_moss: Mapping[str, int] = MappingProxyType({})
        self._app_to_moss_roads: Mapping[str, tuple[int, ...]] = MappingProxyType({})

        self._coverage: WorldPackCoverage | None = None
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def coverage(self) -> WorldPackCoverage:
        self._require_loaded()
        assert self._coverage is not None
        return self._coverage

    @property
    def sample_times_s(self) -> np.ndarray:
        """Return the immutable snapshot-time array."""

        self._require_loaded()
        assert self._sample_times_s is not None
        return self._sample_times_s

    def load(self) -> None:
        """Validate and load the WorldPack into immutable process memory."""

        required = {
            "manifest.json",
            "road-static.npz",
            "road-state.npz",
            "road-id-map.json",
        }

        missing = sorted(
            filename
            for filename in required
            if not (self.worldpack_path / filename).is_file()
        )
        if missing:
            raise WorldPackError(
                "WorldPack is incomplete; missing: "
                + ", ".join(missing)
            )

        if not self.edge_map_path.is_file():
            raise WorldPackError(
                f"App-to-SUMO edge map is missing: {self.edge_map_path}"
            )

        manifest = self._load_manifest()
        road_mapping = self._load_road_mapping()

        (
            road_ids,
            road_lengths_m,
            free_flow_speed_mps,
        ) = self._load_static()

        (
            sample_times_s,
            state_road_ids,
            mean_speed_mps,
            vehicle_count,
            waiting_vehicle_count,
        ) = self._load_state()

        self._validate_arrays(
            manifest=manifest,
            road_ids=road_ids,
            state_road_ids=state_road_ids,
            road_lengths_m=road_lengths_m,
            free_flow_speed_mps=free_flow_speed_mps,
            sample_times_s=sample_times_s,
            mean_speed_mps=mean_speed_mps,
            vehicle_count=vehicle_count,
            waiting_vehicle_count=waiting_vehicle_count,
        )

        moss_to_source = {
            int(moss_id): str(source_id)
            for moss_id, source_id
            in road_mapping["moss_to_source"].items()
        }

        if len(moss_to_source) != len(road_ids):
            raise WorldPackError(
                "road-id-map count does not match WorldPack road count"
            )

        road_id_set = {
            int(road_id)
            for road_id in road_ids
        }

        mapping_id_set = set(moss_to_source)

        if mapping_id_set != road_id_set:
            missing_mapping = road_id_set - mapping_id_set
            unknown_mapping = mapping_id_set - road_id_set
            raise WorldPackError(
                "road-id-map and NPZ road IDs differ: "
                f"missing={len(missing_mapping)}, "
                f"unknown={len(unknown_mapping)}"
            )

        source_to_moss: dict[str, int] = {}

        for moss_id, source_id in moss_to_source.items():
            if source_id in source_to_moss:
                raise WorldPackError(
                    "WorldPack source SUMO mapping is ambiguous: "
                    f"{source_id}"
                )
            source_to_moss[source_id] = moss_id

        (
            app_to_moss,
            coverage,
        ) = self._load_app_mapping(
            source_to_moss=source_to_moss,
            worldpack_road_count=len(road_ids),
        )

        road_position = {
            int(road_id): position
            for position, road_id
            in enumerate(road_ids)
        }

        self._freeze_array(road_ids)
        self._freeze_array(road_lengths_m)
        self._freeze_array(free_flow_speed_mps)
        self._freeze_array(sample_times_s)
        self._freeze_array(mean_speed_mps)
        self._freeze_array(vehicle_count)
        self._freeze_array(waiting_vehicle_count)

        self._road_ids = road_ids
        self._road_lengths_m = road_lengths_m
        self._free_flow_speed_mps = free_flow_speed_mps
        self._sample_times_s = sample_times_s
        self._mean_speed_mps = mean_speed_mps
        self._vehicle_count = vehicle_count
        self._waiting_vehicle_count = waiting_vehicle_count

        self._road_position = MappingProxyType(road_position)
        self._moss_to_source = MappingProxyType(moss_to_source)
        self._source_to_moss = MappingProxyType(source_to_moss)
        self._app_to_moss_roads = MappingProxyType(app_to_moss)

        self.world_id = str(manifest["world_id"])
        self.classification = str(manifest["classification"])
        self.manifest = MappingProxyType(manifest)
        self._coverage = coverage
        self._loaded = True

    def moss_roads_for_app_edge(
        self,
        app_edge_id: str,
    ) -> tuple[int, ...]:
        """Return all exact accepted WorldPack roads for one app edge."""

        self._require_loaded()
        return self._app_to_moss_roads.get(
            str(app_edge_id),
            (),
        )

    def snapshot_index_for_time(
        self,
        requested_time_s: float,
    ) -> int:
        """Return the newest snapshot at or before requested_time_s."""

        self._require_loaded()

        if not math.isfinite(requested_time_s):
            raise WorldPackTimeError(
                "requested WorldPack time must be finite"
            )

        assert self._sample_times_s is not None

        index = (
            int(
                np.searchsorted(
                    self._sample_times_s,
                    requested_time_s,
                    side="right",
                )
            )
            - 1
        )

        if index < 0:
            first = float(self._sample_times_s[0])
            raise WorldPackTimeError(
                "No causally valid WorldPack snapshot exists "
                f"for t={requested_time_s}; first snapshot is t={first}"
            )

        return index

    def road_sample(
        self,
        moss_road_id: int,
        requested_time_s: float,
    ) -> WorldPackRoadSample:
        """Return one road's traffic state using causal snapshot lookup."""

        self._require_loaded()

        moss_road_id = int(moss_road_id)

        try:
            road_index = self._road_position[moss_road_id]
        except KeyError as error:
            raise WorldPackError(
                f"Unknown MOSS road ID: {moss_road_id}"
            ) from error

        snapshot_index = self.snapshot_index_for_time(
            requested_time_s
        )

        assert self._sample_times_s is not None
        assert self._road_lengths_m is not None
        assert self._free_flow_speed_mps is not None
        assert self._mean_speed_mps is not None
        assert self._vehicle_count is not None
        assert self._waiting_vehicle_count is not None

        snapshot_time_s = float(
            self._sample_times_s[snapshot_index]
        )
        road_length_m = float(
            self._road_lengths_m[road_index]
        )
        free_flow_speed_mps = float(
            self._free_flow_speed_mps[road_index]
        )
        vehicle_count = int(
            self._vehicle_count[
                snapshot_index,
                road_index,
            ]
        )
        waiting_vehicle_count = int(
            self._waiting_vehicle_count[
                snapshot_index,
                road_index,
            ]
        )

        observed = float(
            self._mean_speed_mps[
                snapshot_index,
                road_index,
            ]
        )

        raw_mean_speed_mps: float | None

        if vehicle_count == 0:
            raw_mean_speed_mps = None
            effective_speed_mps = free_flow_speed_mps
            speed_source: SpeedSource = "free_flow"
        else:
            raw_mean_speed_mps = observed

            if observed < self.technical_speed_floor_mps:
                effective_speed_mps = (
                    self.technical_speed_floor_mps
                )
                speed_source = "floor"
            else:
                effective_speed_mps = observed
                speed_source = "observed"

        return WorldPackRoadSample(
            moss_road_id=moss_road_id,
            source_sumo_edge_id=self._moss_to_source[
                moss_road_id
            ],
            snapshot_time_s=snapshot_time_s,
            road_length_m=road_length_m,
            free_flow_speed_mps=free_flow_speed_mps,
            raw_mean_speed_mps=raw_mean_speed_mps,
            effective_speed_mps=effective_speed_mps,
            vehicle_count=vehicle_count,
            waiting_vehicle_count=waiting_vehicle_count,
            speed_source=speed_source,
        )

    def app_edge_sample(
        self,
        app_edge_id: str,
        requested_time_s: float,
    ) -> WorldPackAppEdgeSample:
        """Return the WorldPack roads associated with one app edge.

        For 1:N app-to-SUMO mappings, the equivalent speed is a
        road-length-weighted harmonic speed. This represents the
        combined travel time of the mapped WorldPack segments without
        pretending that the WorldPack is the authoritative app graph.

        The canonical app-edge length will be applied by the router in
        the next integration slice.
        """

        self._require_loaded()

        app_edge_id = str(app_edge_id)

        road_ids = self.moss_roads_for_app_edge(
            app_edge_id
        )

        if not road_ids:
            return WorldPackAppEdgeSample(
                app_edge_id=app_edge_id,
                requested_time_s=float(
                    requested_time_s
                ),
                snapshot_time_s=None,
                mapped=False,
                moss_road_ids=(),
                road_samples=(),
                equivalent_speed_mps=None,
            )

        samples = tuple(
            self.road_sample(
                road_id,
                requested_time_s,
            )
            for road_id in road_ids
        )

        total_length = sum(
            sample.road_length_m
            for sample in samples
        )

        total_travel_time = sum(
            sample.road_length_m
            / sample.effective_speed_mps
            for sample in samples
        )

        if total_length <= 0 or total_travel_time <= 0:
            raise WorldPackError(
                f"Invalid mapped road costs for app edge {app_edge_id}"
            )

        equivalent_speed = (
            total_length
            / total_travel_time
        )

        snapshot_times = {
            sample.snapshot_time_s
            for sample in samples
        }

        if len(snapshot_times) != 1:
            raise WorldPackError(
                "Mapped roads unexpectedly used different snapshots"
            )

        return WorldPackAppEdgeSample(
            app_edge_id=app_edge_id,
            requested_time_s=float(
                requested_time_s
            ),
            snapshot_time_s=samples[0].snapshot_time_s,
            mapped=True,
            moss_road_ids=road_ids,
            road_samples=samples,
            equivalent_speed_mps=equivalent_speed,
        )

    def _load_manifest(self) -> dict[str, object]:
        path = self.worldpack_path / "manifest.json"

        try:
            manifest = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError) as error:
            raise WorldPackError(
                "WorldPack manifest is not valid JSON"
            ) from error

        if manifest.get("format") != WORLDPACK_FORMAT:
            raise WorldPackError(
                "Unsupported WorldPack format: "
                f"{manifest.get('format')!r}"
            )

        if manifest.get("version") != WORLDPACK_VERSION:
            raise WorldPackError(
                "Unsupported WorldPack version: "
                f"{manifest.get('version')!r}"
            )

        if not manifest.get("world_id"):
            raise WorldPackError(
                "WorldPack manifest has no world_id"
            )

        if not manifest.get("classification"):
            raise WorldPackError(
                "WorldPack manifest has no classification"
            )

        return manifest

    def _load_road_mapping(
        self,
    ) -> dict[str, object]:
        path = (
            self.worldpack_path
            / "road-id-map.json"
        )

        try:
            mapping = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError) as error:
            raise WorldPackError(
                "WorldPack road-id-map is not valid JSON"
            ) from error

        if mapping.get("format") != WORLDPACK_FORMAT:
            raise WorldPackError(
                "road-id-map format does not match WorldPack"
            )

        if mapping.get("version") != WORLDPACK_VERSION:
            raise WorldPackError(
                "road-id-map version does not match WorldPack"
            )

        if (
            mapping.get("direction")
            != "moss_road_id_to_source_sumo_edge_id"
        ):
            raise WorldPackError(
                "road-id-map has unsupported mapping direction"
            )

        moss_to_source = mapping.get(
            "moss_to_source"
        )

        if not isinstance(
            moss_to_source,
            dict,
        ):
            raise WorldPackError(
                "road-id-map moss_to_source must be an object"
            )

        return mapping

    def _load_static(
        self,
    ) -> tuple[
        np.ndarray,
        np.ndarray,
        np.ndarray,
    ]:
        path = (
            self.worldpack_path
            / "road-static.npz"
        )

        try:
            with np.load(
                path,
                allow_pickle=False,
            ) as data:
                expected = {
                    "road_ids",
                    "road_length_m",
                    "free_flow_speed_mps",
                }

                if set(data.files) != expected:
                    raise WorldPackError(
                        "road-static.npz schema mismatch"
                    )

                return (
                    data["road_ids"].copy(),
                    data["road_length_m"].copy(),
                    data[
                        "free_flow_speed_mps"
                    ].copy(),
                )
        except (OSError, ValueError) as error:
            if isinstance(
                error,
                WorldPackError,
            ):
                raise
            raise WorldPackError(
                "Could not read road-static.npz"
            ) from error

    def _load_state(
        self,
    ) -> tuple[
        np.ndarray,
        np.ndarray,
        np.ndarray,
        np.ndarray,
        np.ndarray,
    ]:
        path = (
            self.worldpack_path
            / "road-state.npz"
        )

        try:
            with np.load(
                path,
                allow_pickle=False,
            ) as data:
                expected = {
                    "sample_times_s",
                    "road_ids",
                    "mean_speed_mps",
                    "vehicle_count",
                    "waiting_vehicle_count",
                }

                if set(data.files) != expected:
                    raise WorldPackError(
                        "road-state.npz schema mismatch"
                    )

                return (
                    data["sample_times_s"].copy(),
                    data["road_ids"].copy(),
                    data[
                        "mean_speed_mps"
                    ].copy(),
                    data[
                        "vehicle_count"
                    ].copy(),
                    data[
                        "waiting_vehicle_count"
                    ].copy(),
                )
        except (OSError, ValueError) as error:
            if isinstance(
                error,
                WorldPackError,
            ):
                raise
            raise WorldPackError(
                "Could not read road-state.npz"
            ) from error

    def _validate_arrays(
        self,
        *,
        manifest: dict[str, object],
        road_ids: np.ndarray,
        state_road_ids: np.ndarray,
        road_lengths_m: np.ndarray,
        free_flow_speed_mps: np.ndarray,
        sample_times_s: np.ndarray,
        mean_speed_mps: np.ndarray,
        vehicle_count: np.ndarray,
        waiting_vehicle_count: np.ndarray,
    ) -> None:
        expected_dtypes = {
            "road_ids": np.dtype("int64"),
            "road_length_m": np.dtype("float32"),
            "free_flow_speed_mps": np.dtype("float32"),
            "sample_times_s": np.dtype("float32"),
            "mean_speed_mps": np.dtype("float32"),
            "vehicle_count": np.dtype("uint32"),
            "waiting_vehicle_count": np.dtype("uint32"),
        }

        actual_dtypes = {
            "road_ids": road_ids.dtype,
            "road_length_m": road_lengths_m.dtype,
            "free_flow_speed_mps": free_flow_speed_mps.dtype,
            "sample_times_s": sample_times_s.dtype,
            "mean_speed_mps": mean_speed_mps.dtype,
            "vehicle_count": vehicle_count.dtype,
            "waiting_vehicle_count": waiting_vehicle_count.dtype,
        }

        for field, expected in expected_dtypes.items():
            if actual_dtypes[field] != expected:
                raise WorldPackError(
                    f"{field} dtype must be {expected}; "
                    f"got {actual_dtypes[field]}"
                )

        if road_ids.ndim != 1:
            raise WorldPackError(
                "road_ids must be one-dimensional"
            )

        road_count = len(road_ids)

        if road_count == 0:
            raise WorldPackError(
                "WorldPack contains no roads"
            )

        if len(np.unique(road_ids)) != road_count:
            raise WorldPackError(
                "WorldPack contains duplicate road IDs"
            )

        if not np.array_equal(
            road_ids,
            state_road_ids,
        ):
            raise WorldPackError(
                "Static and state road ID ordering differs"
            )

        if road_lengths_m.shape != (road_count,):
            raise WorldPackError(
                "road_length_m shape is invalid"
            )

        if free_flow_speed_mps.shape != (
            road_count,
        ):
            raise WorldPackError(
                "free_flow_speed_mps shape is invalid"
            )

        if (
            np.any(~np.isfinite(road_lengths_m))
            or np.any(road_lengths_m <= 0)
        ):
            raise WorldPackError(
                "WorldPack road lengths must be finite and positive"
            )

        if (
            np.any(
                ~np.isfinite(
                    free_flow_speed_mps
                )
            )
            or np.any(
                free_flow_speed_mps <= 0
            )
        ):
            raise WorldPackError(
                "WorldPack free-flow speeds must be finite and positive"
            )

        if (
            sample_times_s.ndim != 1
            or len(sample_times_s) == 0
        ):
            raise WorldPackError(
                "sample_times_s must be a non-empty vector"
            )

        if np.any(
            ~np.isfinite(sample_times_s)
        ):
            raise WorldPackError(
                "sample_times_s contains non-finite values"
            )

        if np.any(
            np.diff(sample_times_s) <= 0
        ):
            raise WorldPackError(
                "sample_times_s must be strictly increasing"
            )

        snapshots = len(sample_times_s)
        expected_matrix_shape = (
            snapshots,
            road_count,
        )

        for field, array in [
            (
                "mean_speed_mps",
                mean_speed_mps,
            ),
            (
                "vehicle_count",
                vehicle_count,
            ),
            (
                "waiting_vehicle_count",
                waiting_vehicle_count,
            ),
        ]:
            if array.shape != expected_matrix_shape:
                raise WorldPackError(
                    f"{field} shape must be "
                    f"{expected_matrix_shape}; "
                    f"got {array.shape}"
                )

        if np.any(
            waiting_vehicle_count
            > vehicle_count
        ):
            raise WorldPackError(
                "waiting_vehicle_count exceeds vehicle_count"
            )

        empty = vehicle_count == 0
        occupied = ~empty

        if np.any(
            ~np.isnan(
                mean_speed_mps[empty]
            )
        ):
            raise WorldPackError(
                "Empty roads must have NaN mean_speed_mps"
            )

        occupied_speeds = (
            mean_speed_mps[occupied]
        )

        if (
            np.any(
                ~np.isfinite(
                    occupied_speeds
                )
            )
            or np.any(
                occupied_speeds < 0
            )
        ):
            raise WorldPackError(
                "Occupied roads must have finite non-negative speeds"
            )

        simulation = manifest.get(
            "simulation"
        )

        network = manifest.get(
            "network"
        )

        if not isinstance(
            simulation,
            dict,
        ) or not isinstance(
            network,
            dict,
        ):
            raise WorldPackError(
                "WorldPack manifest is missing simulation/network metadata"
            )

        if int(
            network.get(
                "road_count",
                -1,
            )
        ) != road_count:
            raise WorldPackError(
                "Manifest road_count does not match NPZ data"
            )

        if int(
            simulation.get(
                "snapshot_count",
                -1,
            )
        ) != snapshots:
            raise WorldPackError(
                "Manifest snapshot_count does not match NPZ data"
            )

        interval = float(
            simulation.get(
                "sample_interval_seconds",
                -1,
            )
        )

        if interval <= 0:
            raise WorldPackError(
                "Manifest sample interval must be positive"
            )

        if snapshots > 1 and not np.allclose(
            np.diff(sample_times_s),
            interval,
            rtol=0,
            atol=1e-6,
        ):
            raise WorldPackError(
                "Snapshot spacing does not match manifest"
            )

    def _load_app_mapping(
        self,
        *,
        source_to_moss: dict[str, int],
        worldpack_road_count: int,
    ) -> tuple[
        dict[str, tuple[int, ...]],
        WorldPackCoverage,
    ]:
        required_columns = [
            "app_edge_id",
            "sumo_edge_id",
            "status",
        ]

        try:
            edge_map = pd.read_parquet(
                self.edge_map_path,
                columns=required_columns,
            )
        except Exception as error:
            raise WorldPackError(
                "Could not read app-to-SUMO edge map"
            ) from error

        accepted = edge_map[
            (
                edge_map["status"]
                == "accepted"
            )
            & edge_map[
                "sumo_edge_id"
            ].notna()
        ].copy()

        accepted[
            "app_edge_id"
        ] = accepted[
            "app_edge_id"
        ].astype(str)

        accepted[
            "sumo_edge_id"
        ] = accepted[
            "sumo_edge_id"
        ].astype(str)

        world_sources = set(
            source_to_moss
        )

        inside = accepted[
            accepted[
                "sumo_edge_id"
            ].isin(
                world_sources
            )
        ].copy()

        app_to_moss_sets: dict[
            str,
            set[int],
        ] = {}

        for row in inside.itertuples(
            index=False
        ):
            app_id = str(
                row.app_edge_id
            )
            source_id = str(
                row.sumo_edge_id
            )
            moss_id = source_to_moss[
                source_id
            ]

            app_to_moss_sets.setdefault(
                app_id,
                set(),
            ).add(
                moss_id
            )

        app_to_moss = {
            app_id: tuple(
                sorted(
                    road_ids
                )
            )
            for app_id, road_ids
            in app_to_moss_sets.items()
        }

        reached_sources = set(
            inside[
                "sumo_edge_id"
            ]
        )

        accepted_app_edges = set(
            accepted[
                "app_edge_id"
            ]
        )

        inside_app_edges = set(
            inside[
                "app_edge_id"
            ]
        )

        coverage = WorldPackCoverage(
            worldpack_road_count=worldpack_road_count,
            worldpack_source_count=len(
                world_sources
            ),
            accepted_relation_count=len(
                accepted
            ),
            inside_relation_count=len(
                inside
            ),
            worldpack_sources_reached=len(
                reached_sources
            ),
            worldpack_sources_unreached=(
                len(world_sources)
                - len(
                    reached_sources
                )
            ),
            worldpack_source_coverage_percent=round(
                100
                * len(
                    reached_sources
                )
                / max(
                    len(world_sources),
                    1,
                ),
                3,
            ),
            accepted_app_edge_count=len(
                accepted_app_edges
            ),
            app_edges_touching_worldpack=len(
                inside_app_edges
            ),
        )

        return (
            app_to_moss,
            coverage,
        )

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise WorldPackError(
                "WorldPackService has not been loaded"
            )

    @staticmethod
    def _freeze_array(
        array: np.ndarray,
    ) -> None:
        array.setflags(
            write=False
        )

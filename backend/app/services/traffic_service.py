"""Local traffic observation imports and reproducible reliability estimates."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point

from backend.app.db.database import DatabaseManager
from backend.app.schemas.traffic import (
    EarlyDepartureBenefit,
    ReliabilityRequest,
    ReliabilityResponse,
    TrafficDataQuality,
    TrafficImportResponse,
    TrafficProfile,
    TrafficProfilesResponse,
)
from backend.app.services.graph_service import GraphService, GraphUnavailableError

LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")
NORMALIZATION_VERSION = "traffic-normalization-v1"
MODELED_PROFILE_VERSION = "modeled-weekday-v1"
MAX_IMPORT_BYTES = 50 * 1024 * 1024
PROFILE_PERIODS = {
    "weekday_morning": (5, 10),
    "weekday_afternoon": (14, 19),
}


class TrafficImportError(ValueError):
    """The supplied local file cannot produce trustworthy observations."""


class TrafficProfileNotFoundError(LookupError):
    """A requested profile does not exist in the shared database."""


class TrafficProfileMismatchError(ValueError):
    """A profile belongs to a different routing graph version."""


class TrafficService:
    """Own traffic artifacts, calibration profiles, and reliability sampling."""

    def __init__(
        self,
        database: DatabaseManager,
        graph_service: GraphService,
        traffic_path: Path,
    ) -> None:
        self.database = database
        self.graph_service = graph_service
        self.traffic_path = traffic_path
        self._to_projected = Transformer.from_crs(
            "EPSG:4326", "EPSG:32610", always_xy=True
        )

    def list_profiles(self) -> TrafficProfilesResponse:
        """Return shared versioned profiles newest first."""

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM traffic_profiles ORDER BY created_at DESC, period"
            ).fetchall()
        return TrafficProfilesResponse(profiles=[self._profile_from_row(row) for row in rows])

    def import_observations(
        self,
        *,
        filename: str,
        source_name: str,
        contents: bytes,
    ) -> TrafficImportResponse:
        """Preserve, normalize, match, and profile one user-provided data file."""

        manifest = self.graph_service.require_manifest()
        if not source_name.strip():
            raise TrafficImportError("Give this traffic source a name.")
        if not contents:
            raise TrafficImportError("The traffic file is empty.")
        if len(contents) > MAX_IMPORT_BYTES:
            raise TrafficImportError("Traffic imports must be 50 MB or smaller.")

        suffix = Path(filename).suffix.lower()
        if suffix not in {".csv", ".parquet"}:
            raise TrafficImportError("Use a CSV or Parquet traffic observation file.")

        import_id = uuid4()
        safe_filename = _safe_filename(filename, suffix)
        raw_directory = self.traffic_path / "raw" / str(import_id)
        raw_directory.mkdir(parents=True, exist_ok=False)
        raw_path = raw_directory / safe_filename
        raw_path.write_bytes(contents)
        raw_sha256 = hashlib.sha256(contents).hexdigest()

        try:
            frame = self._read_frame(raw_path, suffix)
            normalized, quality = self._normalize(frame, source_name.strip())
            if normalized.empty:
                raise TrafficImportError(
                    "No usable observations remained after validation and road matching."
                )

            normalized_directory = self.traffic_path / "normalized"
            normalized_directory.mkdir(parents=True, exist_ok=True)
            normalized_path = normalized_directory / f"{import_id}.parquet"
            normalized.to_parquet(normalized_path, index=False)

            now = datetime.now(LOCAL_TIMEZONE)
            profile_records = self._build_profiles(
                normalized=normalized,
                import_id=import_id,
                source_name=source_name.strip(),
                raw_sha256=raw_sha256,
                graph_version=manifest.graph_version,
                created_at=now,
            )
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO traffic_imports (
                        id, source_name, original_filename, raw_path, raw_sha256,
                        normalized_path, graph_version, imported_at, row_count,
                        accepted_count, rejected_count, quality_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(import_id),
                        source_name.strip(),
                        filename,
                        str(raw_path),
                        raw_sha256,
                        str(normalized_path),
                        manifest.graph_version,
                        now.isoformat(),
                        quality.row_count,
                        quality.accepted_count,
                        quality.rejected_count,
                        quality.model_dump_json(),
                    ),
                )
                for profile, stats in profile_records:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO traffic_profiles (
                            id, import_id, name, version, source_name, source_window,
                            period, graph_version, observation_count, stats_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(profile.id),
                            str(import_id),
                            profile.name,
                            profile.version,
                            profile.source_name,
                            profile.source_window,
                            profile.period,
                            profile.graph_version,
                            profile.observation_count,
                            json.dumps(stats, separators=(",", ":")),
                            profile.created_at.isoformat(),
                        ),
                    )
                connection.commit()

            stored_profiles = [
                self._profile_by_version(profile.version)
                for profile, _stats in profile_records
            ]
            self._write_manifest()
            return TrafficImportResponse(
                import_id=import_id,
                source_name=source_name.strip(),
                raw_sha256=raw_sha256,
                normalized_path=f"normalized/{normalized_path.name}",
                quality=quality,
                profiles=stored_profiles,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, TrafficImportError):
                raise
            raise TrafficImportError(
                "The traffic file could not be read or normalized. Check its schema."
            ) from error

    def simulate(self, payload: ReliabilityRequest) -> ReliabilityResponse:
        """Return deterministic percentiles and arrival-risk statistics."""

        if payload.profile_id is None:
            evidence_level = "modeled_uncalibrated"
            source_name = "Commute Help structural model"
            source_window = "No historical observations"
            profile_version = MODELED_PROFILE_VERSION
            assumptions = [
                "This is an uncalibrated stochastic model, not observed traffic.",
                "Travel-time variation is applied to the selected route's free-flow estimate.",
                "Weather, incidents, queues, and live traffic are not included.",
            ]
            seed = _stable_seed(payload, profile_version)
            generator = np.random.default_rng(seed)
            multipliers = generator.lognormal(
                mean=math.log(1.15), sigma=0.18, size=payload.sample_count
            )
            multipliers = np.maximum(multipliers, 1.0)
        else:
            profile, stats = self._profile_with_stats(payload.profile_id)
            current_graph_version = self.graph_service.require_manifest().graph_version
            if profile.graph_version != current_graph_version:
                raise TrafficProfileMismatchError(
                    "The selected profile belongs to an older road graph. Import it again for review."
                )
            evidence_level = "historically_calibrated"
            source_name = profile.source_name
            source_window = profile.source_window
            profile_version = profile.version
            assumptions = [
                "Sampling uses matched September/October weekday observations from the selected period.",
                "Observed edge speed relative to free-flow speed calibrates route-wide variation.",
                "The model does not include live incidents, weather, or guaranteed future conditions.",
            ]
            samples = np.asarray(stats.get("multiplier_samples", []), dtype=float)
            if not len(samples):
                raise TrafficProfileNotFoundError(
                    "The selected traffic profile has no reproducible samples."
                )
            seed = _stable_seed(payload, profile_version)
            generator = np.random.default_rng(seed)
            multipliers = generator.choice(samples, size=payload.sample_count, replace=True)

        travel_seconds = np.asarray(
            multipliers * payload.route.travel_time_seconds,
            dtype=float,
        )
        required_seconds = float(np.quantile(travel_seconds, payload.confidence_target))
        benefits: list[EarlyDepartureBenefit] = []
        on_time_probability: float | None = None
        latest_safe_departure: datetime | None = None
        if payload.planning_mode == "arrive_by":
            assert payload.arrival_deadline is not None
            allowance_seconds = (
                payload.arrival_deadline - payload.departure_time
            ).total_seconds() - payload.buffer_minutes * 60
            on_time_probability = float(np.mean(travel_seconds <= allowance_seconds))
            latest_safe_departure = payload.arrival_deadline - timedelta(
                seconds=required_seconds + payload.buffer_minutes * 60
            )
            for minutes in (5, 10, 15):
                probability = float(
                    np.mean(travel_seconds <= allowance_seconds + minutes * 60)
                )
                benefits.append(
                    EarlyDepartureBenefit(
                        minutes_earlier=minutes,
                        on_time_probability=probability,
                        improvement=max(0.0, probability - on_time_probability),
                    )
                )

        median_seconds = float(np.median(travel_seconds))
        p85_seconds = float(np.quantile(travel_seconds, 0.85))
        p90_seconds = float(np.quantile(travel_seconds, 0.90))
        p95_seconds = float(np.quantile(travel_seconds, 0.95))

        return ReliabilityResponse(
            planning_mode=payload.planning_mode,
            evidence_level=evidence_level,
            mean_seconds=float(np.mean(travel_seconds)),
            median_seconds=median_seconds,
            p85_seconds=p85_seconds,
            p90_seconds=p90_seconds,
            p95_seconds=p95_seconds,
            likely_low_seconds=float(np.quantile(travel_seconds, 0.10)),
            likely_high_seconds=float(np.quantile(travel_seconds, 0.90)),
            on_time_probability=on_time_probability,
            latest_safe_departure=latest_safe_departure,
            planned_departure_time=payload.departure_time,
            median_arrival_time=payload.departure_time + timedelta(seconds=median_seconds),
            p85_arrival_time=payload.departure_time + timedelta(seconds=p85_seconds),
            p90_arrival_time=payload.departure_time + timedelta(seconds=p90_seconds),
            p95_arrival_time=payload.departure_time + timedelta(seconds=p95_seconds),
            confidence_arrival_time=payload.departure_time
            + timedelta(seconds=required_seconds),
            confidence_target=payload.confidence_target,
            sample_count=payload.sample_count,
            source_name=source_name,
            source_window=source_window,
            profile_version=profile_version,
            early_departure_benefits=benefits,
            assumptions=assumptions,
        )

    @staticmethod
    def _read_frame(path: Path, suffix: str) -> pd.DataFrame:
        if suffix == ".csv":
            return pd.read_csv(path)
        return pd.read_parquet(path)

    def _normalize(
        self,
        source_frame: pd.DataFrame,
        source_name: str,
    ) -> tuple[pd.DataFrame, TrafficDataQuality]:
        frame = source_frame.copy()
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        required = {"station_or_segment_id", "timestamp_local"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise TrafficImportError(
                "Missing required traffic columns: " + ", ".join(missing) + "."
            )
        if "speed_kph" not in frame.columns and "travel_time_seconds" not in frame.columns:
            raise TrafficImportError(
                "Include speed_kph or travel_time_seconds observations."
            )

        row_count = len(frame)
        for column in (
            "source",
            "timezone",
            "direction",
            "volume",
            "speed_kph",
            "occupancy",
            "travel_time_seconds",
            "quality_flag",
            "latitude",
            "longitude",
        ):
            if column not in frame.columns:
                frame[column] = pd.NA
        frame["source"] = frame["source"].fillna(source_name).astype(str)
        frame["timezone"] = frame["timezone"].fillna("America/Los_Angeles").astype(str)
        frame["station_or_segment_id"] = frame["station_or_segment_id"].astype(str).str.strip()
        for column in (
            "volume",
            "speed_kph",
            "occupancy",
            "travel_time_seconds",
            "latitude",
            "longitude",
        ):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        frame = frame.drop(
            columns=[
                "edge_id",
                "road_name",
                "road_class",
                "edge_maxspeed_kph",
                "edge_free_flow_seconds",
                "match_distance_m",
                "match_confidence",
                "normalization_version",
            ],
            errors="ignore",
        )

        timestamps: list[datetime | None] = []
        for timestamp, timezone_name in zip(
            frame["timestamp_local"], frame["timezone"], strict=True
        ):
            timestamps.append(_parse_local_timestamp(timestamp, timezone_name))
        frame["timestamp_local"] = timestamps

        edge_lookup = self._edge_lookup()
        matches = [
            self._match_edge(row, edge_lookup)
            for _index, row in frame.iterrows()
        ]
        match_frame = pd.DataFrame(matches, index=frame.index)
        frame = pd.concat([frame, match_frame], axis=1)

        valid = (
            frame["timestamp_local"].notna()
            & frame["station_or_segment_id"].ne("")
            & frame["edge_id"].notna()
            & (
                frame["speed_kph"].gt(0)
                | frame["travel_time_seconds"].gt(0)
            )
        )
        normalized = frame.loc[valid].copy()
        normalized["timestamp_local"] = normalized["timestamp_local"].map(
            lambda value: value.isoformat()
        )
        normalized["normalization_version"] = NORMALIZATION_VERSION
        normalized["quality_flag"] = normalized["quality_flag"].fillna("").astype(str)
        normalized = normalized[
            [
                "source",
                "station_or_segment_id",
                "timestamp_local",
                "timezone",
                "direction",
                "volume",
                "speed_kph",
                "occupancy",
                "travel_time_seconds",
                "quality_flag",
                "latitude",
                "longitude",
                "edge_id",
                "road_name",
                "road_class",
                "edge_maxspeed_kph",
                "edge_free_flow_seconds",
                "match_distance_m",
                "match_confidence",
                "normalization_version",
            ]
        ]
        station_matches = frame.groupby("station_or_segment_id", dropna=False)[
            "edge_id"
        ].apply(lambda values: values.notna().any())
        quality_flags = sorted(
            {
                str(value).strip()
                for value in frame["quality_flag"].dropna()
                if str(value).strip()
            }
        )
        quality = TrafficDataQuality(
            row_count=row_count,
            accepted_count=len(normalized),
            rejected_count=row_count - len(normalized),
            matched_station_count=int(station_matches.sum()),
            unmatched_station_count=int((~station_matches).sum()),
            missing_speed_percent=_missing_percent(frame["speed_kph"]),
            missing_volume_percent=_missing_percent(frame["volume"]),
            quality_flags=quality_flags,
        )
        return normalized, quality

    def _edge_lookup(self) -> dict[str, int]:
        edges = self.graph_service.edges
        projected_edges = self.graph_service.projected_edges
        if edges is None or projected_edges is None:
            raise GraphUnavailableError("Routing graph has not been loaded.")
        if not self.graph_service.edge_id_positions:
            raise GraphUnavailableError("Routing graph edge lookup is unavailable.")
        return self.graph_service.edge_id_positions

    def _match_edge(
        self,
        row: pd.Series,
        edge_lookup: dict[str, int],
    ) -> dict[str, Any]:
        station_id = str(row["station_or_segment_id"])
        exact_position = edge_lookup.get(station_id)
        edges = self.graph_service.edges
        if exact_position is not None and edges is not None:
            return _match_record(edges.iloc[exact_position], 0.0, "high")

        latitude = row.get("latitude")
        longitude = row.get("longitude")
        projected_edges = self.graph_service.projected_edges
        edges = self.graph_service.edges
        if pd.isna(latitude) or pd.isna(longitude) or projected_edges is None or edges is None:
            return _unmatched_record()
        if not -90 <= float(latitude) <= 90 or not -180 <= float(longitude) <= 180:
            return _unmatched_record()
        x, y = self._to_projected.transform(float(longitude), float(latitude))
        indices, distances = projected_edges.sindex.nearest(
            Point(x, y), return_all=False, return_distance=True
        )
        if not len(distances) or float(distances[0]) > 500:
            return _unmatched_record()
        distance = float(distances[0])
        position = int(indices[1][0])
        target_bearing = _direction_bearing(row.get("direction"))
        if target_bearing is not None:
            nearby_positions = projected_edges.sindex.query(
                Point(x, y).buffer(max(15.0, distance + 5.0)),
                predicate="intersects",
            )
            directed_candidates = [
                int(candidate)
                for candidate in nearby_positions
                if projected_edges.iloc[int(candidate)].geometry.distance(Point(x, y))
                <= distance + 5.0
            ]
            if directed_candidates:
                position = min(
                    directed_candidates,
                    key=lambda candidate: _bearing_difference(
                        _line_bearing(projected_edges.iloc[candidate].geometry),
                        target_bearing,
                    ),
                )
                distance = float(projected_edges.iloc[position].geometry.distance(Point(x, y)))
        confidence = "high" if distance <= 50 else "medium"
        return _match_record(edges.iloc[position], distance, confidence)

    def _build_profiles(
        self,
        *,
        normalized: pd.DataFrame,
        import_id: UUID,
        source_name: str,
        raw_sha256: str,
        graph_version: str,
        created_at: datetime,
    ) -> list[tuple[TrafficProfile, dict[str, Any]]]:
        frame = normalized.copy()
        frame["parsed_timestamp"] = pd.to_datetime(frame["timestamp_local"], utc=True).dt.tz_convert(
            LOCAL_TIMEZONE
        )
        frame = frame[
            frame["parsed_timestamp"].dt.month.isin([9, 10])
            & frame["parsed_timestamp"].dt.dayofweek.lt(5)
        ]
        frame = frame[
            (frame["speed_kph"].gt(0) | frame["travel_time_seconds"].gt(0))
            & ~frame["quality_flag"].str.lower().isin({"bad", "invalid", "rejected"})
        ].copy()
        speed_multiplier = frame["edge_maxspeed_kph"] / frame["speed_kph"]
        travel_time_multiplier = (
            frame["travel_time_seconds"] / frame["edge_free_flow_seconds"]
        )
        frame["multiplier"] = speed_multiplier.where(
            frame["speed_kph"].gt(0),
            travel_time_multiplier,
        ).clip(lower=1.0, upper=5.0)

        profiles: list[tuple[TrafficProfile, dict[str, Any]]] = []
        for period, (start_hour, end_hour) in PROFILE_PERIODS.items():
            period_frame = frame[
                frame["parsed_timestamp"].dt.hour.ge(start_hour)
                & frame["parsed_timestamp"].dt.hour.lt(end_hour)
            ]
            if period_frame.empty:
                continue
            multipliers = period_frame["multiplier"].dropna().to_numpy(dtype=float)
            if not len(multipliers):
                continue
            sample_values = _bounded_profile_samples(multipliers)
            source_window = (
                f"{period_frame['parsed_timestamp'].min().isoformat()} to "
                f"{period_frame['parsed_timestamp'].max().isoformat()}"
            )
            version = hashlib.sha256(
                (
                    f"{raw_sha256}|{NORMALIZATION_VERSION}|{period}|{graph_version}"
                ).encode("utf-8")
            ).hexdigest()[:20]
            stats = {
                "normalization_version": NORMALIZATION_VERSION,
                "raw_sha256": raw_sha256,
                "mean_multiplier": float(np.mean(multipliers)),
                "median_multiplier": float(np.median(multipliers)),
                "p85_multiplier": float(np.quantile(multipliers, 0.85)),
                "p90_multiplier": float(np.quantile(multipliers, 0.90)),
                "p95_multiplier": float(np.quantile(multipliers, 0.95)),
                "multiplier_samples": sample_values,
            }
            profile = TrafficProfile(
                id=uuid4(),
                name=(
                    "September/October weekday morning"
                    if period == "weekday_morning"
                    else "September/October weekday afternoon"
                ),
                version=version,
                source_name=source_name,
                source_window=source_window,
                period=period,
                graph_version=graph_version,
                observation_count=len(multipliers),
                median_multiplier=stats["median_multiplier"],
                p85_multiplier=stats["p85_multiplier"],
                p90_multiplier=stats["p90_multiplier"],
                p95_multiplier=stats["p95_multiplier"],
                created_at=created_at,
            )
            profiles.append((profile, stats))
        return profiles

    def _profile_by_version(self, version: str) -> TrafficProfile:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM traffic_profiles WHERE version = ?", (version,)
            ).fetchone()
        if row is None:
            raise TrafficProfileNotFoundError("The traffic profile was not stored.")
        return self._profile_from_row(row)

    def _profile_with_stats(self, profile_id: UUID) -> tuple[TrafficProfile, dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM traffic_profiles WHERE id = ?", (str(profile_id),)
            ).fetchone()
        if row is None:
            raise TrafficProfileNotFoundError("The selected traffic profile no longer exists.")
        return self._profile_from_row(row), json.loads(row["stats_json"])

    @staticmethod
    def _profile_from_row(row: Any) -> TrafficProfile:
        stats = json.loads(row["stats_json"])
        return TrafficProfile(
            id=UUID(row["id"]),
            name=row["name"],
            version=row["version"],
            source_name=row["source_name"],
            source_window=row["source_window"],
            period=row["period"],
            graph_version=row["graph_version"],
            observation_count=row["observation_count"],
            median_multiplier=stats["median_multiplier"],
            p85_multiplier=stats["p85_multiplier"],
            p90_multiplier=stats["p90_multiplier"],
            p95_multiplier=stats["p95_multiplier"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _write_manifest(self) -> None:
        profiles = self.list_profiles().profiles
        manifest_path = self.traffic_path / "traffic-profile-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "normalization_version": NORMALIZATION_VERSION,
                    "profiles": [profile.model_dump(mode="json") for profile in profiles],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def _safe_filename(filename: str, suffix: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip(".-")
    return f"{stem or 'traffic-observations'}{suffix}"


def _parse_local_timestamp(value: Any, timezone_name: Any) -> datetime | None:
    try:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            return None
        if parsed.tzinfo is None:
            timezone = ZoneInfo(str(timezone_name))
            parsed = parsed.tz_localize(timezone)
        return parsed.tz_convert(LOCAL_TIMEZONE).to_pydatetime()
    except (TypeError, ValueError, KeyError):
        return None


def _match_record(edge: Any, distance: float, confidence: str) -> dict[str, Any]:
    return {
        "edge_id": str(edge["edge_id"]),
        "road_name": str(edge.get("road_name", "Unnamed road")),
        "road_class": str(edge.get("road_class", "unclassified")),
        "edge_maxspeed_kph": float(edge.get("maxspeed_kph", 35)),
        "edge_free_flow_seconds": float(edge.get("free_flow_seconds", 1)),
        "match_distance_m": round(distance, 1),
        "match_confidence": confidence,
    }


def _unmatched_record() -> dict[str, Any]:
    return {
        "edge_id": None,
        "road_name": None,
        "road_class": None,
        "edge_maxspeed_kph": None,
        "edge_free_flow_seconds": None,
        "match_distance_m": None,
        "match_confidence": "unmatched",
    }


def _missing_percent(series: pd.Series) -> float:
    if not len(series):
        return 0.0
    return round(float((series.isna() | series.le(0)).mean() * 100), 1)


def _bounded_profile_samples(values: np.ndarray) -> list[float]:
    ordered = np.sort(values)
    if len(ordered) > 10_000:
        indices = np.linspace(0, len(ordered) - 1, num=10_000, dtype=int)
        ordered = ordered[indices]
    return [round(float(value), 6) for value in ordered]


def _stable_seed(payload: ReliabilityRequest, profile_version: str) -> int:
    stable = json.dumps(
        {
            "route_id": payload.route.route_id,
            "travel_time_seconds": payload.route.travel_time_seconds,
            "planning_mode": payload.planning_mode,
            "departure_time": payload.departure_time.isoformat(),
            "arrival_deadline": (
                payload.arrival_deadline.isoformat()
                if payload.arrival_deadline is not None
                else None
            ),
            "buffer_minutes": payload.buffer_minutes,
            "confidence_target": payload.confidence_target,
            "sample_count": payload.sample_count,
            "profile_version": profile_version,
        },
        sort_keys=True,
    )
    return int.from_bytes(hashlib.sha256(stable.encode("utf-8")).digest()[:8], "big")


def _direction_bearing(value: Any) -> float | None:
    normalized = re.sub(r"[^A-Z]", "", str(value).upper())
    bearings = {
        "N": 0.0,
        "NB": 0.0,
        "NORTH": 0.0,
        "NORTHBOUND": 0.0,
        "NE": 45.0,
        "NORTHEAST": 45.0,
        "NORTHEASTBOUND": 45.0,
        "E": 90.0,
        "EB": 90.0,
        "EAST": 90.0,
        "EASTBOUND": 90.0,
        "SE": 135.0,
        "SOUTHEAST": 135.0,
        "SOUTHEASTBOUND": 135.0,
        "S": 180.0,
        "SB": 180.0,
        "SOUTH": 180.0,
        "SOUTHBOUND": 180.0,
        "SW": 225.0,
        "SOUTHWEST": 225.0,
        "SOUTHWESTBOUND": 225.0,
        "W": 270.0,
        "WB": 270.0,
        "WEST": 270.0,
        "WESTBOUND": 270.0,
        "NW": 315.0,
        "NORTHWEST": 315.0,
        "NORTHWESTBOUND": 315.0,
    }
    return bearings.get(normalized)


def _line_bearing(geometry: Any) -> float:
    start_x, start_y = geometry.coords[0]
    end_x, end_y = geometry.coords[-1]
    return (math.degrees(math.atan2(end_x - start_x, end_y - start_y)) + 360) % 360


def _bearing_difference(first: float, second: float) -> float:
    difference = abs(first - second) % 360
    return min(difference, 360 - difference)

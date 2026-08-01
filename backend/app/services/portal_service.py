"""Programmatic, bounded acquisition from the public PORTAL highway API."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

import httpx
import pandas as pd
from pyproj import Transformer

from backend.app.schemas.portal import (
    PortalAcquireRequest,
    PortalAcquireResponse,
    PortalHighway,
    PortalHighwaysResponse,
)
from backend.app.services.graph_service import GraphService, GraphUnavailableError
from backend.app.services.traffic_service import TrafficImportError, TrafficService

PORTAL_BASE_URL = "https://new.portal.its.pdx.edu"
MAX_PORTAL_RESPONSE_BYTES = 60 * 1024 * 1024


class PortalAcquisitionError(RuntimeError):
    """PORTAL could not provide a bounded, usable observation set."""


@dataclass(frozen=True)
class _Metadata:
    highways: dict[int, dict[str, Any]]
    detectors: dict[int, dict[str, Any]]
    stations: dict[int, dict[str, Any]]
    raw_highways: bytes
    raw_detectors: bytes
    raw_stations: bytes


class PortalService:
    """Fetch public PORTAL observations and feed the normal traffic importer."""

    def __init__(
        self,
        graph_service: GraphService,
        traffic_service: TrafficService,
        client: httpx.Client | None = None,
    ) -> None:
        self.graph_service = graph_service
        self.traffic_service = traffic_service
        self._client = client or httpx.Client(
            base_url=PORTAL_BASE_URL,
            timeout=httpx.Timeout(60),
            follow_redirects=True,
            headers={"User-Agent": "Commute-Help/0.1 local traffic importer"},
        )
        self._owns_client = client is None
        self._metadata: _Metadata | None = None
        self._from_web_mercator = Transformer.from_crs(
            "EPSG:3857", "EPSG:4326", always_xy=True
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def list_highways(self) -> PortalHighwaysResponse:
        """List highways having detector stations inside the loaded graph region."""

        metadata = self._load_metadata()
        graph = self.graph_service.graph
        if graph is None or not self.graph_service.ready:
            raise GraphUnavailableError("Routing graph has not been loaded.")
        longitudes = [float(data["x"]) for _node, data in graph.nodes(data=True)]
        latitudes = [float(data["y"]) for _node, data in graph.nodes(data=True)]
        bounds = (
            min(longitudes) - 0.05,
            min(latitudes) - 0.05,
            max(longitudes) + 0.05,
            max(latitudes) + 0.05,
        )
        counts: dict[int, int] = {}
        for station in metadata.stations.values():
            longitude = station.get("longitude")
            latitude = station.get("latitude")
            highway_id = station.get("highwayid")
            if (
                isinstance(highway_id, int)
                and longitude is not None
                and latitude is not None
                and bounds[0] <= longitude <= bounds[2]
                and bounds[1] <= latitude <= bounds[3]
            ):
                counts[highway_id] = counts.get(highway_id, 0) + 1
        highways = [
            PortalHighway(
                id=highway_id,
                name=str(metadata.highways[highway_id].get("highwayname", highway_id)),
                direction=str(metadata.highways[highway_id].get("direction", "unknown")),
                station_count=station_count,
            )
            for highway_id, station_count in counts.items()
            if highway_id in metadata.highways
        ]
        highways.sort(key=lambda highway: (highway.name, highway.direction, highway.id))
        return PortalHighwaysResponse(highways=highways)

    def acquire(self, request: PortalAcquireRequest) -> PortalAcquireResponse:
        """Download, join, aggregate, and import one bounded PORTAL request."""

        available = {highway.id: highway for highway in self.list_highways().highways}
        unknown = sorted(set(request.highway_ids) - set(available))
        if unknown:
            raise PortalAcquisitionError(
                "Selected PORTAL highways are outside the loaded graph region: "
                + ", ".join(str(value) for value in unknown)
            )
        metadata = self._load_metadata()
        parameters: list[tuple[str, str | int]] = [
            ("start_date", request.start_date.isoformat()),
            ("end_date", request.end_date.isoformat()),
            ("format", "json"),
            ("resolution", request.resolution),
        ]
        parameters.extend(("days_of_week", day) for day in request.days_of_week)
        parameters.extend(("highway_id", highway_id) for highway_id in request.highway_ids)
        raw_observations, observations = self._get_json(
            "/highways/api/freewaydata/",
            params=parameters,
        )
        if not isinstance(observations, list) or not observations:
            raise PortalAcquisitionError(
                "PORTAL returned no highway observations for that selection."
            )
        normalized = self._normalize(
            observations,
            metadata,
            request.resolution,
        )
        if normalized.empty:
            raise PortalAcquisitionError(
                "PORTAL observations could not be joined to active station metadata."
            )
        requested = [available[highway_id] for highway_id in request.highway_ids]
        source_name = (
            "PORTAL "
            + ", ".join(f"{highway.name} {highway.direction}" for highway in requested)
            + f" {request.start_date.isoformat()} to {request.end_date.isoformat()}"
        )
        csv_bytes = normalized.to_csv(index=False).encode("utf-8")
        try:
            import_result = self.traffic_service.import_observations(
                filename="portal-highway-observations.csv",
                source_name=source_name,
                contents=csv_bytes,
            )
        except TrafficImportError as error:
            raise PortalAcquisitionError(str(error)) from error

        raw_directory = (
            self.traffic_service.traffic_path / "raw" / str(import_result.import_id)
        )
        raw_directory.mkdir(parents=True, exist_ok=True)
        (raw_directory / "portal-freewaydata.json").write_bytes(raw_observations)
        (raw_directory / "portal-highwaymetadata.json").write_bytes(metadata.raw_highways)
        (raw_directory / "portal-detectormetadata.json").write_bytes(metadata.raw_detectors)
        (raw_directory / "portal-stationmetadata.json").write_bytes(metadata.raw_stations)
        (raw_directory / "portal-request.json").write_text(
            request.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        return PortalAcquireResponse(
            requested_highways=requested,
            downloaded_observation_count=len(observations),
            normalized_station_count=int(normalized["station_or_segment_id"].nunique()),
            import_result=import_result,
        )

    def _load_metadata(self) -> _Metadata:
        if self._metadata is not None:
            return self._metadata
        raw_highways, highway_payload = self._get_json(
            "/highways/api/highwaymetadata/"
        )
        raw_detectors, detector_payload = self._get_json(
            "/highways/api/detectormetadata/"
        )
        raw_stations, station_payload = self._get_json(
            "/highways/api/stationmetadata/"
        )
        if not isinstance(highway_payload, list) or not isinstance(detector_payload, list):
            raise PortalAcquisitionError("PORTAL highway metadata has an unexpected format.")
        features = station_payload.get("features") if isinstance(station_payload, dict) else None
        if not isinstance(features, list):
            raise PortalAcquisitionError("PORTAL station metadata has an unexpected format.")
        stations: dict[int, dict[str, Any]] = {}
        for feature in features:
            properties = dict(feature.get("properties") or {})
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coordinates) >= 2:
                longitude, latitude = self._from_web_mercator.transform(
                    float(coordinates[0]), float(coordinates[1])
                )
                properties["longitude"] = longitude
                properties["latitude"] = latitude
            station_id = properties.get("stationid")
            if isinstance(station_id, int):
                stations[station_id] = properties
        self._metadata = _Metadata(
            highways={int(item["highwayid"]): item for item in highway_payload},
            detectors={int(item["detectorid"]): item for item in detector_payload},
            stations=stations,
            raw_highways=raw_highways,
            raw_detectors=raw_detectors,
            raw_stations=raw_stations,
        )
        return self._metadata

    def _normalize(
        self,
        observations: list[dict[str, Any]],
        metadata: _Metadata,
        resolution: str,
    ) -> pd.DataFrame:
        seconds = _resolution_seconds(resolution)
        interval_to_hour = 3600 / seconds
        records: list[dict[str, Any]] = []
        for observation in observations:
            detector = metadata.detectors.get(int(observation.get("detector_id", -1)))
            if detector is None:
                continue
            station = metadata.stations.get(int(detector.get("stationid", -1)))
            highway = metadata.highways.get(int(detector.get("highwayid", -1)))
            if station is None or highway is None:
                continue
            volume = _positive_number(observation.get("volume"))
            speed_mph = _positive_number(observation.get("speed"))
            count_readings = _positive_number(observation.get("countreadings")) or 0.0
            expected_readings = max(seconds / 20, 1)
            records.append(
                {
                    "station_or_segment_id": f"portal-station-{station['stationid']}",
                    "timestamp_local": observation.get("starttime"),
                    "timezone": "America/Los_Angeles",
                    "direction": highway.get("direction"),
                    "latitude": station.get("latitude"),
                    "longitude": station.get("longitude"),
                    "speed_kph": speed_mph * 1.609344 if speed_mph else math.nan,
                    "interval_volume": volume if volume else math.nan,
                    "volume": volume * interval_to_hour if volume else math.nan,
                    "occupancy": observation.get("occupancy"),
                    "countreadings": count_readings,
                    "quality_flag": (
                        "low_sample" if count_readings < expected_readings * 0.5 else "good"
                    ),
                    "source": "PORTAL Highways API",
                }
            )
        frame = pd.DataFrame.from_records(records)
        if frame.empty:
            return frame

        def aggregate(group: pd.DataFrame) -> pd.Series:
            volumes = pd.to_numeric(group["interval_volume"], errors="coerce")
            speeds = pd.to_numeric(group["speed_kph"], errors="coerce")
            valid_weights = volumes.where(volumes.gt(0))
            if valid_weights.notna().any() and speeds.notna().any():
                aligned = speeds.notna() & valid_weights.notna()
                speed = (
                    float((speeds[aligned] * valid_weights[aligned]).sum() / valid_weights[aligned].sum())
                    if aligned.any()
                    else float(speeds.median())
                )
            else:
                speed = float(speeds.median()) if speeds.notna().any() else math.nan
            return pd.Series(
                {
                    "timezone": "America/Los_Angeles",
                    "latitude": group["latitude"].iloc[0],
                    "longitude": group["longitude"].iloc[0],
                    "speed_kph": speed,
                    "volume": pd.to_numeric(group["volume"], errors="coerce").sum(min_count=1),
                    "occupancy": pd.to_numeric(group["occupancy"], errors="coerce").mean(),
                    "quality_flag": (
                        "low_sample" if (group["quality_flag"] == "low_sample").any() else "good"
                    ),
                    "source": "PORTAL Highways API",
                }
            )

        return (
            frame.groupby(
                ["station_or_segment_id", "timestamp_local", "direction"],
                as_index=False,
            )
            .apply(aggregate, include_groups=False)
            .reset_index(drop=True)
        )

    def _get_json(
        self,
        path: str,
        params: list[tuple[str, str | int]] | None = None,
    ) -> tuple[bytes, Any]:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise PortalAcquisitionError(
                "PORTAL is unavailable or rejected the bounded data request."
            ) from error
        contents = response.content
        if len(contents) > MAX_PORTAL_RESPONSE_BYTES:
            raise PortalAcquisitionError(
                "PORTAL returned more than 60 MB. Choose fewer highways or a shorter date range."
            )
        try:
            return contents, response.json()
        except ValueError as error:
            raise PortalAcquisitionError("PORTAL returned an invalid JSON response.") from error


def _resolution_seconds(value: str) -> int:
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None

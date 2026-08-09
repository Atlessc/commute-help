"""Parse bounded SUMO XML outputs into API-safe summaries."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree


def parse_tripinfo(path: Path, vehicle_id: str) -> dict[str, float | str]:
    root = ElementTree.parse(path).getroot()
    trip = next((item for item in root.findall("tripinfo") if item.get("id") == vehicle_id), None)
    if trip is None:
        raise ValueError(f"TripInfo has no vehicle {vehicle_id}")
    return {
        "vehicle_id": vehicle_id,
        "depart": float(trip.attrib["depart"]),
        "arrival": float(trip.attrib["arrival"]),
        "duration": float(trip.attrib["duration"]),
        "route_length": float(trip.attrib["routeLength"]),
        "waiting_time": float(trip.attrib["waitingTime"]),
    }

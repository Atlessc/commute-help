"""Validation and normalization gate for agency regional OD deliveries."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd


INTAKE_SCHEMA_VERSION = 1
NORMALIZED_SCHEMA_VERSION = 1
SUPPORTED_TABLE_FORMATS = {"csv", "parquet"}
SUPPORTED_ZONE_FORMATS = {"geojson", "gpkg", "parquet", "shapefile"}
SOURCE_REDISTRIBUTION_VALUES = {"allowed", "prohibited"}
CODE_PUBLICATION_VALUES = {
    "allowed",
    "not_restricted_by_data_terms",
    "prohibited",
    "unknown",
}
DERIVED_PUBLICATION_VALUES = {"allowed", "prohibited", "unknown"}


class RegionalOdIntakeError(RuntimeError):
    """The intake package is malformed or cannot be verified safely."""


def validate_regional_od_intake(
    campaign_directory: Path,
    manifest_path: Path | None = None,
) -> tuple[pd.DataFrame | None, gpd.GeoDataFrame | None, dict[str, Any]]:
    """Verify one immutable agency delivery and normalize it when safe."""

    campaign_directory = campaign_directory.resolve()
    manifest_path = (
        manifest_path.resolve()
        if manifest_path is not None
        else campaign_directory / "campaign-manifest.json"
    )
    manifest = _read_json(manifest_path)
    _validate_manifest_shape(manifest)

    od_file = _verified_file(campaign_directory, manifest["files"]["od_table"])
    zone_file = _verified_file(campaign_directory, manifest["files"]["zones"])
    od_source = _read_table(od_file["path"], od_file["format"])
    zones_source = _read_zones(zone_file["path"], zone_file["format"])

    blocking: list[str] = []
    warnings: list[str] = []
    mapping = manifest["column_mapping"]
    required_od_mapping = {
        "origin_zone_id",
        "destination_zone_id",
        "period",
        "trip_value",
    }
    missing_mapping = sorted(required_od_mapping - set(mapping))
    if missing_mapping:
        raise RegionalOdIntakeError(
            "OD column_mapping is missing: " + ", ".join(missing_mapping) + "."
        )
    required_od_columns = {str(mapping[key]) for key in required_od_mapping}
    missing_columns = sorted(required_od_columns - set(od_source.columns))
    if missing_columns:
        raise RegionalOdIntakeError(
            "OD table is missing mapped columns: " + ", ".join(missing_columns) + "."
        )

    zone_mapping = manifest["zone_column_mapping"]
    if "zone_id" not in zone_mapping:
        raise RegionalOdIntakeError("zone_column_mapping must define zone_id.")
    zone_id_column = str(zone_mapping["zone_id"])
    if zone_id_column not in zones_source.columns:
        raise RegionalOdIntakeError(
            f"Zone file is missing mapped zone ID column {zone_id_column}."
        )

    normalized_zones = _normalize_zones(zones_source, zone_mapping, blocking, warnings)
    normalized_od = _normalize_od(od_source, manifest, mapping, blocking, warnings)
    _audit_relations(normalized_od, normalized_zones, blocking, warnings)
    license_report = _audit_license(manifest["license"], blocking, warnings)

    duplicate_key_columns = [
        "source",
        "model_version",
        "base_year",
        "period",
        "origin_zone_id",
        "destination_zone_id",
        "vehicle_class",
    ]
    duplicate_rows = normalized_od.duplicated(duplicate_key_columns, keep=False)
    duplicate_key_count = int(
        normalized_od.loc[duplicate_rows, duplicate_key_columns]
        .drop_duplicates()
        .shape[0]
    )
    if duplicate_key_count:
        blocking.append(
            f"OD table contains {duplicate_key_count} duplicate canonical keys."
        )

    negative_count = int(normalized_od["vehicle_trips"].lt(0).sum())
    invalid_trip_count = int(normalized_od["vehicle_trips"].isna().sum())
    if negative_count:
        blocking.append(f"OD table contains {negative_count} negative trip values.")
    if invalid_trip_count:
        blocking.append(
            f"OD table contains {invalid_trip_count} missing or non-numeric trip values."
        )

    boundary = manifest["boundary_overlap"]
    boundary_status = str(boundary.get("status", "unknown"))
    if boundary_status not in {"documented", "not_applicable", "unknown"}:
        raise RegionalOdIntakeError(
            "boundary_overlap.status must be documented, not_applicable, or unknown."
        )
    if boundary_status == "unknown":
        warnings.append(
            "Oregon/Washington boundary overlap is unknown; this source cannot yet be "
            "merged into a regional assignment."
        )

    blocking = list(dict.fromkeys(blocking))
    warnings = list(dict.fromkeys(warnings))
    normalization_ready = not blocking
    assignment_ready = normalization_ready and boundary_status != "unknown"
    publication_ready = bool(
        assignment_ready
        and license_report["code_publication_status"]
        in {"allowed", "not_restricted_by_data_terms"}
        and license_report["derived_output_publication_status"] == "allowed"
    )
    if normalization_ready:
        normalized_od = normalized_od.sort_values(
            [
                "period",
                "vehicle_class",
                "origin_zone_id",
                "destination_zone_id",
            ],
            ignore_index=True,
        )
        normalized_zones = normalized_zones.sort_values(
            "zone_id", ignore_index=True
        )
    else:
        normalized_od = None
        normalized_zones = None

    report = {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "generated_at": _now(),
        "campaign_id": str(manifest["campaign_id"]),
        "agency": str(manifest["agency"]),
        "model": manifest["model"],
        "status": (
            "blocked"
            if not normalization_ready
            else "assignment_ready"
            if assignment_ready
            else "validated_not_assignment_ready"
        ),
        "normalization_ready": normalization_ready,
        "assignment_ready": assignment_ready,
        "publication_ready": publication_ready,
        "blocking_issues": blocking,
        "warnings": warnings,
        "files": {
            "od_table": _file_report(od_file),
            "zones": _file_report(zone_file),
        },
        "quality": {
            "source_od_rows": int(len(od_source)),
            "source_zone_rows": int(len(zones_source)),
            "canonical_duplicate_key_count": duplicate_key_count,
            "negative_trip_value_count": negative_count,
            "invalid_trip_value_count": invalid_trip_count,
            "intrazonal_row_count": int(
                (
                    _normalized_ids(od_source[str(mapping["origin_zone_id"])])
                    == _normalized_ids(od_source[str(mapping["destination_zone_id"])])
                ).sum()
            ),
            "periods": _period_summary(normalized_od, od_source, manifest, mapping),
        },
        "boundary_overlap": boundary,
        "license": license_report,
        "limitations": [
            "A validated intake does not prove that assignment reproduces observed traffic.",
            "Zone-to-road connectors and bi-state overlap must be reviewed before regional assignment.",
            "Publication readiness is separate from permission to use data internally.",
        ],
    }
    return normalized_od, normalized_zones, report


def report_markdown(report: dict[str, Any]) -> str:
    """Render the machine-readable gate as a compact human review."""

    quality = report["quality"]
    lines = [
        "# Regional OD intake report",
        "",
        f"- Campaign: `{report['campaign_id']}`",
        f"- Agency: `{report['agency']}`",
        f"- Status: **{report['status']}**",
        f"- Normalization ready: `{str(report['normalization_ready']).lower()}`",
        f"- Assignment ready: `{str(report['assignment_ready']).lower()}`",
        f"- Publication ready: `{str(report['publication_ready']).lower()}`",
        "",
        "## Data checks",
        "",
        f"- OD rows: {quality['source_od_rows']:,}",
        f"- Zone rows: {quality['source_zone_rows']:,}",
        f"- Duplicate canonical keys: {quality['canonical_duplicate_key_count']:,}",
        f"- Negative trip values: {quality['negative_trip_value_count']:,}",
        f"- Invalid trip values: {quality['invalid_trip_value_count']:,}",
        f"- Intrazonal rows: {quality['intrazonal_row_count']:,}",
        "",
    ]
    if report["blocking_issues"]:
        lines.extend(["## Blocking issues", ""])
        lines.extend(f"- {item}" for item in report["blocking_issues"])
        lines.append("")
    if report["warnings"]:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {item}" for item in report["warnings"])
        lines.append("")
    lines.extend(
        [
            "## Release boundary",
            "",
            f"- Source redistribution: `{report['license']['source_data_redistribution']}`",
            f"- Open-source code: `{report['license']['code_publication_status']}`",
            f"- Derived outputs: `{report['license']['derived_output_publication_status']}`",
            "",
            "A passing intake is only a schema, provenance, units, geography, and permission gate. "
            "It does not promote the data to a calibrated traffic profile.",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_manifest_shape(manifest: dict[str, Any]) -> None:
    if int(manifest.get("schema_version", 0)) != INTAKE_SCHEMA_VERSION:
        raise RegionalOdIntakeError(
            f"campaign manifest must use schema_version {INTAKE_SCHEMA_VERSION}."
        )
    required = {
        "campaign_id",
        "agency",
        "received_at",
        "model",
        "files",
        "column_mapping",
        "zone_column_mapping",
        "units",
        "license",
        "boundary_overlap",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise RegionalOdIntakeError(
            "campaign manifest is missing: " + ", ".join(missing) + "."
        )
    if not {"od_table", "zones"}.issubset(manifest["files"]):
        raise RegionalOdIntakeError("files must define od_table and zones.")
    model_required = {"name", "version", "base_year"}
    if not model_required.issubset(manifest["model"]):
        raise RegionalOdIntakeError(
            "model must define name, version, and base_year."
        )


def _verified_file(campaign_directory: Path, entry: dict[str, Any]) -> dict[str, Any]:
    for key in ("path", "format", "bytes", "sha256"):
        if key not in entry:
            raise RegionalOdIntakeError(f"file entry is missing {key}.")
    relative = Path(str(entry["path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise RegionalOdIntakeError(f"unsafe campaign-relative path: {relative}.")
    path = (campaign_directory / relative).resolve()
    try:
        path.relative_to(campaign_directory)
    except ValueError as error:
        raise RegionalOdIntakeError(f"file escapes campaign directory: {relative}.") from error
    if not path.is_file():
        raise RegionalOdIntakeError(f"campaign file not found: {relative}.")
    contents = path.read_bytes()
    actual_sha256 = hashlib.sha256(contents).hexdigest()
    if len(contents) != int(entry["bytes"]):
        raise RegionalOdIntakeError(f"byte-length mismatch for {relative}.")
    if actual_sha256 != str(entry["sha256"]):
        raise RegionalOdIntakeError(f"SHA-256 mismatch for {relative}.")
    return {
        "path": path,
        "relative_path": str(relative),
        "format": str(entry["format"]).lower(),
        "bytes": len(contents),
        "sha256": actual_sha256,
    }


def _read_table(path: Path, file_format: str) -> pd.DataFrame:
    if file_format not in SUPPORTED_TABLE_FORMATS:
        raise RegionalOdIntakeError(
            f"unsupported OD table format {file_format}; use CSV or Parquet."
        )
    return pd.read_csv(path, dtype=str) if file_format == "csv" else pd.read_parquet(path)


def _read_zones(path: Path, file_format: str) -> gpd.GeoDataFrame:
    if file_format not in SUPPORTED_ZONE_FORMATS:
        raise RegionalOdIntakeError(
            f"unsupported zone format {file_format}; use GeoJSON, GeoPackage, shapefile, or Parquet."
        )
    zones = gpd.read_parquet(path) if file_format == "parquet" else gpd.read_file(path)
    if zones.crs is None:
        raise RegionalOdIntakeError("zone geography does not declare a CRS.")
    return zones.to_crs("EPSG:4326")


def _normalize_zones(
    zones: gpd.GeoDataFrame,
    mapping: dict[str, Any],
    blocking: list[str],
    warnings: list[str],
) -> gpd.GeoDataFrame:
    zone_id_column = str(mapping["zone_id"])
    result = gpd.GeoDataFrame(
        {
            "zone_id": _normalized_ids(zones[zone_id_column]),
            "zone_type": (
                zones[str(mapping["zone_type"])].fillna("unknown").astype(str).str.strip().str.lower()
                if mapping.get("zone_type")
                else "unknown"
            ),
        },
        geometry=zones.geometry,
        crs="EPSG:4326",
    )
    blank_ids = int(result["zone_id"].eq("").sum())
    duplicate_ids = int(result["zone_id"].duplicated(keep=False).sum())
    invalid_geometries = int((result.geometry.isna() | ~result.geometry.is_valid).sum())
    if blank_ids:
        blocking.append(f"Zone geography contains {blank_ids} blank zone IDs.")
    if duplicate_ids:
        blocking.append(f"Zone geography contains {duplicate_ids} rows with duplicate zone IDs.")
    if invalid_geometries:
        blocking.append(f"Zone geography contains {invalid_geometries} missing or invalid geometries.")
    recognized_types = {"internal", "external", "gateway", "special_generator"}
    unknown_types = int((~result["zone_type"].isin(recognized_types)).sum())
    if unknown_types:
        warnings.append(f"{unknown_types} zones have an unrecognized or unknown zone type.")
    if not result["zone_type"].isin({"external", "gateway"}).any():
        blocking.append("Zone geography does not identify any external or gateway zones.")
    return result


def _normalize_od(
    source: pd.DataFrame,
    manifest: dict[str, Any],
    mapping: dict[str, Any],
    blocking: list[str],
    warnings: list[str],
) -> pd.DataFrame:
    units = manifest["units"]
    trip_measure = str(units.get("trip_measure", "unknown"))
    value_basis = str(units.get("value_basis", "unknown"))
    if trip_measure != "vehicle_trips":
        blocking.append(
            "Trip measure must be documented as vehicle_trips before normalization; "
            f"received {trip_measure}."
        )
    if value_basis not in {"period_total", "vehicles_per_hour"}:
        blocking.append(
            "Trip value basis must be period_total or vehicles_per_hour before normalization."
        )

    period_aliases = {str(key): str(value) for key, value in manifest.get("period_aliases", {}).items()}
    source_period = source[str(mapping["period"])].fillna("").astype(str).str.strip()
    periods = source_period.map(lambda value: period_aliases.get(value, value))
    duration_map = {
        str(key): float(value)
        for key, value in units.get("period_duration_minutes", {}).items()
    }
    durations = periods.map(duration_map)
    missing_duration_periods = sorted(set(periods[durations.isna()]))
    if missing_duration_periods:
        blocking.append(
            "Period duration is missing for: " + ", ".join(missing_duration_periods) + "."
        )

    trip_values = pd.to_numeric(source[str(mapping["trip_value"])], errors="coerce")
    duration_values = pd.to_numeric(durations, errors="coerce")
    if value_basis == "vehicles_per_hour":
        vehicle_trips = trip_values * duration_values / 60.0
    else:
        vehicle_trips = trip_values
    rates = vehicle_trips * 60.0 / duration_values
    vehicle_class = (
        source[str(mapping["vehicle_class"])].fillna("unknown").astype(str).str.strip().str.lower()
        if mapping.get("vehicle_class")
        else pd.Series("all_motor_vehicle", index=source.index, dtype="string")
    )
    if mapping.get("vehicle_class") is None:
        warnings.append("Vehicle class is not supplied; rows are labeled all_motor_vehicle.")

    model = manifest["model"]
    return pd.DataFrame(
        {
            "normalized_schema_version": NORMALIZED_SCHEMA_VERSION,
            "source": str(manifest["agency"]),
            "campaign_id": str(manifest["campaign_id"]),
            "model_name": str(model["name"]),
            "model_version": str(model["version"]),
            "base_year": int(model["base_year"]),
            "period": periods,
            "origin_zone_id": _normalized_ids(source[str(mapping["origin_zone_id"])]),
            "destination_zone_id": _normalized_ids(source[str(mapping["destination_zone_id"])]),
            "vehicle_class": vehicle_class,
            "vehicle_trips": vehicle_trips,
            "vehicle_trip_rate_vph": rates,
            "period_duration_minutes": duration_values,
            "source_trip_measure": trip_measure,
            "source_value_basis": value_basis,
            "quality_flag": "provided_unvalidated_assignment",
        }
    )


def _audit_relations(
    od: pd.DataFrame,
    zones: gpd.GeoDataFrame,
    blocking: list[str],
    warnings: list[str],
) -> None:
    blank_origin = int(od["origin_zone_id"].eq("").sum())
    blank_destination = int(od["destination_zone_id"].eq("").sum())
    if blank_origin or blank_destination:
        blocking.append(
            f"OD table contains {blank_origin} blank origins and {blank_destination} blank destinations."
        )
    zone_ids = set(zones["zone_id"])
    orphan_origins = sorted(set(od["origin_zone_id"]) - zone_ids)
    orphan_destinations = sorted(set(od["destination_zone_id"]) - zone_ids)
    if orphan_origins or orphan_destinations:
        blocking.append(
            f"Zone foreign-key audit found {len(orphan_origins)} orphan origins and "
            f"{len(orphan_destinations)} orphan destinations."
        )
    zone_types = zones.set_index("zone_id")["zone_type"].to_dict()
    external = {zone_id for zone_id, kind in zone_types.items() if kind in {"external", "gateway"}}
    external_external = int(
        (od["origin_zone_id"].isin(external) & od["destination_zone_id"].isin(external)).sum()
    )
    if not external_external:
        warnings.append("No external-to-external OD movements are present in this delivery.")


def _audit_license(
    license_info: dict[str, Any],
    blocking: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    terms_reference = str(license_info.get("terms_reference", "")).strip()
    use_authorized = license_info.get("internal_model_use_authorized") is True
    redistribution = str(license_info.get("source_data_redistribution", "unknown"))
    code_publication = str(license_info.get("open_source_code_publication", "unknown"))
    derived_publication = str(license_info.get("derived_output_publication", "unknown"))
    if not terms_reference:
        blocking.append("License or release terms have no retained reference.")
    if not use_authorized:
        blocking.append("Internal model use is not explicitly recorded as authorized.")
    if redistribution not in SOURCE_REDISTRIBUTION_VALUES:
        blocking.append("Source-data redistribution permission is unknown.")
    if code_publication not in CODE_PUBLICATION_VALUES:
        raise RegionalOdIntakeError("Invalid open_source_code_publication value.")
    if derived_publication not in DERIVED_PUBLICATION_VALUES:
        raise RegionalOdIntakeError("Invalid derived_output_publication value.")
    if code_publication == "unknown":
        warnings.append("Open-source code publication has not been confirmed in writing.")
    if code_publication == "prohibited":
        blocking.append("Release terms prohibit the intended open-source code publication.")
    if derived_publication != "allowed":
        warnings.append("Derived-output publication is not confirmed as allowed.")
    return {
        "terms_reference": terms_reference,
        "internal_model_use_authorized": use_authorized,
        "source_data_redistribution": redistribution,
        "code_publication_status": code_publication,
        "derived_output_publication_status": derived_publication,
        "attribution": str(license_info.get("attribution", "")).strip(),
    }


def _period_summary(
    normalized: pd.DataFrame | None,
    source: pd.DataFrame,
    manifest: dict[str, Any],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    if normalized is None:
        aliases = {str(key): str(value) for key, value in manifest.get("period_aliases", {}).items()}
        values = source[str(mapping["period"])].fillna("").astype(str).map(lambda value: aliases.get(value, value))
        return {str(key): {"row_count": int(value)} for key, value in values.value_counts().sort_index().items()}
    result: dict[str, Any] = {}
    for period, frame in normalized.groupby("period", sort=True):
        result[str(period)] = {
            "row_count": int(len(frame)),
            "vehicle_trips": float(frame["vehicle_trips"].sum()),
            "vehicle_classes": sorted(frame["vehicle_class"].unique().tolist()),
        }
    return result


def _normalized_ids(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip()


def _file_report(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": entry["relative_path"],
        "format": entry["format"],
        "bytes": entry["bytes"],
        "sha256": entry["sha256"],
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RegionalOdIntakeError(f"campaign manifest not found: {path}.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegionalOdIntakeError(f"cannot read campaign manifest: {error}.") from error
    if not isinstance(value, dict):
        raise RegionalOdIntakeError("campaign manifest must be a JSON object.")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

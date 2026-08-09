"""Verify and convert a completed PORTAL campaign into bounded Parquet partitions."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
from typing import Any

import pandas as pd


FINALIZATION_SCHEMA_VERSION = 1
REQUIRED_COLUMNS = {
    "station_or_segment_id",
    "timestamp_local",
    "timezone",
    "direction",
    "latitude",
    "longitude",
    "speed_kph",
    "volume",
    "occupancy",
    "quality_flag",
    "source",
}
PROFILE_REJECT_FLAGS = {"bad", "invalid", "rejected", "low_sample"}
CHECKPOINT_INTERVAL = 25


class CampaignFinalizationError(RuntimeError):
    """The campaign cannot be safely finalized."""


def finalize_campaign(
    campaign_directory: Path,
    output_directory: Path | None = None,
    *,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    """Finalize verified chunks one at a time and return the dataset report."""

    campaign_directory = campaign_directory.resolve()
    manifest_path = campaign_directory / "campaign-manifest.json"
    manifest = _read_json(manifest_path)
    _validate_campaign(manifest, campaign_directory)
    campaign_name = str(manifest["config"]["name"])
    output_directory = (
        output_directory.resolve()
        if output_directory is not None
        else campaign_directory.parents[1] / "processed" / campaign_name
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    state_path = output_directory / "finalization-manifest.json"
    state = _load_or_create_state(state_path, manifest, manifest_path)

    chunks = sorted(
        manifest["chunks"].values(),
        key=lambda item: (
            str(item["start_date"]),
            int(item["highway_id"]),
            str(item["id"]),
        ),
    )
    processed = 0
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        prior = state["chunks"].get(chunk_id)
        if _finalized_chunk_is_valid(prior, output_directory, chunk):
            continue
        if max_chunks is not None and processed >= max_chunks:
            break
        result = _finalize_chunk(
            campaign_directory=campaign_directory,
            output_directory=output_directory,
            chunk=chunk,
        )
        state["chunks"][chunk_id] = result
        state["updated_at"] = _now()
        processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            _write_json_atomic(state_path, state)
            print(f"finalized {len(state['chunks'])}/{len(chunks)} chunks")

    if len(state["chunks"]) == len(chunks):
        state["completed_at"] = state.get("completed_at") or _now()
    else:
        state["completed_at"] = None
    _write_json_atomic(state_path, state)
    report = _build_quality_report(manifest, state, output_directory)
    report["newly_processed_chunks"] = processed
    _write_json_atomic(output_directory / "quality-report.json", report)
    _write_text_atomic(
        output_directory / "quality-report.md",
        _quality_markdown(report),
    )
    return report


def _validate_campaign(manifest: dict[str, Any], campaign_directory: Path) -> None:
    if int(manifest.get("schema_version", 0)) < 2:
        raise CampaignFinalizationError(
            "Only acquisition schema 2 campaigns contain complete daily time buckets."
        )
    expected = int(manifest.get("access_policy", {}).get("chunk_count", 0))
    chunks = manifest.get("chunks")
    if not isinstance(chunks, dict) or len(chunks) != expected:
        raise CampaignFinalizationError(
            f"Campaign has {len(chunks or {})} manifest chunks; expected {expected}."
        )
    failed = [item["id"] for item in chunks.values() if item.get("status") == "failed"]
    incomplete = [item["id"] for item in chunks.values() if item.get("status") != "complete"]
    if failed or incomplete:
        raise CampaignFinalizationError(
            f"Campaign is incomplete: {len(failed)} failed and {len(incomplete)} unfinished chunks."
        )
    partials = list(campaign_directory.rglob("*.part"))
    if partials:
        raise CampaignFinalizationError(
            f"Campaign still contains {len(partials)} partial files."
        )
    _validate_nonoverlapping_chunks(chunks.values())


def _validate_nonoverlapping_chunks(chunks: Any) -> None:
    by_highway: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        by_highway[int(chunk["highway_id"])].append(chunk)
    for highway_id, highway_chunks in by_highway.items():
        previous_end: date | None = None
        for chunk in sorted(highway_chunks, key=lambda item: str(item["start_date"])):
            start = date.fromisoformat(str(chunk["start_date"]))
            end = date.fromisoformat(str(chunk["end_date"]))
            if previous_end is not None and start <= previous_end:
                raise CampaignFinalizationError(
                    f"Highway {highway_id} has overlapping campaign chunks."
                )
            previous_end = end


def _load_or_create_state(
    state_path: Path,
    campaign_manifest: dict[str, Any],
    campaign_manifest_path: Path,
) -> dict[str, Any]:
    identity = {
        "campaign_name": campaign_manifest["config"]["name"],
        "acquisition_schema_version": campaign_manifest["schema_version"],
        "start_date": campaign_manifest["config"]["start_date"],
        "end_date": campaign_manifest["config"]["end_date"],
        "resolution": campaign_manifest["config"]["resolution"],
        "highway_ids": campaign_manifest["config"]["highway_ids"],
        "days_of_week": campaign_manifest["config"]["days_of_week"],
        "chunk_days": campaign_manifest["config"]["chunk_days"],
    }
    if state_path.exists():
        state = _read_json(state_path)
        if state.get("schema_version") != FINALIZATION_SCHEMA_VERSION:
            raise CampaignFinalizationError("Processed dataset uses an incompatible schema.")
        if state.get("source_identity") != identity:
            raise CampaignFinalizationError(
                "Processed dataset belongs to a different campaign definition."
            )
        return state
    state = {
        "schema_version": FINALIZATION_SCHEMA_VERSION,
        "created_at": _now(),
        "updated_at": _now(),
        "source_manifest": str(campaign_manifest_path),
        "source_identity": identity,
        "chunks": {},
    }
    _write_json_atomic(state_path, state)
    return state


def _finalize_chunk(
    *,
    campaign_directory: Path,
    output_directory: Path,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    normalized = chunk.get("normalized") or {}
    source_path = campaign_directory / str(normalized.get("path", ""))
    contents = source_path.read_bytes()
    source_sha256 = hashlib.sha256(contents).hexdigest()
    if len(contents) != int(normalized.get("bytes", -1)):
        raise CampaignFinalizationError(f"Size mismatch for {source_path}.")
    if source_sha256 != normalized.get("sha256"):
        raise CampaignFinalizationError(f"Checksum mismatch for {source_path}.")

    frame = pd.read_csv(BytesIO(contents), low_memory=False)
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise CampaignFinalizationError(
            f"{source_path.name} is missing columns: {', '.join(missing)}."
        )
    frame = _enrich_frame(frame, chunk)
    quality = _profile_frame(frame, chunk)
    output_records: list[dict[str, Any]] = []
    partition_year = frame["local_year"].fillna(-1).astype(int)
    partition_month = frame["local_month"].fillna(-1).astype(int)
    for (year, month), indices in frame.groupby(
        [partition_year, partition_month], sort=True
    ).groups.items():
        year_label = f"{year:04d}" if year >= 0 else "unknown"
        month_label = f"{month:02d}" if month >= 0 else "unknown"
        relative = Path(
            f"year={year_label}",
            f"month={month_label}",
            f"highway_id={int(chunk['highway_id'])}",
            f"{_safe_id(str(chunk['id']))}.parquet",
        )
        target = output_directory / "observations" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".parquet.part")
        frame.loc[indices].to_parquet(
            temporary,
            index=False,
            compression="zstd",
            engine="pyarrow",
        )
        os.replace(temporary, target)
        output_records.append(
            {
                "path": str(Path("observations") / relative),
                "rows": len(indices),
                "bytes": target.stat().st_size,
                "sha256": _sha256_file(target),
            }
        )
    return {
        "source_path": normalized["path"],
        "source_bytes": len(contents),
        "source_sha256": source_sha256,
        "rows": len(frame),
        "outputs": output_records,
        "quality": quality,
        "completed_at": _now(),
    }


def _enrich_frame(frame: pd.DataFrame, chunk: dict[str, Any]) -> pd.DataFrame:
    frame = frame.copy()
    for column in ("speed_kph", "volume", "occupancy", "latitude", "longitude"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    timestamp_text = frame["timestamp_local"].fillna("").astype(str)
    frame["timestamp_utc"] = pd.to_datetime(timestamp_text, errors="coerce", utc=True)
    local_dates = pd.to_datetime(timestamp_text.str.slice(0, 10), errors="coerce")
    clock_parts = timestamp_text.str.slice(11, 16).str.extract(r"^(\d{2}):(\d{2})$")
    hours = pd.to_numeric(clock_parts[0], errors="coerce")
    minutes = pd.to_numeric(clock_parts[1], errors="coerce")
    frame["local_date"] = local_dates
    frame["local_year"] = local_dates.dt.year.astype("Int16")
    frame["local_month"] = local_dates.dt.month.astype("Int8")
    frame["iso_weekday"] = (local_dates.dt.dayofweek + 1).astype("Int8")
    frame["minute_of_day"] = (hours * 60 + minutes).astype("Int16")
    frame["portal_highway_id"] = int(chunk["highway_id"])
    frame["campaign_chunk_id"] = str(chunk["id"])

    quality = frame["quality_flag"].fillna("").astype(str).str.strip().str.lower()
    frame["speed_valid"] = frame["speed_kph"].gt(0) & frame["speed_kph"].le(200)
    frame["volume_valid"] = frame["volume"].ge(0) & frame["volume"].le(20_000)
    frame["occupancy_valid"] = (
        frame["occupancy"].isna()
        | (frame["occupancy"].ge(0) & frame["occupancy"].le(100))
    )
    frame["profile_eligible"] = (
        frame["timestamp_utc"].notna()
        & frame["station_or_segment_id"].fillna("").astype(str).str.strip().ne("")
        & frame["speed_valid"]
        & frame["volume_valid"]
        & ~quality.isin(PROFILE_REJECT_FLAGS)
    )
    return frame


def _profile_frame(frame: pd.DataFrame, chunk: dict[str, Any]) -> dict[str, Any]:
    timestamp_text = frame["timestamp_local"].fillna("").astype(str)
    local_date_text = timestamp_text.str.slice(0, 10)
    start_date = str(chunk["start_date"])
    end_date = str(chunk["end_date"])
    duplicate_columns = ["station_or_segment_id", "timestamp_local", "direction"]
    quality_flags = Counter(
        frame["quality_flag"].fillna("").astype(str).str.strip().str.lower()
    )
    return {
        "rows": len(frame),
        "station_ids": sorted(
            set(frame["station_or_segment_id"].dropna().astype(str))
        ),
        "directions": sorted(set(frame["direction"].dropna().astype(str))),
        "dates": sorted(set(local_date_text[local_date_text.str.len() == 10])),
        "minute_buckets": sorted(
            int(value) for value in frame["minute_of_day"].dropna().unique()
        ),
        "timestamp_min": _timestamp_bound(timestamp_text, "min"),
        "timestamp_max": _timestamp_bound(timestamp_text, "max"),
        "duplicate_keys": int(frame.duplicated(duplicate_columns).sum()),
        "invalid_timestamps": int(frame["timestamp_utc"].isna().sum()),
        "outside_chunk_dates": int(
            ((local_date_text < start_date) | (local_date_text > end_date)).sum()
        ),
        "missing_station_id": int(
            frame["station_or_segment_id"].fillna("").astype(str).str.strip().eq("").sum()
        ),
        "missing_speed": int(frame["speed_kph"].isna().sum()),
        "invalid_speed": int((frame["speed_kph"].notna() & ~frame["speed_valid"]).sum()),
        "missing_volume": int(frame["volume"].isna().sum()),
        "invalid_volume": int((frame["volume"].notna() & ~frame["volume_valid"]).sum()),
        "missing_occupancy": int(frame["occupancy"].isna().sum()),
        "invalid_occupancy": int(
            (frame["occupancy"].notna() & ~frame["occupancy_valid"]).sum()
        ),
        "profile_eligible": int(frame["profile_eligible"].sum()),
        "quality_flags": dict(sorted(quality_flags.items())),
    }


def _build_quality_report(
    campaign_manifest: dict[str, Any],
    state: dict[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    expected_chunks = int(campaign_manifest["access_policy"]["chunk_count"])
    records = list(state["chunks"].values())
    totals: Counter[str] = Counter()
    stations: set[str] = set()
    directions: set[str] = set()
    dates: set[str] = set()
    minute_buckets: set[int] = set()
    quality_flags: Counter[str] = Counter()
    timestamp_min: str | None = None
    timestamp_max: str | None = None
    by_highway: dict[int, Counter[str]] = defaultdict(Counter)
    output_bytes = 0
    output_files = 0
    for chunk_id, record in state["chunks"].items():
        quality = record["quality"]
        highway_id = _highway_from_chunk_id(chunk_id)
        for key in (
            "rows",
            "duplicate_keys",
            "invalid_timestamps",
            "outside_chunk_dates",
            "missing_station_id",
            "missing_speed",
            "invalid_speed",
            "missing_volume",
            "invalid_volume",
            "missing_occupancy",
            "invalid_occupancy",
            "profile_eligible",
        ):
            totals[key] += int(quality[key])
            by_highway[highway_id][key] += int(quality[key])
        stations.update(quality["station_ids"])
        directions.update(quality["directions"])
        dates.update(quality["dates"])
        minute_buckets.update(int(value) for value in quality["minute_buckets"])
        quality_flags.update(quality["quality_flags"])
        timestamp_min = _min_optional(timestamp_min, quality["timestamp_min"])
        timestamp_max = _max_optional(timestamp_max, quality["timestamp_max"])
        output_bytes += sum(int(item["bytes"]) for item in record["outputs"])
        output_files += len(record["outputs"])

    config = campaign_manifest["config"]
    expected_dates = _expected_dates(
        date.fromisoformat(config["start_date"]),
        date.fromisoformat(config["end_date"]),
        set(int(value) for value in config["days_of_week"]),
    )
    rows = totals["rows"]
    findings: list[dict[str, Any]] = []
    if totals["duplicate_keys"]:
        findings.append(_finding("high", "duplicate_grain", totals["duplicate_keys"], rows))
    if totals["invalid_timestamps"] or totals["outside_chunk_dates"]:
        findings.append(
            _finding(
                "high",
                "invalid_or_out_of_window_timestamps",
                totals["invalid_timestamps"] + totals["outside_chunk_dates"],
                rows,
            )
        )
    for severity, name, count in (
        ("medium", "missing_or_invalid_speed", totals["missing_speed"] + totals["invalid_speed"]),
        ("medium", "missing_or_invalid_volume", totals["missing_volume"] + totals["invalid_volume"]),
        ("medium", "invalid_occupancy", totals["invalid_occupancy"]),
        ("low", "low_sample_rows", quality_flags.get("low_sample", 0)),
    ):
        if count:
            findings.append(_finding(severity, name, count, rows))
    missing_dates = sorted(expected_dates - dates)
    if missing_dates:
        findings.append(
            {
                "severity": "medium",
                "name": "missing_campaign_dates",
                "count": len(missing_dates),
                "rate_percent": round(len(missing_dates) / max(len(expected_dates), 1) * 100, 4),
                "examples": missing_dates[:20],
            }
        )

    report = {
        "schema_version": 1,
        "generated_at": _now(),
        "campaign_name": config["name"],
        "complete": len(records) == expected_chunks,
        "expected_chunks": expected_chunks,
        "finalized_chunks": len(records),
        "rows": rows,
        "profile_eligible_rows": totals["profile_eligible"],
        "profile_eligible_percent": _percent(totals["profile_eligible"], rows),
        "unique_station_count": len(stations),
        "directions": sorted(directions),
        "timestamp_min": timestamp_min,
        "timestamp_max": timestamp_max,
        "observed_date_count": len(dates),
        "expected_date_count": len(expected_dates),
        "missing_dates": missing_dates,
        "minute_of_day_bucket_count": len(minute_buckets),
        "minute_of_day_buckets": sorted(minute_buckets),
        "duplicate_keys": totals["duplicate_keys"],
        "invalid_timestamps": totals["invalid_timestamps"],
        "outside_chunk_dates": totals["outside_chunk_dates"],
        "missing_station_id": totals["missing_station_id"],
        "missing_speed": totals["missing_speed"],
        "invalid_speed": totals["invalid_speed"],
        "missing_volume": totals["missing_volume"],
        "invalid_volume": totals["invalid_volume"],
        "missing_occupancy": totals["missing_occupancy"],
        "invalid_occupancy": totals["invalid_occupancy"],
        "quality_flags": dict(sorted(quality_flags.items())),
        "source_rejected_rows": sum(
            int(item.get("rejected_rows", 0)) for item in campaign_manifest["chunks"].values()
        ),
        "source_boundary_rows_removed": sum(
            int(item.get("out_of_window_rows", 0)) for item in campaign_manifest["chunks"].values()
        ),
        "parquet_files": output_files,
        "parquet_bytes": output_bytes,
        "output_directory": str(output_directory),
        "by_highway": {
            str(highway_id): dict(sorted(counter.items()))
            for highway_id, counter in sorted(by_highway.items())
        },
        "findings": findings,
        "assumptions": [
            "Expected dates follow the configured PORTAL day-of-week filter; holidays remain included.",
            "Profile eligibility excludes missing/invalid speed or volume and bad, invalid, rejected, or low-sample flags.",
            "Cross-chunk duplicate risk is controlled by validated non-overlapping highway date ranges.",
            "This report assesses acquired detector observations before OSM directed-edge matching.",
        ],
    }
    return report


def _quality_markdown(report: dict[str, Any]) -> str:
    status = "PASS" if report["complete"] and not any(
        item["severity"] in {"critical", "high"} for item in report["findings"]
    ) else "REVIEW REQUIRED"
    lines = [
        f"# PORTAL campaign quality report — {report['campaign_name']}",
        "",
        f"**Status:** {status}",
        "",
        "## Dataset and grain",
        "",
        f"- Finalized chunks: {report['finalized_chunks']:,} / {report['expected_chunks']:,}",
        f"- Rows: {report['rows']:,}",
        f"- Candidate grain: station + timestamp + direction",
        f"- Stations: {report['unique_station_count']:,}",
        f"- Window: {report['timestamp_min']} through {report['timestamp_max']}",
        f"- Observed configured dates: {report['observed_date_count']:,} / {report['expected_date_count']:,}",
        f"- Time-of-day buckets: {report['minute_of_day_bucket_count']} / 96",
        f"- Profile-eligible rows: {report['profile_eligible_rows']:,} ({report['profile_eligible_percent']:.2f}%)",
        f"- Parquet: {report['parquet_files']:,} files, {_human_bytes(report['parquet_bytes'])}",
        "",
        "## Findings",
        "",
    ]
    if not report["findings"]:
        lines.append("No acquisition-level quality findings crossed the configured checks.")
    else:
        lines.extend(
            f"- **{item['severity'].upper()} — {item['name']}**: "
            f"{item['count']:,} ({item['rate_percent']:.4f}%)"
            for item in report["findings"]
        )
    lines.extend(
        [
            "",
            "## Integrity checks",
            "",
            f"- Duplicate grain keys: {report['duplicate_keys']:,}",
            f"- Invalid timestamps: {report['invalid_timestamps']:,}",
            f"- Rows outside logical chunk dates: {report['outside_chunk_dates']:,}",
            f"- Source metadata join rejects: {report['source_rejected_rows']:,}",
            f"- Exclusive boundary rows removed: {report['source_boundary_rows_removed']:,}",
            "",
            "## Next gate",
            "",
            "Match unique PORTAL stations to directed OSM edges with recorded distance, direction compatibility, and confidence. Do not label results historically calibrated until held-out validation passes.",
            "",
        ]
    )
    return "\n".join(lines)


def _finalized_chunk_is_valid(
    prior: dict[str, Any] | None,
    output_directory: Path,
    source_chunk: dict[str, Any],
) -> bool:
    if not prior:
        return False
    normalized = source_chunk.get("normalized") or {}
    if prior.get("source_sha256") != normalized.get("sha256"):
        return False
    for output in prior.get("outputs", []):
        path = output_directory / output["path"]
        if not path.exists() or path.stat().st_size != int(output["bytes"]):
            return False
    return bool(prior.get("outputs")) or int(prior.get("rows", 0)) == 0


def _expected_dates(start: date, end: date, portal_weekdays: set[int]) -> set[str]:
    values: set[str] = set()
    cursor = start
    while cursor <= end:
        portal_weekday = ((cursor.weekday() + 1) % 7) + 1
        if portal_weekday in portal_weekdays:
            values.add(cursor.isoformat())
        cursor += timedelta(days=1)
    return values


def _finding(severity: str, name: str, count: int, denominator: int) -> dict[str, Any]:
    return {
        "severity": severity,
        "name": name,
        "count": int(count),
        "rate_percent": _percent(count, denominator),
    }


def _timestamp_bound(values: pd.Series, operation: str) -> str | None:
    present = values[values.str.len() > 0]
    if present.empty:
        return None
    return str(present.min() if operation == "min" else present.max())


def _highway_from_chunk_id(chunk_id: str) -> int:
    match = re.match(r"^highway-(\d+)_", chunk_id)
    if match is None:
        raise CampaignFinalizationError(f"Cannot parse highway ID from {chunk_id}.")
    return int(match.group(1))


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def _min_optional(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _max_optional(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _percent(numerator: int, denominator: int) -> float:
    return round(numerator / max(denominator, 1) * 100, 4)


def _human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{value} B"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignFinalizationError(f"Cannot read {path}.") from error


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and finalize a completed PORTAL acquisition campaign."
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-chunks", type=int)
    args = parser.parse_args()
    if args.max_chunks is not None and args.max_chunks < 1:
        parser.error("--max-chunks must be a positive integer")
    return args


def main() -> None:
    args = _parse_args()
    try:
        report = finalize_campaign(
            args.campaign,
            args.output,
            max_chunks=args.max_chunks,
        )
    except CampaignFinalizationError as error:
        raise SystemExit(f"PORTAL finalization stopped safely: {error}") from error
    print(
        f"finalization safe point: {report['finalized_chunks']}/{report['expected_chunks']} "
        f"chunks, {report['rows']:,} rows, "
        f"{report['profile_eligible_percent']:.2f}% profile eligible"
    )


if __name__ == "__main__":
    main()

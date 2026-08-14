"""Plan, inspect, or explicitly materialize raw PORTAL calibration-v2 shards.

Normal invocation cannot materialize the campaign.  The campaign-wide path
requires both ``--materialize-campaign`` and ``--confirm-full-campaign``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.calibration_v2_integrity_service import (
    IntegrityReconciliationProgress,
    index_calibration_shard,
    inspect_calibration_integrity_index,
    inspect_calibration_integrity_progress,
)
from backend.app.services.portal_calibration_v2_campaign import (
    finalize_portal_calibration_corpus,
    materialize_portal_calibration_campaign,
    plan_portal_calibration_campaign,
)
from backend.app.services.portal_calibration_v2_importer import (
    PortalImportError,
    build_portal_corpus_manifest,
    import_portal_partition,
)
from backend.app.services.portal_calibration_v2_preflight import (
    measure_materialization_preflight,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="inventory all source partitions without reading raw rows")
    mode.add_argument("--status", action="store_true", help="show plan and available persistent integrity status")
    mode.add_argument("--preflight", type=Path, metavar="REPRESENTATIVE_OUTPUT", help="project full-run disk needs from existing representative shards")
    mode.add_argument("--chunk", action="append", help="promote one explicitly named source partition")
    mode.add_argument("--materialize-campaign", action="store_true", help="run every planned partition; requires confirmation")
    mode.add_argument("--finalize-corpus", action="store_true", help="fully validate shards and run one explicit final reconciliation")
    parser.add_argument("--confirm-full-campaign", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--station-mappings", type=Path)
    parser.add_argument("--station-mapping-report", type=Path)
    args = parser.parse_args()
    if args.materialize_campaign and not args.confirm_full_campaign:
        parser.error("--materialize-campaign requires --confirm-full-campaign")
    if args.confirm_full_campaign and not args.materialize_campaign:
        parser.error("--confirm-full-campaign is valid only with --materialize-campaign")
    plan = plan_portal_calibration_campaign(args.campaign, args.output)
    if args.plan:
        print(json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    if args.status:
        result = {"plan": plan.model_dump(mode="json")}
        try:
            result["integrity"] = inspect_calibration_integrity_progress(
                args.output
            ).model_dump(mode="json")
        except PortalImportError:
            result["integrity"] = None
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.preflight:
        preflight = measure_materialization_preflight(
            args.campaign,
            args.preflight,
            args.output,
        )
        print(json.dumps(preflight.as_dict(), indent=2, sort_keys=True))
        return
    if args.chunk:
        planned = {entry.source_partition_id for entry in plan.partitions}
        unknown = sorted(set(args.chunk) - planned)
        if unknown:
            parser.error(f"unknown source partition(s): {', '.join(unknown)}")
        for chunk_id in sorted(set(args.chunk)):
            result = import_portal_partition(
                args.campaign,
                chunk_id,
                args.output,
                station_mapping_path=args.station_mappings,
                station_mapping_report_path=args.station_mapping_report,
            )
            integrity = index_calibration_shard(args.output, result.shard_directory, result.manifest)
            print(f"{chunk_id}: {'reused' if result.reused_existing else 'promoted'} {result.manifest.audit.accepted_count} observations; global conflicts={integrity.summary.global_conflict_identity_count}")
        refreshed = plan_portal_calibration_campaign(args.campaign, args.output)
        summary = inspect_calibration_integrity_index(args.output).summary
        state = "conflicts_present" if summary.global_conflict_identity_count else ("duplicates_resolved_logically" if summary.global_duplicate_claim_count else "incomplete")
        corpus = build_portal_corpus_manifest(args.output, integrity=summary, materialization_state=state)
        print(f"corpus: {len(corpus.shards)}/{refreshed.expected_partition_count} shards, {corpus.observation_count} physical observations, state={corpus.materialization_state}")
        return
    if args.finalize_corpus:
        finalized = finalize_portal_calibration_corpus(
            args.campaign,
            args.output,
            progress_callback=_print_reconciliation_progress,
        )
        print(
            "corpus finalized: "
            f"{sum(entry.state == 'reusable_verified' for entry in finalized.partitions)}/"
            f"{finalized.expected_partition_count} reusable shards"
        )
        return
    updated = materialize_portal_calibration_campaign(
        args.campaign,
        args.output,
        station_mapping_path=args.station_mappings,
        station_mapping_report_path=args.station_mapping_report,
        allow_partial=args.allow_partial,
    )
    print(f"campaign execution finished: {sum(entry.state == 'reusable_verified' for entry in updated.partitions)}/{updated.expected_partition_count} reusable shards")


def _print_reconciliation_progress(
    progress: IntegrityReconciliationProgress,
) -> None:
    percent = (
        100 * progress.processed_claim_count / progress.total_claim_count
        if progress.total_claim_count
        else 100.0
    )
    eta = (
        _format_duration(progress.estimated_remaining_seconds)
        if progress.estimated_remaining_seconds is not None
        else "unknown"
    )
    print(
        "Reconciliation: "
        f"{progress.processed_claim_count:,} / "
        f"{progress.total_claim_count:,} ({percent:.1f}%); "
        f"elapsed={_format_duration(progress.elapsed_seconds)}; "
        f"rate={progress.claims_per_second:,.0f} claims/sec; "
        f"ETA={eta}",
        flush=True,
    )


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total_seconds = max(round(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{remaining_seconds:02d}s"
    if minutes:
        return f"{minutes}m{remaining_seconds:02d}s"
    return f"{remaining_seconds}s"


if __name__ == "__main__":
    main()

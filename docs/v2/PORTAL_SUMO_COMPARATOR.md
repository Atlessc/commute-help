# Phase 2.2 — PORTAL ↔ SUMO comparator

Status: implemented; awaiting Tyler's bounded accepted-smoke comparison. Outputs remain `modeled_uncalibrated` and `not_calibrated`.

## Exact comparison grain

The comparator resolves each SUMO interval start from the simulation request timestamp into `America/Los_Angeles`, then joins only the matching:

```text
variant × app edge × SUMO edge × weekday × local 15-minute bucket
```

Monday through Friday remain separate. Weekend telemetry receives no invented historical target. Phase 1.3 policy status stays attached; supplemental and insufficient evidence remain reportable but are excluded from primary metrics.

The primary historical targets are the exact-date distribution medians (`flow_vph_p50`, `speed_kph_p50`, and `slowdown_p50`). Historical P85/P90/P95 slowdown residuals remain in every row. The legacy pooled 2.650976 multiplier is not used.

## Flow semantics

PORTAL `volume` is a count crossing a detector during a 15-minute interval. SUMO edgeData distinguishes:

| Quantity | Meaning in this comparator |
|---|---|
| `entered_count` | vehicles entering the edge from another edge |
| `departed_count` | vehicles emitted directly onto this edge |
| Phase 2.1 `flow_vph` | native retained evidence: `entered_count × 4` |
| `sumo_comparable_inflow_count` | `entered_count + departed_count` |
| `sumo_comparable_flow_vph` | `(entered_count + departed_count) × 3600 / 900` |

The comparator uses edge inflow because excluding `departed_count` would erase vehicles whose route begins on the measured edge. It is still not identical to a point detector crossing, so the manifest versions this as `edge-inflow-entered-plus-departed-v1` and preserves the limitation. Phase 2.1 telemetry is never rewritten.

This is also SUMO's own detector-conversion convention: the installed SUMO 1.27.1 `tools/detector/flowFromEdgeData.py` computes edge flow as `edge.departed + edge.entered` before assigning it to detector groups. That local runtime source is the semantic evidence for comparator-v1; it is not an assumption based only on attribute names.

## Mapping contract

The mapping identity contains the graph version, SUMO network version, immutable edge-map SHA-256, and contract version `accepted-one-to-one-v1`.

- Exactly one distinct accepted SUMO edge: comparable.
- More than one accepted SUMO edge: `ambiguous_multiple_accepted`; no winner is selected.
- Review, unmatched, and absent: retained in coverage, not scored.

One-to-many segment aggregation needs a separately reviewed spatial/counting rule. Silently summing longitudinal SUMO segments would count the same traffic repeatedly.

## Metrics

- Flow WAPE = `sum(abs(SUMO comparable VPH − historical P50 VPH)) / sum(abs(historical P50 VPH))`.
- Speed MAE uses SUMO mean edge speed versus historical P50 detector-derived edge speed.
- Speed bias is `SUMO − historical`; negative means SUMO is slower.
- SUMO slowdown is graph reference speed divided by positive SUMO mean speed. Residuals are emitted against historical P50/P85/P90/P95.
- Peak timing compares the maximum historical P50 flow bucket with maximum SUMO comparable-flow bucket per edge/weekday/variant only when at least two comparable buckets exist.
- Occupancy is secondary and receives no primary residual: SUMO edge-space occupancy is not PORTAL point-detector occupancy.

No metric has a pass/fail threshold in Phase 2.2.

## Artifacts and atomicity

A successful run creates `historical-comparison-v1/` beneath the selected SUMO run:

- `comparison.parquet`
- `comparison-summary.json`
- `comparison-report.md`
- `comparison-manifest.json`

Construction occurs in `.historical-comparison-v1.pending` and promotes by directory rename only after all files, hashes, deterministic row identity, and the typed manifest are complete. Interrupted pending output is non-authoritative. Existing completed output is immutable and reused.

## Tyler acceptance commands

Build against the accepted 30-minute Phase 2.1 smoke (this does not rerun SUMO):

```bash
.venv/bin/python -m scripts.compare_sumo_to_historical \
  --run data/sumo/runs/886b4177-8250-44cf-8d7e-cf19dda6deb4
```

Inspect the bounded result without reading the Phase 1 raw corpus:

```bash
.venv/bin/python -m scripts.inspect_sumo_historical_comparison \
  --run data/sumo/runs/886b4177-8250-44cf-8d7e-cf19dda6deb4
```

Poor residuals are expected from an uncalibrated SUMO world and are diagnostic, not a Phase 2.2 implementation failure.

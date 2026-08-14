# Commute Help V2 — Technical Proposal and Methodology

**Status:** Living proposal
**Project:** `commute-help`
**Current architecture:** V2 regional “ocean” model
**Change-control rule:** Continue the current methodology and architecture unless a deliberate project decision is made to change it. External guidance from ODOT, TPAU, MPOs, universities, vendors, or other transportation-modeling experts should be documented and evaluated against the current evidence before altering the architecture.

---

## 1. Executive Summary

Commute Help V2 is a local-first traffic-simulation and route-impact analysis system designed to answer a practical question:

> Given a realistic regional traffic state, how will a planned road closure or restriction change traffic conditions, and how will that affect a specific trip?

The project separates three kinds of state:

1. **Regional baseline world** — reusable regional traffic without a selected commute or closure.
2. **Regional scenario world** — a fork of a baseline with closures, lane restrictions, speed restrictions, awareness/rerouting assumptions, or other interventions.
3. **Trip probe** — a lightweight individual-trip question injected into an existing baseline and/or scenario world.

This avoids rebuilding the entire regional simulation whenever a user changes origin, destination, or departure time.

The project is intentionally staged so that historical truth, SUMO measurement, physical measurement alignment, demand calibration, reusable baseline worlds, scenario worlds, and trip probes are validated in that order.

---

## 2. Core Architecture

```text
Historical traffic evidence
        ↓
historical calibration products
        ↓
regional_baseline
        ├── trip_probe
        └── regional_scenario
                └── trip_probe
```

### World identity rules

A **regional baseline** is identified only by inputs that change the regional traffic world.

A **regional scenario** is identified by baseline identity plus regional interventions such as closures, restrictions, timing, and awareness/rerouting assumptions.

A **trip probe** is identified by referenced world/checkpoint, origin, destination, departure time, probe configuration, routing inputs, and RNG inputs.

Origin, destination, and selected commute **must not be part of baseline/scenario identity**.

---

## 3. Governing Methodological Principles

### 3.1 Historical data is evidence, not automatic calibration truth

Raw PORTAL observations remain historical observations. Derived weekday/time profiles remain derived evidence. Nothing becomes `historically_calibrated` until it passes an explicit comparison and promotion gate.

### 3.2 Monday through Friday remain distinct

Historical traffic is represented at:

```text
edge × direction × weekday × 15-minute bucket
```

Monday through Friday are not silently pooled. Weekends are not synthesized without evidence.

### 3.3 Missing evidence is not zero traffic

Missing values are not interpreted as zero flow, zero speed, closure, or free-flow traffic. Missing evidence remains missing until an explicit fallback rule is applied.

### 3.4 Measurement semantics must match before comparison

Fields with the same name are not assumed to mean the same physical quantity. Example: PORTAL detector occupancy and SUMO edge-space occupancy are not interchangeable.

### 3.5 Provenance and determinism are first-class requirements

Promoted artifacts should preserve source identity, versions, parent digests, graph/network identity, SUMO version, algorithm version, deterministic ordering, deterministic content digest, and evidence/calibration status.

### 3.6 Partial work must never masquerade as complete work

Temporary outputs, partial intervals, interrupted compilation, and partial simulation fragments remain unpromoted. Recovery checkpoints and measurement intervals are separate concepts.

---

# 4. Phase 1 — Establish Historical Traffic Truth

## Phase 1.1 — Canonical Historical Artifact Schema

### Objective
Define strict structures for accepted historical observations, derived historical profiles, synthetic fixtures, provenance, and calibration status.

### Method
Historical observations and derived profiles are structurally separated. Neither can claim calibration merely because the source data is historical.

### Why
Real historical data can still be incomplete, transformed incorrectly, poorly mapped, or unsuitable for a particular comparison.

### Remaining gap
No major structural gap remains.

---

## Phase 1.2 — PORTAL Source Characterization and Import

### Objective
Determine the true grain and semantics of PORTAL data and create a reproducible historical corpus.

### Method
Use raw detector observations rather than normalized VPH station CSVs.

Canonical grain:

```text
detector_id × interval_start × resolution
```

For 15-minute raw volume:

```text
VPH = count × 4
```

Speed conversion:

```text
km/h = mph × 1.609344
```

Overlapping export-boundary observations are deterministically suppressed. Duplicates and conflicts are tracked separately.

### Why
Treating a normalized VPH value as a raw 15-minute count would approximately quadruple demand.

### Result
Accepted observations: **71,482,755**.

### External data that could improve this phase
- official detector/station metadata definitions
- detector installation/retirement dates
- outage and maintenance history
- lane configuration history
- quality/status flags
- official occupancy semantics
- official station aggregation rules

Potential sources: ODOT, PORTAL maintainers, TPAU, PSU/PORTAL partners.

---

## Phase 1.3 — Historical Edge/Time Compiler

### Objective
Convert accepted detector observations into reusable edge-level historical evidence.

### Method

```text
raw detectors
    ↓
station/time measurements
    ↓
app-edge/date/15-minute rows
    ↓
weekday/time distributions
```

Within a station:
- detector/lane flow is summed
- speed is positive-volume weighted
- available occupancy is averaged

For multiple longitudinal stations mapped to the same app edge:
- flow is **not summed across stations**
- edge values use the median across station measurements

Each exact historical date receives equal weight in the weekday distribution. Missing values are preserved. Zero-speed evidence is retained diagnostically.

### External data that could improve this phase
- official station grouping rules
- which station groups are longitudinal versus lane groups
- agency-approved outlier rules
- minimum sample-day policies
- seasonal normalization guidance

---

## Phase 1.3 — Historical Quality Policy

Historical support classes:

- **Direct calibration evidence:** coverage ≥ 90%
- **Supplemental evidence:** 75% ≤ coverage < 90%
- **Insufficient direct evidence:** coverage < 75%

No global sample-count threshold is currently imposed.

### External guidance that could improve this phase
- agency minimum sample-day rules
- accepted historical period selection
- seasonal validity windows
- accepted missing-data practices

---

## Phase 1.4 — Legacy P95 Tail Analysis

### Objective
Explain the legacy ~2.651 slowdown multiplier before replacing it.

### Finding
The value reflected both real recurring freeway congestion and consequential weighting/pooling choices. It was not created by zero-speed contamination or mapping leakage.

### Conclusion
Do not calibrate to a single route-wide `2.651×` slowdown value. Preserve congestion at:

```text
edge × direction × weekday × 15-minute bucket
```

---

# 5. Phase 2 — Measure and Validate SUMO Before Tuning

## Phase 2.1 — Native 15-Minute SUMO Edge Telemetry

### Objective
Measure the simulated traffic world before changing demand.

### Method
SUMO 1.27.1 mesoscopic edgeData is emitted from disposable 100-second worker children and deterministically merged into complete:

```text
SUMO edge × variant × 900-second interval
```

### Metrics
- entered count
- departed count
- left count
- arrived count
- sampled vehicle-seconds
- mean speed
- travel-time estimate
- density
- occupancy
- waiting time
- time loss

Native Phase 2.1 `flow_vph` remains:

```text
entered_count × 4
```

It is not assumed to equal PORTAL point-detector flow.

### Accepted regional smoke
- 235,721 SUMO edges
- baseline + scenario
- 942,884 rows
- 0 duplicate identities
- 0 ordering violations

### Remaining gap
edgeData measures edge-state traffic, not a historical detector cross-section.

---

## Phase 2.2 — PORTAL ↔ SUMO Comparator v1

### Objective
Create an objective scoreboard before demand tuning.

### Method
Match evidence by:

```text
variant × app edge × SUMO edge × local weekday × 15-minute bucket
```

Local time is resolved in `America/Los_Angeles`.

### Primary diagnostics
- flow WAPE
- speed MAE
- speed bias
- slowdown residuals
- mapping coverage
- corridor/direction/time breakdowns

### Initial uncalibrated result
- flow WAPE ≈ **0.75**
- speed MAE ≈ **12.9 km/h**
- speed bias ≈ **−3.94 km/h**
- slowdown P50 MAE ≈ **0.193**
- slowdown P95 MAE ≈ **0.475**

### Interpretation
The current proxy demand substantially underloads important freeway corridors. This is evidence for later demand calibration, not a comparator failure.

---

# 6. Phase 2.2a — App-Edge ↔ SUMO Topology Resolution

### Objective
Determine whether one-to-many mappings are ordinary segmentation or true ambiguity.

### Result
- one-to-many app edges: **14,271**
- proven ordered directed chains: **13,829**
- true topology ambiguity/conflict: **442**

### Method
A chain is only accepted when the selected SUMO members form one continuous directed traversal. Source row order is never trusted.

### Important flow rule
Never sum flow across sequential members of the same chain because that repeatedly counts the same vehicles.

### Potential direct-profile topology coverage
**111,787 / 112,265 = 99.57%**.

---

# 7. Phase 2.2b — Station ↔ SUMO Cross-Section Geometry

### Objective
Locate historical measurement stations on the accepted SUMO relation.

### Result
- accepted historical stations: **426**
- historical-profile participating: **426**
- topologically projectable: **425**
- non-projectable: **1**

### Method
A station is projected only onto SUMO members already belonging to the accepted app-edge relation. There is no whole-network fallback search.

Persisted evidence includes:
- SUMO member
- position along member
- position along chain
- projection distance
- boundary distance
- direction compatibility
- mapping provenance

### E1 detector investigation
Native mesoscopic E1 was tested and rejected for PORTAL-equivalent point calibration because it behaved as segment evidence rather than an independent physical cross-section detector.

---

# 8. Phase 2.2c — Custom Mesoscopic Station Crossing Counter

### Objective
Determine whether vehicle-position state can support real cross-section counts.

### Method
For a station within an edge:

```text
previous_position < station_position <= current_position
```

Crossing identity includes:

```text
vehicle × station × route occurrence
```

### Proven behavior
- same-edge crossings work
- edge-transition crossings work
- multiple stations on one edge work
- lane identity does not duplicate a physical vehicle
- authoritative direct-departure position can support direct-insertion semantics
- uninterrupted 900 seconds equals nine restartable 100-second children
- checkpoint-boundary crossing is counted exactly once

### Speed limitation
Mesoscopic position updates can be coarse. Crossing counts are defensible; detector-grade instantaneous point speed is not proven.

---

# 9. Phase 2.2d — Station Cross-Section Acceptance Policy

### Objective
Convert geometry characterization into a reproducible acceptance policy.

### Projection-distance rule

```text
<= 25 m → eligible for additional gates
> 25 m  → review_projection_distance
```

The cutoff came from physical-risk inspection, not percentile selection alone.

### Boundary rule
Within 5 m of an internal shared boundary in a proven directed chain:

```text
accepted_edge_transition
```

Within 25 m of a non-equivalent/uncertain boundary:

```text
review_boundary
```

Otherwise:

```text
accepted_within_edge
```

### Direction rule
Direction compatibility is a hard gate.

### Result
- stations: **426**
- flow accepted: **356**
- accepted within-edge: **344**
- accepted edge-transition: **12**
- review: **69**
- unmatched: **1**
- detector-grade point-speed eligible: **0**

### Direct historical profile flow coverage
**88,585 / 112,265 = 78.91%**.

### Multi-station rule
A direct historical profile is locatable only when every Phase 1 station contributing to that profile has an accepted flow cross-section. This preserves Phase 1 spatial semantics.

---

# 10. Phase 2.2e — Regional Station Observer Integration and Performance

### Objective
Determine whether correct cross-section flow measurement is operationally viable in the real regional worker.

### Method
Use only the 356 accepted flow stations.

Production observer uses:
- route-aware vehicle preselection
- subscription-backed retrieval
- road ID
- lane position
- route index

Point speed is not subscribed because the current policy does not approve detector-equivalent speed.

Observer state is promoted atomically with the existing 100-second SUMO/Python checkpoint.

### Matched 900-second regional A/B result

```text
No observer: 3668.893 s
Observer:    3709.795 s
Difference:   +40.902 s
Overhead:       1.1149%
```

Classification: **clearly operationally viable**.

### Station telemetry

```text
356 stations × 2 variants × 1 complete interval = 712 rows
```

### Regression result
The observer did not alter playback, Phase 2.1 edgeData values, traffic outcomes, route selection, or scenario separation.

---

# 11. Phase 2.2f — Actual `random_free` Departure Provenance

**Production integration complete; comparator-v2 remains a separate phase**

### Problem
Regional routes use:

```xml
departPos="random_free"
```

A directly inserted vehicle may begin upstream of a station, at the station, or downstream of it. The route request does not contain the final numeric position selected by SUMO.

### Why this matters
Unknown direct departures cannot silently become zero station crossings because that biases simulated station flow downward.

### Current conservative behavior
Unresolved direct departures are not counted. This is acceptable for performance testing, but not sufficient for primary calibration evidence.

### Approach investigated
Recover authoritative actual departure provenance from SUMO-native output rather than inferring it from a later mesoscopic observation.

Desired provenance:
- vehicle ID
- actual departure edge
- actual departure lane
- actual departure position
- actual departure time

The controlled SUMO 1.27.1 proof established that native vehroute output
contains the resolved numeric `departPos` for the execution in which insertion
occurs. Departure-step libsumo/tripinfo state can supply the actual lane and
departure time. First-observed mesoscopic position is not a valid substitute:
it is post-step vehicle state and has no contract preserving the insertion
position, even though it happened to equal insertion in the corrected fixture.

### Why this approach was selected

It preserves `departPos="random_free"`, does not alter demand or insertion
behavior, and relies on SUMO-native evidence rather than reconstructing the
random choice externally.

### Alternatives investigated and rejected

- **First observed lane position:** rejected because it is post-step state and
  is not authoritative insertion provenance.
- **Unfinished tripinfo after load-state:** rejected as original provenance
  because its `departPos` can represent the loaded/current segment position.
- **Replacing `random_free` with numeric positions:** not authorized because it
  changes insertion behavior.

### Corrected restart-equivalence finding

The initial mandatory restart-equivalence result was a false negative. The
diagnostic harness ran an uninterrupted campaign and then restarted libsumo
repeatedly inside the same Python process. Process-global native RNG/route
handler state survived those close/start cycles, so the second campaign did
not start from the claimed seed state. Its t=100 saved state showed RNG counters
`default=11, routeHandler=30`, versus `default=2, routeHandler=10` in a fresh
process.

Production uses a fresh subprocess for every 100-second child. Repeating the
fixture with one process for the uninterrupted run and one fresh process per
checkpoint child produced identical departure times and resolved positions for
all nine vehicles, including vehicles departing after reload and one unfinished
at t=900. The SUMO save/load contract is not the source of the prior mismatch.

The obsolete feasibility digest `d74814eb4861f8ebdf49903b303479dd3941ece37cb550855cc65803e70d2b73`
must not be used as validation evidence. The corrected schema-v2 feasibility
artifact has content digest
`61e53d7827eb546236dfec16a54eba5b0c5cbd042990f33fcf3f0b2fc9c7c076`.
It proves exact process-isolated equality of departure times, numeric positions,
crossing event identities, and 900-second station totals.

The accepted Phase 2.2e artifact therefore remains unchanged. Its 161 baseline
and 161 scenario unresolved departures cannot be reconstructed because native
departure provenance was not enabled and checkpointed during that run.

### Required completeness output
Each future:

```text
station × variant × 900-second interval
```

should preserve:
- crossing count
- resolved direct-departure count
- unresolved direct-departure count
- `flow_measurement_complete`

Primary comparator eligibility should require no unresolved direct-departure ambiguity unless an explicit future uncertainty policy states otherwise.

### Production method and evidence

Native tripinfo and vehroute output are enabled only when station telemetry is
requested. After the child saves SUMO state and closes libsumo, the worker:

1. parses numeric vehroute records for station-relevant direct departures;
2. accepts only records emitted by the actual departure child;
3. treats a later loaded-active `departPos=-1` as a non-authoritative
   placeholder;
4. hard-fails conflicting numeric provenance;
5. reconciles station-specific crossings and completeness;
6. writes the native fragment, Python observer state, checkpoint metadata, and
   SUMO state within the same temporary checkpoint promotion.

Rerouted regional vehicles use nested `routeDistribution` output. A bounded
smoke exposed and fixed an initial parser assumption that routes were always
direct children. With the corrected parser, both baseline and scenario
recovered numeric provenance for all five station-relevant direct departures
in the first 100 seconds, with zero unresolved station-local cases. The native
fragment was 1.9 KiB per variant. Gzipped vehroute audit output was
1.38–1.39 MiB per variant and unfinished tripinfo was about 1.30 MiB per
variant. Compute times were within prior run-to-run variation,
so the accepted one-hour +1.11% Phase 2.2e performance result did not require a
repeat.

Completed telemetry rows now carry
`resolved_direct_departure_count`,
`unresolved_direct_departure_count`, and
`flow_measurement_complete`. Uncertainty remains local to the affected station
and interval. No point-speed equivalence was promoted.

### External guidance that could improve this phase

- SUMO developer guidance on mesoscopic insertion-control RNG persistence
- a documented state variable/output exposing original resolved insertion
  provenance across reload
- a minimal upstream SUMO reproducer or patch if current state serialization
  omits required insertion-control state

External guidance remains proposed evidence until reviewed against this
fixture and the existing checkpoint contract.

### Downstream dependency and next validation gate

The Phase 2.2f production capture gate was satisfied at that phase boundary.
Comparator-v2 could then use only station rows whose
`flow_measurement_complete` is true; its implementation and acceptance remained
separate Phase 2.2g work.

---

# 12. Phase 2.2g — PORTAL ↔ SUMO Comparator v2

**Complete. Comparator v2 is accepted as the FLOW calibration scoreboard. Its
production evidence remains `modeled_uncalibrated` / `not_calibrated`.**

### Objective

Create the calibration scoreboard that compares historical and simulated FLOW
at the same physical station/cross-section grain without tuning the model.

Target relationship:

```text
PORTAL historical station
            ↕
SUMO virtual station crossing measurement
```

### Approach

The primary comparison identity is:

```text
variant × station × Portland-local weekday × 15-minute bucket
```

Historical station flow preserves Phase 1 semantics:

```text
detector/lane measurements within station/date/bucket
    ↓ sum
station/date/bucket flow
    ↓ equal-weight exact-date distribution
station weekday/bucket flow distribution
```

SUMO station flow is:

```text
crossing_count × 3600 / 900
```

The baseline primary scoreboard uses the station historical P50 and the
physically aligned SUMO crossing flow. Scenario rows remain diagnostic and are
never pooled into baseline calibration metrics.

App-edge comparison deliberately does **not** use
`median(station weekday P50)`. Phase 1 first took the station median on each
exact date and only then formed the weekday distribution; medians and quantiles
across dimensions are not generally commutative. Comparator v2 therefore uses
the authoritative Phase 1 app-edge P50 on the historical side and the median of
the exact same contributing SUMO station identities for the simulated interval.
Every contributing Phase 1 station must have accepted, complete SUMO evidence;
a partial station median remains diagnostic only.

### Why this approach was selected

Comparator v1 used whole-edge inflow `(entered + departed) × 4`, which was a
useful diagnostic but not a PORTAL point/cross-section measurement. Phases
2.2a–2.2f established accepted station geometry, positional crossing semantics,
checkpoint-safe collection, and actual `random_free` departure provenance.
Comparator v2 consumes that evidence rather than extending the edge-flow
approximation.

### Historical station artifact and round-trip evidence

Phase 1 created station/date values internally but persisted only later
app-edge products. Comparator v2 therefore reconstructs and persists a separate
versioned historical station-flow artifact from the frozen corpus. Temporary
station/date partitions are bounded scratch data and are removed only after
successful atomic promotion.

Before production scoring, the reconstruction must pass this hard round trip:

```text
frozen detector corpus
    ↓ detector sum within station/date/bucket
station/date rows
    ↓ median across stations for app-edge/date/bucket
reconstructed app-edge/date rows
    ↓ equal-date weekday distribution
reconstructed app-edge profiles
    ↕ exact/numerically equivalent comparison
accepted Phase 1.3 date rows and profiles
```

The mapped-observation count must equal **62,895,977**. Missing identities,
station-membership/count differences, and flow/volume numeric differences above
the deterministic tolerance block comparison promotion rather than rewriting
either side.

### Primary eligibility and metrics

A station row is primary FLOW evidence only when historical station and
app-edge support are direct, the Phase 2.2d mapping is accepted, a complete
900-second SUMO row exists, station-local departure uncertainty is zero, and
all artifact provenance agrees. Excluded rows remain materialized with explicit
reasons.

Primary metrics are FLOW WAPE (ratio of absolute-error and historical-volume
sums), MAE, and signed SUMO-minus-historical bias. Reports retain numerator,
denominator, error/ratio distributions, high-volume slices, and weekday,
time-bucket, direction, corridor, and variant breakdowns. No promotion
threshold is introduced here.

### Alternatives investigated or rejected

- **Whole-edge inflow:** retained only as Comparator-v1 history; rejected as the
  primary physical station metric.
- **Median of station profile medians:** rejected because it does not reproduce
  Phase 1's date-first aggregation order.
- **Partial station median:** retained only as a labeled diagnostic; rejected
  for primary scoring.
- **Point speed:** remains ineligible because detector-equivalent mesoscopic
  point speed has not been proven.
- **Missing-as-zero:** rejected; missing historical or SUMO evidence remains
  missing and explicitly excluded.

### Current limitations and data gaps

- Phase 2.2d leaves 69 station projections in review and one unmatched.
- Detector-grade point speed remains unavailable.
- Historical station profiles are a reconstructed derived intermediate rather
  than an original Phase 1 output, hence the mandatory round-trip gate.
- One 15-minute simulation window measures only that run window; it must not be
  presented as source-wide historical coverage.

Official detector/station aggregation guidance, outage history, lane history,
and agency screenline/corridor calibration procedures could improve later
validation. Such guidance remains proposed evidence until explicitly approved.

### Downstream dependency and next gate

The production station telemetry, historical round trip, atomic Comparator-v2
artifact, and scoreboard review gates have passed. Phase 2.3 is unblocked to
implement lifecycle/promotion machinery and visible failure reasons. The
gameplan explicitly forbids inventing final numeric thresholds in that phase.
Comparator v2 defines the ruler; it has not declared the current model fit for
promotion.

### Completed-checkpoint telemetry recovery

The first Phase-2.2f-compatible 900-second producer completed and atomically
promoted all nine baseline and nine scenario checkpoints, then failed the
selected-trip free-flow diagnostic before final telemetry assembly. Source
inspection established that station telemetry materialization occurs after
that diagnostic even though it depends only on the already-promoted station and
departure-provenance checkpoint fragments.

The failure remains authoritative at the run-wrapper level. The selected trip
had not arrived at the 900-second simulation boundary, so its provisional
duration was 900 seconds against a SUMO route free-flow value of 1080.272
seconds and a minimum allowed value of 1047.864 seconds. Its initial and final
route hashes also differ. This is classified as a truncated selected-trip
diagnostic, not proof of an impossibly fast completed traversal. The free-flow
gate is neither removed nor weakened.

A separate `promoted-checkpoint-chain-v1` recovery gate may finalize station
FLOW evidence without resimulation. It requires exactly 18 contiguous promoted
checkpoints through second 900; validates child-request parentage, checkpoint
metadata, native edge windows, station fragments, departure-fragment internal
digests, Python observer state, SUMO/network/demand/seed/policy/observation-plan
identity, and final dense station identities; and then calls the same production
station telemetry materializer. The original failed `result.json` remains
unchanged. A recovery lineage record is promoted atomically beside the normal
station Parquet and manifest and explicitly records the failed wrapper status,
checkpoint hashes, recovery reason, and selected-trip diagnostic.

This separation was selected because station crossing evidence and the selected
trip are independent consumers of the same completed simulation. Discarding the
station evidence would require an identical expensive resimulation without
improving its physical meaning; marking the whole run successful would erase a
valid diagnostic failure. Comparator v2 may consume failed-wrapper telemetry
only when this recovery lineage validates against the normal telemetry digest.

### Production artifact lineage and round-trip result

The accepted Comparator-v2 artifact is:

```text
producer: portal_sumo_station_flow_comparator / phase-2.2g-v1
algorithm: station-cross-section-flow-v1
eligibility policy: direct-complete-all-stations-v1
content digest: 09f3f0cfbb8ec797e3d1e9536ab19eda34fbe37afc279cfcfee32327e51f36c9
```

Its principal inputs are:

```text
historical corpus: 09a80fc2e419db11d19925fa078e370afccba4469af476bc1da0e4707844a37b
historical integrity: 4aed0b90c157f49c76469aa5a00e23b2b99efa6f02a570a69602c7a485c18d31
Phase 1.3 profiles: 8bfa6d1032f5b505d3681caec26f63386288f8ae03f020b8c74de93b74ffea9f
Phase 1.3 quality policy: c5771eaa089ab2356fcb1a0fbfa3747ae563382f5bf43f8e971b0f1f7557502c
station policy: 5a5506a26997be4fbebbec8a11569e62a13744ddb13e3ec9e861dc356a349ef2
station observation plan: e67b81ac3bd8ca2d653c77a71ef3879f658d0a469a32129daecc2c75f94e4e46
station telemetry: ed2ed2ad913e3911903437de81c7880f70bf9d744ff287ec964b7f0081eace73
SUMO run: bf4bdb3a91d67fbc92da21159b3321ca7fb3538deeb189910554b78c2809c039
network: pv-sumo-2026-08-08-v1
```

The reconstructed historical station artifact contains **201,758** profiles
and has content digest
`fd270a0f7e987ec178cceefdadfdc018174e635406e1dbace88e3e47cc194b3f`.
All **62,895,977** accepted mapped observations reconciled. Reapplying the
Phase 1 aggregation order reproduced all **16,961,000** accepted app-edge/date
rows and all **140,704** weekday profiles exactly or numerically equivalently:
zero mismatches, zero missing identities on either side, and maximum reported
flow and volume differences of `0.0`. This closes the risk that station-level
reconstruction or a changed aggregation order manufactured the comparison.

### Coverage and production scoreboard

Source-wide coverage and this one-run comparison window remain deliberately
separate:

- source-wide: 140,704 candidate app-edge profiles, 112,265 direct-evidence
  profiles, 88,585 direct profiles with every contributing station physically
  locatable, 356 FLOW-accepted stations, and 201,758 historical station
  profiles;
- Monday 17:00 run window: 852 station comparison rows and 584 app-edge
  comparison rows across baseline and scenario;
- baseline primary station score: 288 of 426 rows, WAPE `0.855896`, MAE
  `2493.493056 VPH`, and SUMO-minus-PORTAL bias `-2488.854167 VPH`;
- baseline primary app-edge score: 176 of 292 rows, WAPE
  `0.8538030991836535`, MAE `2381.7613636363635 VPH`, bias
  `-2381.7613636363635 VPH`, numerator `419,190 VPH`, and denominator
  `490,968 VPH`.

The equality of app-edge MAE and the magnitude of the negative signed bias
means every primary app-edge error in this slice is non-positive: simulated
flow never exceeds the corresponding historical target. Direction WAPE is
`0.918088` eastbound, `0.905820` northbound, `0.731112` southbound, and
`0.897713` westbound; median simulated/historical ratios are approximately
`0.0543`, `0.0689`, `0.1859`, and `0.0808`, respectively. Severe negative bias
also appears across I-5, I-205, I-84/US-30, US-26, OR-217, WA 14, WA 500,
the Interstate Bridge, and the Glenn L. Jackson Memorial Bridge. Marquam Bridge
has no primary row in this slice and therefore no claimed score.

Comparator v1's WAPE was `0.7495723699`, but numeric equivalence is neither
required nor expected: v1 measured approximate whole-edge entered-plus-departed
flow, while v2 measures physical station cross-section crossings and mirrors
Phase 1's station-to-app-edge aggregation.

### Accepted conclusion and remaining limitations

Comparator v2 is accepted as a trustworthy FLOW measurement/comparison ruler.
The current Monday-at-17:00 regional baseline is severely and systematically
underloaded relative to PORTAL station cross-sections. All reported integrity
counters are zero, so this result is not presently attributable to historical
round-trip error, changed Phase 1 aggregation order, whole-edge comparator
semantics, accepted-station projection ambiguity, mesoscopic E1 behavior,
checkpoint duplication, unresolved `random_free` departures, partial station
aggregation, scenario leakage, or weekend leakage.

This conclusion applies only to **Monday at local 17:00**
(`bucket_start_minute=1020`). It does not characterize every weekday or time
of day. Point speed remains primary-ineligible. The selected trip's truncated
free-flow diagnostic remains separately open, and the recovered FLOW artifact
does not rewrite that failed wrapper result. Regional loading, demand, and
assignment are now the unresolved model layer; no demand, assignment,
capacity, speed, or promotion tuning has occurred. Neither WAPE nor the
simulated/historical ratios are adopted as a tuning multiplier.

---

# 13. Phase 2.3 — Promotion Gates

**Implemented and validated. The promotion mechanism is complete; final
production traffic-accuracy thresholds remain intentionally unresolved.**

### Objective
Implement explicit lifecycle states such as `candidate`,
`calibration_passed`, `validation_passed`, `promoted`, and `rejected`; preserve
visible failure reasons; and prove with synthetic fixtures that failure cannot
promote while a passing fixture can. The gameplan does not authorize final
numeric thresholds in this phase.

Potential metrics:
- flow WAPE
- speed MAE/bias where semantically appropriate
- corridor/screenline error
- direction error
- time-of-day error
- peak timing
- slowdown-distribution residuals

### Rule
Do not invent thresholds merely to make the current model pass.

### Implemented lifecycle contract

Phase 2.3 uses the typed `explicit-lineage-state-machine-v1` contract produced
by `calibration_promotion_gate / phase-2.3-v1`. A decision records:

- candidate kind and immutable artifact identity;
- parent and source artifact lineage;
- Comparator name, version, algorithm, digest, measurement semantics, evidence
  level, and calibration status;
- criteria policy name, version, status, definitions, and results;
- decision author, producer, timestamp, and deterministic content digest;
- previous decision digest, explicit from/to states, and decision kind;
- machine-readable failure codes plus human-readable details.

Regional-world candidate identity has no trip origin, destination, selected
trip, or trip departure fields. The strict schema rejects extra identity
fields, preserving the ocean/droplet boundary.

Every state change creates a new atomic directory containing:

```text
promotion-decision.json
promotion-manifest.json
```

The manifest hashes the decision file and carries the decision, candidate,
prior-decision, and resulting-state identities. An existing artifact is reused
only when its deterministic decision digest is identical; another decision
cannot overwrite it. Interrupted staging removes only the derived pending
directory. A promoted record is terminal and immutable; reevaluation starts a
new decision chain/version.

### Legal transitions

The enforced state machine is:

```text
initial registration -> candidate
candidate -> calibration_passed
calibration_passed -> validation_passed
validation_passed -> promoted

candidate | calibration_passed | validation_passed -> rejected
```

Direct `candidate -> promoted` and `calibration_passed -> promoted` transitions
are illegal. Both `promoted` and `rejected` are terminal. In particular,
`rejected -> promoted`, `promoted -> candidate`, and `promoted -> rejected`
hard-fail with `illegal_state_transition` rather than writing a misleading
artifact.

Successful calibration and validation transitions require the complete set of
criteria for that phase, results consistent with the criterion definitions,
complete measurement evidence, passing integrity, and unchanged candidate,
evidence, comparator, and policy lineage. Promotion authorization carries no
new metric result: it relies on the linked calibration and validation decisions
already evaluated in the chain.

### Failure semantics

The versioned failure vocabulary includes:

```text
calibration_metric_failed
validation_metric_failed
missing_required_evidence
incompatible_lineage
incomplete_measurement
integrity_failure
unsupported_comparator
illegal_state_transition
production_criteria_unresolved
```

Rejected decisions require at least one persisted failure reason. A referenced
criterion must belong to the applied policy. Illegal requests that cannot form
a valid decision raise a typed failure and cannot create an output artifact.
There is no generic hidden false boolean.

### Synthetic acceptance evidence

The passing and failing policies are static synthetic fixtures. Their numeric
criteria are explicitly `fixture_local`, their criterion IDs begin with
`synthetic.`, and the schema permits them to evaluate only a
`synthetic_fixture` candidate. They cannot be attached to a calibration artifact
or regional production world.

The passing fixture traverses and links every required state:

```text
candidate -> calibration_passed -> validation_passed -> promoted
```

The deliberately failing fixture persists
`calibration_metric_failed`, ends `rejected`, and cannot subsequently promote.
Additional tests prove that missing/incomplete evidence, failed integrity,
incompatible lineage, incomplete criterion sets, illegal transitions, and
mutation of terminal decisions cannot produce promotion. Identical evaluation
is idempotent; changing candidate identity changes decision identity; atomic
interruption leaves no promoted or pending artifact.

Validation completed with 14 focused lifecycle tests, 58 combined promotion
and lineage/artifact tests, and 235 full backend tests. Ruff and whitespace
checks passed. No SUMO run, historical reconstruction, Comparator-v2 rerun, or
traffic tuning was performed.

### Current production Comparator-v2 registration

The accepted Comparator-v2 artifact was evaluated only through the
threshold-free registration gate and materialized separately at:

```text
data/sumo/runs/phase-2-2g-900s-observer/promotion-decisions/
  comparator-v2-baseline-candidate-v1/
```

Its decision is:

```text
candidate digest: 09f3f0cfbb8ec797e3d1e9536ab19eda34fbe37afc279cfcfee32327e51f36c9
state: candidate
criteria policy: traffic_accuracy_criteria_pending_review /
                 phase-2.3-unresolved-v1
criteria policy status: unresolved_production
criteria count: 0
decision digest: b602b6361df5778e2b4c54895954ea2b47a1d603df3ec5859ca8265510d0781b
manifest digest: 4a52fc97173e3b075372a350794d6aae9f8c5a21eba38cd525d1b69cb9fe735b
evidence level: modeled_uncalibrated
calibration status: not_calibrated
```

This is mechanism evidence, not a traffic-model pass. The unresolved production
policy is structurally forbidden from containing thresholds and cannot advance
to `calibration_passed`. Comparator-v2's poor Monday 17:00 scores were neither
copied into a fixture nor converted into a permissive production rule.

### Inputs, immutable evidence, and next gate

The promotion mechanism may consume versioned Comparator-v2 metrics and future
approved policy criteria. It must not rewrite the frozen historical corpus,
Phase 1 profiles/policies, station mapping/policy, observer/departure evidence,
recovered station telemetry, Comparator-v1 history, or the accepted
Comparator-v2 artifact. A promotion decision is a new lineage-bearing record,
not a mutation of evidence.

The gameplan does not require another SUMO weekday/time slice before Phase 2.3
or before defining the Phase 3.1/3.2 demand contracts. Broader time evidence is
introduced by Phase 3.3's bounded snapshot-versus-dynamic experiment (initially
a high-value window such as 04:00–10:00), followed by progressively longer
world candidates and later Monday–Friday acceptance. The present single-slice
limitation must remain visible and cannot support a whole-day or whole-week
promotion claim.

Phase 3 is unblocked only after the Phase 2.3 focused and full-backend gates are
green. Its authorized changes remain limited to the dynamic demand/assignment
layer: weekday and 15-minute OD demand, zone and vehicle-class targets,
trips/VPH and evidence metadata, temporal regularization, within-bucket
departure placement, balanced stochastic rounding, and later bounded path sets
and probabilities. Network geometry, capacities, speed limits, signals,
vehicle behavior, mesoscopic mode, checkpoint cadence, station mappings,
historical artifacts, comparator semantics, and closure behavior remain outside
that phase's authority.

### High-value ODOT/TPAU guidance
- Oregon calibration acceptance criteria
- preferred validation metrics
- GEH/WAPE/MAE expectations
- screenline/corridor procedures
- peak-period validation methodology
- count weighting rules
- planning-model versus operations-model standards

---

# 14. Phase 3 — Dynamic 15-Minute Demand Calibration

### Objective
Replace proxy demand with historically supported dynamic regional demand.

### Planned method
Use the comparator scoreboard to adjust demand rather than intuition.

Demand should vary by:
- weekday
- 15-minute bucket
- direction
- OD behavior where supported

### Major data gap
Detector counts tell us where vehicles were observed but do not uniquely determine:

```text
origin → destination
```

An OD prior is therefore required.

### Highest-value external data
If shareable from ODOT/TPAU/MPO partners:
- OD matrices/trip tables
- TAZ definitions
- travel-demand model outputs
- synthetic population/trip generation products
- employment/population inputs
- trip-purpose distributions
- time-of-day factors
- freight/truck matrices
- external/visitor trips
- bridge screenline volumes
- assignment seed matrices
- transit-diversion assumptions
- work-from-home assumptions

These inputs could greatly reduce inverse inference from counts alone.

## Phase 3.1 — Dynamic OD contract

### Objective

Define the evidence and identity contract for weekday-specific, 15-minute
regional OD matrices before any SUMO demand file is generated or any demand is
tuned.

### Approach and artifact identity

The typed `commute_help_dynamic_od_time_series` schema uses producer
`historical_dynamic_od_contract_compiler / phase-3.1-v1` and algorithm
`evidence-anchored-dynamic-od-v1`. Each row identifies:

- Portland local Monday–Friday weekday;
- 15-minute `bucket_start_minute` with `[start, end)` semantics;
- origin and destination zone IDs and zone types;
- the explicit gateway-aware movement class derived from those zone types;
- vehicle class;
- target vehicle trips and the exact corresponding VPH value;
- immutable evidence references; and
- bucket-specific evidence supporting a change from the preceding matrix.

The artifact identity includes legitimate regional-world inputs: demand
version, weekday/buckets, OD targets, vehicle classes, evidence lineage,
assignment policy, and RNG policy. It excludes selected-trip origin,
destination, departure, and probe identity. Those remain questions asked of a
regional world, not causes of that world.

The schema carries a deterministic semantic content digest that excludes only
`generated_at`; each OD row also has a deterministic identity at the physical
weekday/bucket/zone-pair/vehicle-class grain. Writes use atomic replacement.

### Why this approach was selected

Comparator-v2 proves the current Monday 17:00 world is severely underloaded,
but WAPE, MAE, signed bias, and station/corridor ratios do not identify the OD
matrix that caused those counts. A global multiplier would conceal origin,
destination, gateway, vehicle-class, and time structure. The contract therefore
keeps those dimensions explicit and leaves inference/calibration to later
bounded phases.

### Evidence and lineage

Every artifact must reference immutable evidence that includes the frozen
Phase 1.2 observation corpus, Phase 1.3 historical flow profiles, and Phase 1.3
quality policy. The synthetic two-hour gate uses the accepted digests:

- corpus: `09a80fc2e419db11d19925fa078e370afccba4469af476bc1da0e4707844a37b`;
- profiles: `8bfa6d1032f5b505d3681caec26f63386288f8ae03f020b8c74de93b74ffea9f`;
- quality policy: `c5771eaa089ab2356fcb1a0fbfa3747ae563382f5bf43f8e971b0f1f7557502c`.

Additional zone-system, gateway-inventory, regional-OD-prior, vehicle-class,
and bucket-specific detector evidence are separately typed. A row cannot cite
an unknown evidence identity.

### Conservation, RNG, and 1:1 semantics

Each weekday/bucket matrix retains the same explicit OD/vehicle-class keys,
including explicit zero targets when appropriate. Per-bucket totals must equal
the sum of row targets and the sum of gateway-aware movement-class totals.
For a 900-second bucket:

```text
target_flow_vph = target_vehicle_trips × 4
```

The contract fixes one SUMO vehicle per modeled vehicle trip and carries a
base seed plus deterministic seed-derivation policy. Phase 3.1 does not sample,
round, place departures, choose connectors, or assign paths; those operations
remain explicitly deferred to Phase 3.2.

### Temporal regularization and fallback policy

Phase 3.1 does not invent a numerical definition of a “wild” matrix change.
Instead, `evidence-anchored-adjacent-matrix-v1` applies a stronger provenance
rule: a changed target in an adjacent 15-minute matrix must cite
bucket-specific detector evidence, and any direction reversal is governed by
the same evidence requirement. The production numerical change threshold is
explicitly unresolved rather than hidden in a fixture.

Missing buckets are rejected, not interpolated. Weekday pooling, weekday
substitution, season substitution, exact-date substitution, and missing-bucket
interpolation are all explicitly prohibited in contract v1. No weekend can be
synthesized.

### Validation evidence

The deterministic synthetic gate covers Monday 07:00–09:00 as eight complete
15-minute matrices. It contains internal→internal, gateway→internal,
internal→gateway, and gateway→gateway movements plus passenger and heavy-truck
classes. Tests prove conservation, units, immutable lineage, evidence-gated
adjacent changes, no implicit fallback, selected-trip-independent identity,
deterministic digest/ordering, and atomic round trips. It is a schema fixture,
not calibrated regional demand.

### Alternatives investigated or rejected

- A global multiplier derived from Comparator-v2 error is rejected because it
  cannot identify OD structure.
- Monday–Friday pooling and cross-weekday substitution are rejected because
  Phase 1 preserves weekday evidence independently.
- Unexplained interpolation and unconstrained independent bucket matrices are
  rejected because they can manufacture temporal demand structure.
- Phase 3.2 departure placement, balanced stochastic rounding, and path choice
  are deliberately not pulled forward into this contract phase.

### Current limitations and data needed

Phase 3.1 defines how evidence-backed OD demand must be represented; it does
not derive a production Portland OD matrix. Detector counts constrain
screenlines and cross-sections but do not uniquely identify origin and
destination. Production demand still needs a reviewed OD prior, zone system,
gateway totals, vehicle-class/freight splits, and bucket-specific evidence.
ODOT TPAU, Metro, RTC, or other MPO matrices, TAZ definitions, time-of-day
factors, external trips, freight demand, and screenline procedures could close
those gaps. Such guidance remains proposed evidence until reviewed and
authorized; it does not silently replace this contract.

### Downstream dependency and next gate

Phase 3.2 may consume only a validated Phase 3.1 artifact and must implement
deterministic within-bucket departure generation with balanced stochastic
rounding and exact per-bucket conservation. Its two-hour gate must prove no
bucket leakage and same-seed determinism. Phase 3.1 does not run SUMO, modify
the network or measurements, or claim that any demand is calibrated.

## Phase 3.2 — Time-sliced SUMO demand generation

### Objective

Materialize a validated Phase 3.1 weekday OD artifact as deterministic SUMO
trip demand while preserving the source 15-minute matrix boundaries. Phase 3.2
generates demand evidence; it does not simulate, route, calibrate, or tune it.

### Approach and output lineage

The typed `commute_help_time_sliced_sumo_demand` artifact is produced by
`dynamic_od_sumo_demand_generator / phase-3.2-v1` using
`bucket-contained-balanced-rounding-v1`. It contains:

```text
time-sliced-demand.trips.xml.gz
time-sliced-demand-manifest.json
```

The compressed XML contains one SUMO `<trip>` per represented modeled vehicle,
with explicit `fromTaz`, `toTaz`, vehicle type, deterministic identity, and
departure time. It is intentionally labeled `unrouted_zone_pair_demand`:
connector selection and route/path assignment are downstream work, not hidden
inside this phase.

The manifest preserves the Phase 3.1 content digest and demand version, all
source evidence digests, weekday, bucket coverage, assignment policy, source
and effective seed, seed-derivation method, 1:1 scale, generated count,
per-row/per-bucket rounding audits, movement- and vehicle-class conservation,
departure-boundary audit, compressed and uncompressed file hashes, and a
deterministic semantic content digest. Atomic directory promotion prevents a
partial XML or manifest from becoming authoritative.

### Half-open bucket placement

Each source bucket maps directly from local-day minutes to simulation seconds:

```text
interval_start_seconds = bucket_start_minute × 60
interval_end_seconds   = interval_start_seconds + 900

interval_start_seconds <= departure < interval_end_seconds
```

Placement is independently seeded at the source OD-row grain. A trip from the
07:00 matrix cannot depart at or after 07:15, and a trip from the 07:15 matrix
cannot depart before 07:15. Demand is never spread across the whole two-hour
window.

### Balanced stochastic rounding and conservation

Fractional targets are jointly rounded within each weekday/bucket. The
algorithm first floors every row, stochastically rounds the exact bucket total,
then assigns the remaining vehicles among fractional rows using deterministic
weighted sampling without replacement. At 1:1 scale this guarantees:

```text
absolute per-row rounding error < 1 modeled vehicle trip
absolute per-bucket aggregate error < 1 modeled vehicle trip
```

Rounding occurs before departure placement and cannot transfer a vehicle to an
adjacent bucket. The manifest reports source target, represented target,
rounding error, and pass/fail state for every row and bucket. Movement- and
vehicle-class totals remain visible; the accepted integer fixture conserves
them exactly.

### RNG and identity contract

Phase 3.2 preserves the Phase 3.1 base seed and source RNG policy. Derived
rounding and placement streams use:

```text
sha256(effective seed, demand version, weekday, bucket, operation scope)
```

The same source artifact and effective seed therefore produce byte-identical
demand and identical semantic/file digests. A legitimate seed override changes
stochastic departure placement and artifact identity while leaving source
targets, bucket membership, 1:1 scale, and conservation bounds unchanged.

Regional demand identity contains only regional inputs. It contains no selected
trip origin, destination, departure, app-edge endpoints, or probe identity.

### Synthetic two-hour evidence

The accepted Monday 07:00–09:00 fixture produced:

```text
source Phase 3.1 digest:
b6a854b6f5c156fd0951a6e5eed781ae5a88e22b2a47b29a0090752aeba4167b

Phase 3.2 content digest:
acf19e7ad77d2ec5fb7740445392c2dacaf6f7a5627e11ad8b05e8a494e6179a

compressed demand SHA-256:
9d666ab9bad2f00ffe4d0b2053194b8b1d73ee5c97dbcfb826673e7a9f5c2cdb

uncompressed demand SHA-256:
12aa7d2ec8bfad795e083a24cf2d39cbde80762e9a7393f1ba8d5edf071c0ab2

8 buckets
32 source OD rows
2,160 target trips
2,160 generated SUMO trips
0 aggregate rounding error
0 bucket leakage
0 interval-end departures
0 duplicate vehicle identities
```

Bucket targets and generated counts were exactly:

```text
07:00  200 → 200
07:15  220 → 220
07:30  240 → 240
07:45  260 → 260
08:00  280 → 280
08:15  300 → 300
08:30  320 → 320
08:45  340 → 340
```

A repeat with the same seed reproduced both content and demand-file hashes
exactly. Seed `31002` retained all 2,160 targets and zero leakage but produced a
different demand hash
`f6e79a279e5fa8539213e70aa7f8fa4c394aee4bda95631ebf84efcadc1a3a5b`,
proving placement variability without target mutation. Separate fractional
fixtures prove the balanced-rounding bounds rather than relying only on the
integer production gate.

### Why this approach was selected

The prior snapshot generator accepted one start-time target and spread its
departures across a long period. That changes the temporal meaning of a
15-minute target and can shift peak demand into neighboring intervals. Direct
bucket containment preserves the Phase 3.1 evidence grain. Individual trips
also make departure identity and leakage directly auditable.

### Alternatives investigated or rejected

- Whole-window departure smearing is rejected because it destroys bucket
  identity.
- A global multiplier is rejected because Comparator-v2 residuals do not
  identify OD structure.
- Independent per-row rounding is rejected because its errors can accumulate
  without a bucket-level conservation bound.
- Route/path assignment is deferred rather than guessed; Phase 3.4 owns bounded
  alternative path sets and probabilities.
- Weekday substitution, pooling, weekend synthesis, and missing-bucket
  interpolation remain prohibited by the source contract.

### Current limitations and external data needs

The synthetic artifact proves generation mechanics, not Portland demand
accuracy. The output still needs reviewed production OD matrices, zone/TAZ and
connector artifacts, gateway/external trips, freight and vehicle-class splits,
and bucket-specific targets before a real regional experiment. ODOT TPAU,
Metro, RTC, and MPO model inputs could improve those layers but remain proposed
evidence until reviewed and authorized. No network, capacity, speed, signal,
vehicle behavior, station, comparator, or closure method changed.

### Downstream dependency and next gate

Phase 3.3 may compare the existing snapshot demand with dynamic 15-minute
demand only in a separately authorized bounded experiment, initially a
high-value window such as 04:00–10:00. It must use Comparator-v2 and diagnose
OD distribution if peak flow/timing fails to improve or speed residuals degrade
catastrophically. Phase 3.2 itself ran no SUMO simulation and made no calibration
or promotion claim.

---

# 15. Phase 4 — Reusable Baseline Worlds

### Objective
Generate calibrated regional traffic worlds that can serve many trip questions.

### Planned identity
Only world-changing inputs belong in baseline identity, such as:
- network
- weekday/date profile
- demand version
- season/weather assumptions if modeled
- controls/signals
- seed/RNG policy
- simulation mode

Selected origin/destination does not belong in baseline identity.

### Checkpoints
Two concepts remain distinct:

**Recovery checkpoints:** current 100-second crash/restart state.

**World checkpoints:** planned 15-minute reusable regional states.

---

# 16. Phase 5 — Forked Closure Worlds

### Objective
Create closure/restriction scenarios from causally valid baseline checkpoints.

### Method
Fork from the latest baseline checkpoint before closure awareness or the restriction itself can affect behavior.

Potential scenario inputs:
- full closure
- lane reduction
- speed restriction
- start/end time
- traveler awareness
- rerouting behavior
- signal/ramp changes

Parent baseline worlds remain immutable.

---

# 17. Phase 6 — Trip Probe

### Objective
Answer a specific commute question without rebuilding regional traffic.

### Method
Inject a lightweight equivalent trip probe into an existing baseline/scenario world.

Compare:
- route
- travel time
- congestion encountered
- baseline vs scenario impact

Changing origin/destination changes the question, not the regional world.

---

# 18. Phase 7 — Uncertainty

Potential uncertainty sources:
- historical day-to-day variation
- OD uncertainty
- demand scaling
- route choice
- driver awareness
- incidents
- weather
- signal/control assumptions
- RNG/seed sensitivity

Potential outputs:
- median travel time
- P10/P90
- scenario delta distribution
- confidence/coverage flags

### External data that could help
- incident history
- weather
- construction history
- event calendars
- school/holiday effects
- freight seasonality
- agency scenario assumptions

---

# 19. Phase 8 — Real Commute Validation

### Objective
Validate end-to-end route/travel-time predictions against real trips.

Potential evidence:
- controlled observed commute runs
- GPS traces
- probe-vehicle datasets
- agency travel-time datasets
- legally/technically available historical routing/travel-time products

This validates end-to-end realism rather than only detector matching.

---

# 20. Phase 9 — UI

Planned capabilities:
- map-based closure selection
- full/lane restriction configuration
- date/time selection
- origin/destination selection
- baseline/scenario comparison
- uncertainty display
- route visualization
- local save/load scenarios
- launch selected route in Google Maps

UI follows model validity rather than preceding it.

---

# 21. Phase 10 — Durability and Performance

Focus:
- checkpoint durability
- resumability
- bounded memory
- deterministic identities
- cache reuse
- disk limits
- cleanup
- baseline/scenario reuse
- fast probe execution

Optimizations may not weaken physical or calibration semantics.

---

# 22. Phase 11 — Final Acceptance

Final product should demonstrate:

1. historical evidence has traceable provenance
2. SUMO traffic state is directly measurable
3. historical/simulated measurement semantics are aligned
4. calibration error is objectively quantified
5. dynamic demand reproduces important observed patterns
6. reusable baseline worlds work
7. closure worlds fork causally
8. trip probes are lightweight
9. uncertainty is surfaced
10. predicted commute impacts can be validated against reality

---

# 23. Data Gap Register

## A. Historical detector metadata
Desired:
- exact detector cross-section/position
- lane assignment
- station grouping semantics
- detector active dates
- outage/maintenance history
- quality flags
- official aggregation rules

## B. Origin-Destination demand
Desired:
- OD matrices
- trip tables
- TAZs
- time-of-day factors
- external trips
- freight/truck demand
- trip-purpose distributions

Potential sources: ODOT TPAU, Metro, RTC, MPO models.

## C. Calibration/validation standards
Desired:
- Oregon calibration methodology
- acceptance thresholds
- screenline procedures
- corridor balancing methods
- count weighting
- peak-period validation rules

## D. Traffic controls
Desired:
- signal timing plans
- ramp-meter timing
- variable speed limits
- managed-lane rules
- temporary closure control plans

## E. Incidents/weather/special conditions
Desired:
- incident history
- crash/closure records
- weather
- special events
- unusual construction
- school/holiday effects

## F. Construction scenario assumptions
Desired:
- exact closure geometry
- staging
- closure schedule
- ramp impacts
- traveler-information assumptions
- diversion assumptions
- temporary signal/ramp changes

---

# 24. Questions to Ask ODOT / TPAU

1. What regional/MPO travel-demand model covers the Portland-Vancouver study area?
2. Can OD matrices or trip tables be shared for planning/research use?
3. What TAZ system is used?
4. What time-of-day periods are represented?
5. Which traffic count datasets calibrate/validate the model?
6. How are PORTAL stations aggregated when multiple detectors or longitudinal stations represent a roadway?
7. What detector-quality rules are used?
8. What calibration metrics and thresholds are standard?
9. Are screenline or corridor balancing procedures used?
10. How are Columbia River bridge crossings validated?
11. How are external trips and freight handled?
12. How are peak spreading and time-of-day demand represented?
13. What route-choice/assignment algorithm is used?
14. Are volume-delay functions and capacity assumptions shareable?
15. Are signal/ramp-meter inputs incorporated?
16. Are model outputs available for I-5/I-205 Columbia River crossings?
17. Are there existing closure/diversion studies relevant to this bridge closure?
18. What assumptions would TPAU consider essential before treating this project as planning-grade evidence?

---

# 25. Change-Control Procedure for External Guidance

When outside experts recommend another method:

1. Record the recommendation.
2. Identify the project assumption/phase it affects.
3. Capture the evidence supporting it.
4. Compare it with the current methodology/artifacts.
5. Classify whether it:
   - fills a known gap
   - improves accuracy
   - changes implementation only
   - changes model semantics
   - invalidates previous artifacts
6. Define migration impact.
7. Add tests/validation criteria.
8. Only then change the architecture.

The project should not be rewritten solely because an authoritative organization uses a different tool. The relevant question is whether new information better represents the physical traffic process being modeled.

---

# 26. Current Project Position

```text
Phase 1 historical truth                  COMPLETE
Phase 2.1 SUMO native telemetry           COMPLETE
Phase 2.2 comparator-v1                   COMPLETE
Phase 2.2a mapping topology               COMPLETE
Phase 2.2b station geometry               COMPLETE
Phase 2.2c crossing counter feasibility   COMPLETE
Phase 2.2d projection policy              COMPLETE
Phase 2.2e regional observer              COMPLETE
Phase 2.2f departure provenance           COMPLETE
Phase 2.2g comparator-v2                  COMPLETE / ACCEPTED FLOW RULER
Phase 2.3 promotion gates                 COMPLETE
Phase 3.1 dynamic OD contract             COMPLETE
Phase 3.2 time-sliced demand generation   COMPLETE
Phase 3.3 bounded dynamic experiment      UNBLOCKED / NOT STARTED
```

The FLOW measurement instrument, promotion machinery, dynamic OD contract, and
bucket-contained demand generator are accepted. Phase 3.2 produced only an
unrouted synthetic demand artifact; no SUMO run or traffic tuning occurred.
Phase 3.3 may begin only as a separate authorized task.

---

# 27. Short Explanation for Reviewers

> The simulator is being built from evidence outward. Historical observations are first preserved and characterized; SUMO is then instrumented so its simulated traffic state can be measured; historical and simulated evidence are aligned to the same roadway, time, direction, and physical cross-section; only after that comparison layer is trustworthy will demand be calibrated. This prevents tuning a simulation against mismatched or poorly understood metrics and allows future baseline traffic worlds to be reused for many closure and commute questions.

---

# 28. Living Proposal Maintenance Rule

This document is part of the project documentation contract. It must be
updated alongside any phase or subphase that changes or establishes:

- methodology
- physical or modeling assumptions
- data interpretation
- artifact lineage
- calibration or validation methodology
- measurement semantics
- known limitations or unresolved evidence gaps
- required external data
- promotion criteria

For every major methodology, preserve:

1. objective
2. approach
3. why the approach was selected
4. evidence supporting it
5. alternatives investigated or rejected
6. current limitations
7. data needed to close remaining gaps
8. external agency/model data that could improve it
9. downstream dependency
10. next validation gate

External guidance from ODOT, TPAU, Metro, RTC, MPOs, academics, or other
experts must first be recorded as proposed guidance and evaluated against the
existing evidence and artifact lineage. It does not change the active
methodology until Tyler explicitly authorizes that change.

Update this document whenever a phase:
- introduces a methodology
- changes an assumption
- identifies a limitation
- resolves a data gap
- introduces a promoted artifact
- receives external technical guidance
- changes a promotion criterion
- changes metric interpretation

Every update should answer:

1. What changed?
2. Why?
3. What evidence supports it?
4. What does it invalidate, if anything?
5. What data is still missing?
6. What is the next gate?

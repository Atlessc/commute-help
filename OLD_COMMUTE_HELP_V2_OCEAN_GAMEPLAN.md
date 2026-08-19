# Commute Help V2 — “Sim the Ocean, Then Navigate the Droplet” Game Plan

**Branch:** `Commute-Help-v2`  
**Execution:** Codex builds one small chunk at a time. Tyler runs each acceptance gate.  
**Reasoning:** Use **Terra Medium** for bounded/mechanical work. Use **Terra High** only for calibration math, world identity/forking, OD modeling, convergence, and predictive routing.

---

## Core architecture

```text
Historical PORTAL observations
        ↓
Historical edge/time truth
        ↓
Historically calibrated baseline traffic world
        ↓
Reusable 15-minute world checkpoints
        ↓
Fork selected closure/restriction world
        ↓
Inject selected trip into baseline + closure worlds
        ↓
Compare trip time, route, diversion point, and why
```

The **baseline is the ocean**. The **closure scenario is the altered ocean**. The **trip is the droplet**.

A selected trip must never be required to build a baseline or closure world.

---

# Codex working contract

For every Codex task:

1. Read `AGENTS.md` and this game plan first.
2. Work on **one numbered chunk only**.
3. Inspect Git status and the current implementation before editing.
4. Preserve unrelated local changes.
5. Add/update tests for changed behavior.
6. Codex may run targeted tests while building, but the chunk does **not pass** until Tyler runs the user gate.
7. Do not commit, push, delete generated data, or mutate raw PORTAL data unless Tyler explicitly authorizes it.
8. Do not fabricate weekend traffic. Historical V2 is Monday–Friday only.
9. Do not call anything `historically_calibrated` until its validation gate passes.
10. Keep physical rerouting at **60 seconds**.
11. Preserve the existing **100-second compute checkpoint** mechanism unless the chunk explicitly changes only the durable-world checkpoint layer.
12. Keep regional SUMO mesoscopic. Do not turn the whole metro into microscopic simulation.
13. Keep 1:1 meaning approximately one SUMO vehicle per modeled vehicle trip.
14. Never hardcode private home/work coordinates.
15. Stop after the requested chunk and report files changed, tests run, artifacts, limitations, and Tyler's exact acceptance commands.

If Tyler's gate fails:

```text
STOP → diagnose this chunk → fix this chunk → Tyler reruns gate
```

Do not continue to the next architecture layer.

---

# Common Tyler health gate

Run frequently:

```bash
git status --short --branch
npm run doctor
npm run sumo:doctor
npm run test:backend
npm run test:frontend
```

At macro-phase boundaries:

```bash
npm test
```

For UI changes:

```bash
npm run dev
```

Then Tyler tests the real browser workflow.

Every generated calibration/world artifact must expose its schema/model version, graph/network version, source hashes, weekday/date selection, time resolution, seed, scale, evidence level, limitations, and validation status.

---

# PHASE 0 — Lock the V2 contract

## 0.1 Current-system inventory
**Codex:** Terra Medium

Create a small developer inventory of the current:

- simulation run kinds;
- traffic profile/schedule artifacts;
- background seed;
- SUMO network/model versions;
- checkpoint/playback formats;
- selected-trip coupling;
- closure coupling;
- reliability/P95 calculation path;
- evidence labels.

Do not change runtime behavior.

**Tyler gate**

```bash
git diff --check
npm run test:backend
```

**Pass:** The inventory clearly shows that today's `regional_comparison` couples baseline + scenario + selected trip.

---

## 0.2 Add V2 invariants to agent instructions
**Codex:** Terra Medium

Document the new run domains:

```text
regional_baseline
regional_scenario
trip_probe
```

Required invariants:

- baseline requires no origin/destination;
- baseline requires no closure;
- scenario references a parent baseline world;
- trip probe references already-created world state(s);
- historical input is not the same as historical calibration;
- weekday, exact-date, and season-matched profiles stay distinguishable;
- no weekends until observed weekend data exists.

**Tyler gate**

```bash
git diff -- AGENTS.md
git diff --check
```

---

# PHASE 1 — Historical traffic truth

The old PM reliability profile has a useful clue: median slowdown about **1.05×**, P85 **1.48×**, P90 **1.96×**, P95 **2.651×**. Do not preserve `2.651` as a magic constant. Preserve and understand the underlying edge/time slowdown distributions that created it.

## 1.1 Calibration-v2 artifact schema
**Codex:** Terra High

Define two grains:

```text
DATE LEVEL
edge + local_date + weekday + 15-minute bucket

AGGREGATE LEVEL
edge + weekday + 15-minute bucket
```

Keep weekdays separate:

```text
monday
tuesday
wednesday
thursday
friday
```

Required metrics where available:

```text
sample_days
volume mean/p50/p85/p90/p95
speed mean/p10/p50/p85/p90/p95
occupancy mean/p50/p85/p95
slowdown p50/p85/p90/p95
evidence level
quality flags
```

Slowdown keeps the useful concept:

```text
reference/free-flow speed ÷ observed speed
```

Codex must explicitly define detector/lane collapse rules, missing-bucket policy, minimum sample days, outlier policy, and how exact historical dates remain available below canonical weekday aggregates.

**Tyler gate:** inspect schema + synthetic fixture, then:

```bash
npm run test:backend
```

**Pass:** One row can be explained physically without ambiguity.

---

## 1.2 Diagnostic historical compiler
**Codex:** Terra Medium

Build a bounded diagnostic compiler, preferably a new script such as:

```text
scripts/build_historical_calibration_v2.py
```

Output:

```text
edge-day-15m.parquet
edge-time-distributions.parquet
validation-report.json
validation-report.md
```

Never mutate raw PORTAL data.

**Tyler gate:** run a small date/time subset and verify Monday–Friday separation, 15-minute buckets, source hashes, slowdown distributions, and no weekend rows.

Then:

```bash
npm run test:backend
```

---

## 1.3 Production bounded-memory compiler
**Codex:** Terra High

Convert the diagnostic into production processing for Jan 2024 through Jul 2026 using bounded streaming like the current schedule compiler:

- PyArrow batches;
- deterministic accumulators/histograms;
- atomic output;
- deterministic ordering;
- validation report;
- no full-corpus pandas concat in memory.

**Tyler gate:** run the full compiler locally.

```bash
ls -lh data/traffic/processed/calibration-v2/
npm run test:backend
```

**Pass:** Five weekdays, 96 possible buckets/day, ordered quantiles, valid physical values, reproducible hashes, no fabricated weekends.

---

## 1.4 Explain the old P95 tail
**Codex:** Terra Medium

Create a report comparing the old pooled PM multiplier distribution to edge/time-specific weekday distributions.

Report which roads/time buckets produce the high slowdown tail and whether it clusters on specific bottlenecks/corridors.

**Tyler gate:** inspect report output.

**Pass:** We know whether the useful P95 came from genuine bottleneck/time behavior or a mapping/statistical artifact.

---

# PHASE 2 — Instrument SUMO before tuning it

## 2.1 Native 15-minute SUMO telemetry
**Codex:** Terra Medium

Add optional native SUMO edge telemetry at **900-second intervals** for calibration runs.

Capture where available:

```text
entered
left
flow/derived VPH
speed
travel time
density
occupancy
waiting time
time loss
```

Do not remove existing playback or result summaries.

**Tyler gate:** run a 20–30 minute regional smoke. Confirm multiple intervals, valid edge IDs, nonnegative flow, plausible speeds.

```bash
npm run test:backend
```

---

## 2.2 PORTAL ↔ SUMO comparator
**Codex:** Terra High

Create a comparison service/script that maps SUMO telemetry to historical calibration edges and reports at minimum:

```text
VPH WAPE
speed MAE
speed bias
slowdown error
peak-time error
coverage/unmapped counts
```

Treat occupancy as secondary until detector occupancy and SUMO edge occupancy semantics are intentionally aligned.

Outputs:

```text
comparison.parquet
comparison-summary.json
comparison-report.md
```

**Tyler gate:** run a bounded baseline + comparator and inspect worst 25 edge/time residuals.

**Pass:** Every future traffic-model change can be objectively compared against the same metrics.

---

## 2.3 Promotion gates
**Codex:** Terra Medium

Add explicit lifecycle states such as:

```text
candidate
calibration_passed
validation_passed
promoted
rejected
```

Do not invent final numeric thresholds yet. Build the mechanism and make failed reasons visible.

**Tyler gate:** prove a deliberately failing fixture cannot promote and a passing synthetic fixture can.

---

# PHASE 3 — Dynamic 15-minute demand

## 3.1 Dynamic OD contract
**Codex:** Terra High

Define a time-series OD artifact:

```text
weekday
bucket_start_minute
origin zone
destination zone
vehicle class
target trips/VPH
evidence/source
```

Preserve gateway-aware movement classes, demand conservation, seed lineage, and 1:1 semantics.

Define temporal regularization so adjacent OD matrices cannot oscillate wildly without detector evidence.

**Tyler gate:** inspect schema + synthetic 2-hour fixture.

```bash
npm run test:backend
```

---

## 3.2 Time-sliced SUMO demand generation
**Codex:** Terra High

Replace:

```text
one start-time target → departures spread across long run
```

with:

```text
07:00 bucket → departures inside 07:00–07:15
07:15 bucket → departures inside 07:15–07:30
...
```

Retain balanced stochastic rounding.

**Tyler gate:** build a 2-hour demand file and verify per-bucket conservation, no bucket leakage, deterministic same-seed behavior.

```bash
npm run test:backend
```

---

## 3.3 First dynamic-demand experiment
**Codex:** Terra Medium for tooling, Terra High only if model math must change

Compare current snapshot demand versus dynamic 15-minute demand over a bounded high-value window, initially around six hours such as 04:00–10:00.

Use the Phase 2 comparator.

**Tyler gate:** Tyler runs both simulations and compares reports.

**Pass:** Peak timing/flow improves without catastrophic speed residuals elsewhere. If not, stop and diagnose OD distribution.

---

## 3.4 Multiple plausible OD paths
**Codex:** Terra High

Replace one free-flow path per major OD movement with a bounded alternative set, initially about 3–5 meaningfully distinct corridors for high-volume movements.

Store path identity separately from probability/weight.

**Tyler gate:** rerun bounded calibration and compare residuals, route concentration, max V/C, and artificial bottlenecks.

**Pass:** Multi-path assignment improves or at least does not degrade held-out behavior while reducing unrealistic path concentration.

---

# PHASE 4 — First-class baseline worlds

## 4.1 Split run domains
**Codex:** Terra High

Implement schemas/metadata/dispatch for:

```text
regional_baseline
regional_scenario
trip_probe
```

Baseline must validate with no trip or closure. Scenario requires a parent baseline. Probe requires world state(s).

**Tyler gate**

```bash
npm run test:backend
```

Manually verify API validation for the three contracts.

---

## 4.2 Baseline-only worker
**Codex:** Terra High

Reuse existing SUMO network, dynamic demand, mesoscopic mode, 100-second compute chunks, checkpoint/retry, RNG persistence, telemetry, and playback.

Remove selected-trip dependencies from baseline execution.

Output a world manifest with demand/network/model hashes and provenance.

**Tyler gate:** run a 20–30 minute baseline with no selected trip and no closure. Confirm playback dots still represent sampled active SUMO vehicles.

---

## 4.3 Durable 15-minute world checkpoints
**Codex:** Terra Medium

Keep 100-second compute checkpoints for crash recovery, but promote durable scenario-fork states every **900 seconds**.

A promoted state must include/reference:

```text
SUMO state
RNG state
Python state
world ID
simulation clock
network/model hashes
exact future-demand/route hashes
future-demand contract
```

**Tyler gate:** run 45 minutes and verify durable 15, 30, 45-minute states.

---

## 4.4 Resume/fork equivalence gate
**Codex:** Terra Medium

Automate comparison of uninterrupted continuation versus checkpoint reload.

Compare departed, arrived, teleports, active vehicle identity/count, selected telemetry, edge intervals, and RNG-sensitive results.

**Tyler gate:** run the equivalence command.

**Pass:** Match exactly or within an explicitly justified tolerance. No closure work before this passes.

---

## 4.5 Build baseline duration progressively
**Codex:** Terra Medium orchestration only

Do not immediately spend hours on five full days.

Progression:

```text
30-minute smoke
→ 2-hour proof
→ 6-hour peak-window candidate
→ 24-hour weekday candidate
```

Tyler runs the expensive simulations.

**Pass:** Comparator remains healthy as duration increases and a candidate world carries calibration evidence.

---

# PHASE 5 — Closure worlds forked from baseline

## 5.1 Scenario-world identity/cache key
**Codex:** Terra High

Scenario identity includes:

```text
parent baseline world hash
fork time
awareness start
restriction start/end
directed restrictions
driver behavior model version
seed/RNG policy
SUMO/network/model version
```

It must not include trip origin/destination.

**Tyler gate:** same closure + different trips produces same scenario identity; one changed lane/speed/time/edge produces a different identity.

---

## 5.2 Fork from baseline state
**Codex:** Terra High

Load a baseline checkpoint and exact future demand, apply scenario behavior, and simulate forward. Do not replay the earlier day.

**Tyler gate:** short baseline → fork → closure scenario. Verify baseline is not recomputed from time zero.

**Pass:** Parent and scenario states are identical before the fork and diverge only when the scenario says they should.

---

## 5.3 Awareness time vs physical restriction time
**Codex:** Terra Medium

Support:

```text
planned closure: awareness_start < restriction_start
surprise closure: awareness_start == restriction_start
```

Do not guess information-penetration percentages yet.

**Tyler gate:** planned and surprise test cases show pre-closure rerouting only when awareness begins earlier.

---

## 5.4 Scenario edge-cost timeline
**Codex:** Terra Medium

Export time-indexed edge conditions:

```text
travel time
speed
flow
waiting/time loss
restriction state
baseline delta
```

**Tyler gate:** inspect one corridor over time and confirm correct divergence timing.

---

## 5.5 Bounded scenario stabilization
**Codex:** Terra High

Implement bounded loaded-cost iteration:

```text
same baseline checkpoint
→ closure pass 1
→ collect loaded costs
→ closure pass 2
→ compare
→ optional pass 3...
```

Track convergence using edge travel-time, flow-share, route-share, and/or queue/spillback deltas.

Hard-cap iterations. Report non-convergence honestly.

**Tyler gate:** run a bounded closure case and inspect per-pass convergence report.

---

# PHASE 6 — Trip probe: the droplet

## 6.1 Probe worker
**Codex:** Terra High

Probe request includes:

```text
origin
destination
departure time
baseline world
optional scenario world
vehicle behavior/profile
```

At departure:

```text
fork baseline state → inject selected car → run until arrival
fork scenario state → inject identical car → run until arrival
```

Never rebuild the regional ocean.

**Tyler gate:** synthetic route verifies identical vehicle definition/departure and immutable parent worlds.

---

## 6.2 Predictive time-dependent routing
**Codex:** Terra High

The router must evaluate an edge using traffic expected **when the car reaches it**, not only at departure.

Mandatory test:

```text
Road Q clear at departure
Road Q heavily congested before probe reaches it
X→Y remains faster
```

**Tyler gate:** current-state router stays on Q while predictive router correctly diverts upstream.

This is a non-negotiable V2 behavior.

---

## 6.3 “Driving instinct” cost model
**Codex:** Terra High

Add transparent versioned behavior terms such as:

```text
predicted travel time
road hierarchy
residential/local-road exposure
turn cost
backtracking/awkward movement cost
route-switch inertia
minimum meaningful savings
```

Do not present guessed coefficients as calibrated truth.

**Tyler gate:** test four patterns:

1. residential shortcut saves 10 sec;
2. arterial alternative saves 5 min;
3. awkward many-turn route saves little;
4. local-road route is genuinely much faster.

**Pass:** behavior avoids silly shortcuts without blocking genuinely valuable detours.

---

## 6.4 Strategic divergence/rejoin alternatives
**Codex:** Terra Medium

Explain routes as:

```text
stay on normal corridor until X
diverge at X
use Y
rejoin at Z
predicted saving
reason
```

**Tyler gate:** browser/report alternatives must be materially different corridors, not cosmetic one-block variants.

---

## 6.5 Candidate replay
**Codex:** Terra Medium

For top routes, fork identical probe states, run each candidate, and compare predicted versus realized simulated duration.

**Tyler gate:** replay 2–3 candidates from the same scenario state.

**Pass:** recommendation can be based on realized replay instead of only heuristic score.

---

# PHASE 7 — Uncertainty and meaningful percentiles

## 7.1 Coherent historical ensembles
**Codex:** Terra High

Build ensembles from coherent whole historical days or date clusters rather than independently sampling every road.

Preserve spatial/temporal correlation.

**Tyler gate:** run a small bounded ensemble first.

**Pass:** P50/P85/P90/P95 report sample count and coherent source selection.

---

## 7.2 Percentile backtesting
**Codex:** Terra Medium

Hold out historical dates and test empirical coverage of P85/P90/P95.

**Tyler gate:** run the coverage report.

**Pass:** percentile error is visible. Never rename a bad percentile just because a different label looks nicer.

---

# PHASE 8 — Real commute validation

## 8.1 Benchmark-trip regression harness
**Codex:** Terra Medium

Reuse existing local benchmark structures where possible:

```text
weekday
departure time
actual travel duration
reported no-traffic duration
optional route/checkpoints
incident/weather notes
model version
```

Keep personal benchmark data local and uncommitted.

**Tyler gate:** add several known trips if available and run report.

**Pass:** personal trip accuracy complements detector validation rather than replacing it.

---

## 8.2 Compare three routing generations
**Codex:** Terra Medium

Every benchmark compares:

```text
A. free-flow route
B. current-state traffic route
C. predictive future-aware world route
```

Measure ETA error, route regret, and wrong-corridor choice.

**Tyler gate:** run benchmark report.

---

## 8.3 Held-out temporal/corridor validation
**Codex:** Terra High

Hold out whole dates/months and selected corridor/direction groups. Do not rely only on random row splits.

**Tyler gate:** run validation report.

**Pass:** training and held-out performance are both visible and satisfy promotion thresholds Tyler approves.

---

# PHASE 9 — Product UI around the world hierarchy

## 9.1 Baseline Worlds UI
**Codex:** Terra Medium

Show selectable baseline worlds with weekday/profile, historical source window, time span, model version, calibration/validation status, seed, 1:1 scale, and playback.

**Tyler gate**

```bash
npm run dev
npm run test:frontend
```

Browser: play/scrub a baseline without choosing a trip or closure.

---

## 9.2 Closure Scenarios UI
**Codex:** Terra Medium

Workflow:

```text
choose baseline
mark closure/lane/speed restrictions
set awareness/restriction times
build scenario
play baseline vs scenario
```

No trip required.

**Tyler gate:** full browser workflow + `npm run test:frontend`.

---

## 9.3 Trip Probe UI
**Codex:** Terra Medium

Workflow:

```text
choose baseline
choose optional closure scenario
choose origin/destination
choose departure time
run probe
```

Return baseline ETA, scenario ETA, delta, routes, divergence/rejoin, reason, replay results, and uncertainty where available.

**Tyler gate:** real browser workflow.

---

# PHASE 10 — Durability and performance

## 10.1 Durable world workers
**Codex:** Terra High

A browser refresh or Uvicorn reload must not kill a multi-hour baseline job or corrupt its DB status.

Implement durable local worker lifecycle/reconciliation appropriate for the existing architecture.

**Tyler gate:** start a bounded world, intentionally restart the dev server, verify the job survives/resumes and reconciles without manual SQLite repair.

---

## 10.2 Content-addressed world DAG/cache
**Codex:** Terra Medium

Model:

```text
baseline world
    ↓
scenario world
    ↓
trip probes
```

Cache keys include every physical input that can alter results.

**Tyler gate:** same baseline twice, same closure twice, multiple trips against one scenario, then one modified closure.

**Pass:** safe reuse without incompatible collisions.

---

## 10.3 Artifact retention/compaction
**Codex:** Terra Medium

Distinguish:

```text
temporary 100-sec recovery checkpoints
canonical 15-min world checkpoints
playback
telemetry
comparison reports
promoted worlds
failed experiments
```

Create a dry-run retention policy before any deletion logic.

**Tyler gate:** dry run only first. Promoted worlds must never be selected for deletion accidentally.

---

# PHASE 11 — Final Tyler-run acceptance campaign

## 11.1 One full 24-hour weekday baseline

Only after 30m → 2h → 6h tests pass, Tyler runs one full weekday at 1:1.

Required:

```text
playback
canonical checkpoints
telemetry
PORTAL comparison
calibration report
validation status
runtime/disk report
```

---

## 11.2 Monday–Friday baseline worlds

Build all five weekdays separately. Never merge Monday–Thursday.

---

## 11.3 Real closure world

Use an actual planned closure. Verify the baseline is reused and spillover begins at the correct awareness/restriction time.

Measure how much compute was avoided versus replaying from the start.

---

## 11.4 Real trip probe

Compare:

```text
free flow
old reliability/P95 estimate
baseline world probe
closure world probe
predictive route
actual known commute benchmarks where available
```

The final result should explain why a diversion is worthwhile based on traffic expected when the car reaches the affected road.

---

# Recommended Codex task sequence

| # | Chunk | Reasoning |
|---:|---|---|
| 1 | 0.1 inventory | Terra Medium |
| 2 | 0.2 V2 invariants | Terra Medium |
| 3 | 1.1 calibration schema | **Terra High** |
| 4 | 1.2 diagnostic compiler | Terra Medium |
| 5 | 1.3 production compiler | **Terra High** |
| 6 | 1.4 P95-tail analysis | Terra Medium |
| 7 | 2.1 SUMO telemetry | Terra Medium |
| 8 | 2.2 comparator | **Terra High** |
| 9 | 2.3 promotion gates | Terra Medium |
| 10 | 3.1 dynamic OD contract | **Terra High** |
| 11 | 3.2 time-sliced demand | **Terra High** |
| 12 | 3.3 bounded experiment | Terra Medium |
| 13 | 3.4 multi-path OD | **Terra High** |
| 14 | 4.1 run-domain split | **Terra High** |
| 15 | 4.2 baseline worker | **Terra High** |
| 16 | 4.3 durable checkpoints | Terra Medium |
| 17 | 4.4 equivalence gate | Terra Medium |
| 18 | 4.5 baseline candidate tooling | Terra Medium |
| 19 | 5.1 scenario identity | **Terra High** |
| 20 | 5.2 scenario fork | **Terra High** |
| 21 | 5.3 awareness timing | Terra Medium |
| 22 | 5.4 cost timeline | Terra Medium |
| 23 | 5.5 stabilization | **Terra High** |
| 24 | 6.1 trip probe | **Terra High** |
| 25 | 6.2 predictive routing | **Terra High** |
| 26 | 6.3 driving instinct | **Terra High** |
| 27 | 6.4 alternatives | Terra Medium |
| 28 | 6.5 candidate replay | Terra Medium |
| 29 | 7.1 coherent ensembles | **Terra High** |
| 30 | 7.2 percentile backtest | Terra Medium |
| 31 | 8.1 benchmark regression | Terra Medium |
| 32 | 8.2 routing comparison | Terra Medium |
| 33 | 8.3 held-out validation | **Terra High** |
| 34 | 9.1 baseline UI | Terra Medium |
| 35 | 9.2 scenario UI | Terra Medium |
| 36 | 9.3 probe UI | Terra Medium |
| 37 | 10.1 durable workers | **Terra High** |
| 38 | 10.2 cache/DAG | Terra Medium |
| 39 | 10.3 retention | Terra Medium |
| 40 | 11.x acceptance support | Terra Medium unless architecture fails |

---

# Paste-this-first Codex prompt template

```text
You are working in the Commute Help repository on branch Commute-Help-v2.

Read AGENTS.md and COMMUTE_HELP_V2_OCEAN_GAMEPLAN.md completely before editing.

Implement ONLY chunk: <PHASE NUMBER + NAME>.
Do not start the next chunk.

Before editing:
1. inspect git status;
2. inspect the relevant current files;
3. summarize the existing behavior;
4. identify the minimum files that need to change.

Requirements:
<paste only the selected chunk requirements>

Preserve:
- local-only architecture;
- FastAPI/Python backend;
- React/Vite frontend;
- SQLite metadata;
- SUMO in a separate worker process;
- 1:1 physical scale semantics;
- 60-second physical reroute period;
- existing 100-second compute checkpoint behavior unless this chunk explicitly changes durable-world checkpoint promotion;
- all unrelated user changes.

Do not:
- commit or push;
- delete local/generated data;
- mutate raw traffic files;
- fabricate historical data;
- add weekend profiles;
- label anything historically calibrated unless its gate passes;
- hardcode private addresses/coordinates;
- continue into the next game-plan chunk.

Testing:
Run only the smallest relevant tests during implementation. Add/update tests for behavior you change.

At the end, STOP and report:
1. files changed;
2. what changed;
3. tests run and exact outcomes;
4. exact commands Tyler should run for the USER ACCEPTANCE GATE;
5. generated artifacts;
6. known limitations;
7. whether this chunk is ready for acceptance.
```

---

# Tyler pass/fail template

```text
PHASE <X> PASS
commands:
...
results:
...
notes:
...
```

or:

```text
PHASE <X> FAIL
failed gate:
...
output:
...
expected:
...
```

If failed, the next Codex task is simply:

```text
Diagnose and repair PHASE <X> only.
Do not continue to PHASE <X+1>.
```

---

# Do not optimize early

Do not spend early V2 usage on:

- prettier traffic dots;
- higher playback dot caps;
- full-region microscopic simulation;
- final UI polish;
- Google Maps refinements;
- hardcoded 2.651× P95 tuning;
- arbitrary driver penalties;
- huge ensembles before one deterministic world is calibrated;
- trip-routing cleverness before the ocean is measurable.

Correct order:

```text
historical truth
→ SUMO measurement
→ dynamic demand
→ reusable baseline ocean
→ forked closure ocean
→ future-aware trip probe
→ uncertainty
→ UI polish
```

---

# V2 definition of success

V2 is done when:

1. Historical traffic is preserved by **weekday + direction + edge + time**.
2. SUMO is objectively compared with historical VPH/speed/slowdown.
3. Demand changes through the day in 15-minute slices.
4. A baseline world runs and plays back without a selected trip.
5. A closure world forks from a baseline instead of resimulating the earlier day.
6. A trip can be injected into baseline and closure worlds without rebuilding either.
7. Routing evaluates conditions expected when the car reaches each road.
8. The system can strategically divert upstream around predicted downstream spillover.
9. Candidate routes can be replayed in identical frozen world state.
10. P85/P90/P95 come from coherent ensembles/backtesting, not one deterministic run.
11. Long jobs survive app/dev-server interruptions through a tested durable-worker contract.
12. Every promoted world carries reproducible provenance and validation evidence.
13. The old “P95 happens to be accurate” clue becomes an edge/time calibration target rather than a magic multiplier.
14. The user workflow is finally:

```text
choose/build the ocean
→ optionally choose/build the changed ocean
→ drop in the trip
→ understand what happens and why
```

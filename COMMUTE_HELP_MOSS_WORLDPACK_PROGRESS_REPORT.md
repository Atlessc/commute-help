# Commute Help / MOSS / WorldPack Authoritative Roadmap

**Last reconciled:** 2026-09-03

**Role:** Long-range project sequence and scientific gates

**Execution model:** Codex authors; Tyler executes

**Current diagnostic world:** calibration-only, two dates, rho zero, 11:30–19:00 causal replay
**Future production world:** continuous 24-hour historical traffic state compiled into reusable WorldPacks

This is the authoritative long-range roadmap. It is decision-oriented rather than a chronological experiment log. Immediate execution state lives in `/home/tyler/CODEX/CURRENT_STATE.md`; the next manual handoff lives in `/home/tyler/CODEX/handoffs/CURRENT.md`; detailed accepted evidence remains in content-addressed manual-run result namespaces.

## Headline state

| Stage | Status | Headline |
|---|---|---|
| Foundation | ✅ COMPLETE | Map, PORTAL corpus, station mapping, observation panel, and identifiable traffic-state architecture frozen |
| Reconstruction correctness | ✅ COMPLETE | Logical-semantic and behavioral equivalence proven for frozen P60 experiment |
| Historical capture | ✅ COMPLETE | Two calibration dates captured with accepted station-speed and clean T7200 evidence |
| Realism diagnosis | ✅ COMPLETE | Systematic historical overcongestion proven |
| Mechanism localization | 🟡 CURRENT | Final exact per-road stock/arrival/departure campaign running |
| Parameter-family decision | ⬜ NEXT | Choose the smallest actionable calibration family from accepted mechanism evidence |
| Targeted calibration | ⬜ FUTURE | Controlled calibration experiments on calibration dates only |
| Historical calibration closure | ⬜ FUTURE | Freeze thresholds and demonstrate acceptable calibration-date behavior |
| 24-hour continuous traffic world | ⬜ FUTURE | Replace afternoon diagnostic construction with causal all-day state evolution |
| Full-day / multi-date calibration | ⬜ FUTURE | Cover representative temporal and seasonal classes without brute-forcing every day |
| Development validation | 🔒 UNINSPECTED | Open only after calibration choices and thresholds are frozen |
| Blind validation | 🔒 SEALED | Open once, after development closure |
| WorldPack freeze | ⬜ FUTURE | Compile validated traffic worlds into reusable local artifacts |
| Local Stage-1 web app integration | ⬜ FUTURE | Query frozen WorldPacks; never run microscopic MOSS per request |
| Scenario / closure validation | ⬜ FUTURE | Validate rerouting, spillover, queues, ETA effects, and recovery |
| Product polish / performance | ⬜ FUTURE | Optimize UX, latency, packaging, persistence, and resource use after scientific validity |

## Current diagnostic world versus future production world

### Current diagnostic world

- Two calibration dates: `2024-12-18` and `2025-05-29`.
- Local origin 11:30; causal prefix to 13:30; observation window 13:30–19:00.
- `rho_zero = 0`.
- P120 is `NONCONVERGED_DIAGNOSTIC_EXECUTION_BASIS`, not a converged warm-up or production equilibrium.
- Purpose: measure historical residuals and isolate mechanisms.
- It is not the final production architecture.

### Future production world

- Continuous 24-hour causal state evolution.
- Ninety-six 15-minute states per day.
- Multiple validated date/day classes.
- Explicit provenance: `OBSERVED`, `MODEL_CONSTRAINED`, `MODEL_EXTRAPOLATED`, or `UNKNOWN`.
- Frozen reusable WorldPacks for fast local route and scenario queries.
- No per-query microscopic MOSS simulation.

## Stage 1 — Map, data, and structural foundation

**STATUS:** ✅ COMPLETE

**PURPOSE:** Establish a reproducible Portland/Vancouver network, historical observation corpus, station mapping, and identifiable calibration representation.

**WHAT WAS PROVEN / DECIDED:**

- Portland/Vancouver MOSS map and graph foundation is established.
- PORTAL historical observations are integrated under frozen processing and health rules.
- Structural station-to-MOSS-road mapping and structural Level 1 are frozen.
- Structural coverage contains 316 eligible/resolved stations and 305 first-frontier relations.
- Historical panel: 287 stations × 20 calibration dates × 96 buckets/day = 551,040 identities.
- 484,968 identities are observed; 66,072 remain explicit `UNKNOWN`; no silent imputation is permitted.
- Route library: 49,186 canonical routes; structural rank 197; nullity 48,989.
- Exact physical OD cannot be uniquely inferred from station observations alone.
- Production-side estimation architecture: `PORTAL_CONSTRAINED_HISTORICAL_TRAFFIC_STATE` with `COMPACT_STRUCTURAL_BACKGROUND_V1`.
- OSM/SUMO artifacts remain structural scaffolding and lineage, not a behavioral oracle over PORTAL history.

**AUTHORITATIVE EVIDENCE:** Frozen manifests and observations under `/home/tyler/moss-gpu-test/historical-calibration`, with detailed lineage in accepted manual-run results.

**WHAT REMAINS:** No foundation task is on the active critical path. Missing microscopic facts remain unknown unless later evidence makes them decision-relevant.

**GATE TO NEXT STAGE:** Closed.

## Stage 2 — Replay and reconstruction correctness

**STATUS:** ✅ COMPLETE

**PURPOSE:** Prove that bounded replacement-engine reconstruction preserves the traffic state needed by the historical workflow.

**WHAT WAS PROVEN / DECIDED:**

- Final classification: `PROVEN_FOR_FROZEN_P60_EXPERIMENT` / `A_LOGICAL_SEMANTIC_RECONSTRUCTION_EQUIVALENCE_PASS`.
- Native lane-underflow defect fixed.
- Clean baseline native SHA-256: `136eddf35773ebda6cef7bc263658365d96be614036d3125af630871e41f3981`.
- P60 control final-state SHA-256: `6ef4f2d980754a237d12f005f23b49868cd26ed2d7a00b054152d4ecc4c79e05`.
- P60 population: 76,193 persons; SHA-256 `1a67243453e6be5d58481bd01d36fe175c1ff20171dbd56d33aea798dcfa4bc3`.
- Logical-semantic and behavioral-state equality were proven.
- Raw representation equality was unnecessary because remaining differences were source-proven nonsemantic ordering/state-projection effects.

**AUTHORITATIVE EVIDENCE:** `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-P60-FINAL-SEMANTIC-RECONSTRUCTION-ARM-V1/accepted`.

**WHAT REMAINS:** Reconstruction is not an open blocker. Reopen only on contradictory evidence.

**GATE TO NEXT STAGE:** Closed.

## Stage 3 — Historical baseline capture and realism diagnosis

**STATUS:** ✅ COMPLETE — SYSTEMATIC OVERCONGESTION PROVEN

**PURPOSE:** Compare rho-zero/P120-diagnostic MOSS speeds with PORTAL historical observations on calibration dates.

**WHAT WAS PROVEN / DECIDED:**

- Pre-roll convergence failed as `D_MIXED_TOTAL_AND_SPATIAL_NONCONVERGENCE`.
- P120 remains `NONCONVERGED_DIAGNOSTIC_EXECUTION_BASIS`; “causal prefix” is the accepted term.
- Two-date station-speed capture passed for `2024-12-18` and `2025-05-29`.
- Date 1 population: 547,335 persons; SHA-256 `7690ee9c2223be3a4b3708123cc41c063fa9f4db28b38d907ef177bc5b9dfa49`.
- Date 2 population: 596,040 persons; SHA-256 `b9ac1c53546ef697f7c1f192a39c93501ec84ef078c68100e28ddacbc86cd50b`.
- Each date uses 27,000 simulated seconds: 7,200-second causal prefix and 19,800-second observation window split into 22 × 900-second buckets.
- Common-domain coverage: 9,224 / 12,628 identities = 73.044%.
- Combined MOSS-minus-PORTAL bias `-29.130 km/h`; MAE `43.143 km/h`; RMSE `57.211 km/h`; materially-slower fraction `52.125%`.
- Bias worsens from about `-15.3 km/h` at 13:30 to `-54.4 km/h` at 18:45.
- Accepted classification: `A_RHO0_P120_HISTORICAL_SPEEDS_SYSTEMATICALLY_OVERCONGESTED`.
- This is a descriptive calibration diagnosis, not a final acceptance threshold or proof that P120 caused the error.

**AUTHORITATIVE EVIDENCE:**

- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-CLEAN-RECONSTRUCTION-BACKED-RHO0-HISTORICAL-STATION-SPEED-CAPTURE-V2/campaign-validation/summary.json`
- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-TWO-DATE-RHO0-HISTORICAL-SPEED-REALISM-DIAGNOSIS-V1/accepted`

**WHAT REMAINS:** No more baseline capture. Historical realism has not passed; there is no invented global MAE/RMSE threshold.

**GATE TO NEXT STAGE:** Closed with a failing realism diagnosis that motivates mechanism localization.

## Stage 4 — Mechanism localization

**STATUS:** 🟡 CURRENT — FINAL EXACT PER-ROAD CAMPAIGN RUNNING

**PURPOSE:** Separate excessive loading, carry-in, post-boundary accumulation, and local discharge/capacity behavior enough to choose an actionable parameter family.

**WHAT WAS PROVEN / DECIDED:**

- Speed/exposure classification: `D_MIXED_LOADING_AND_LOCAL_CAPACITY`.
- Spearman exposure versus signed residual is `-0.7531`; versus absolute error `+0.6987`.
- PORTAL volume versus MOSS exposure is extremely weak: station grain `+0.0557`, replication-aware road grain `+0.0604`.
- Of Q4 MOSS-exposure rows, 50.11% have Q1/Q2 PORTAL volume; a separate high-throughput/slow-discharge class is material.
- From 16:15 to 18:45, PORTAL volume falls 17.4%, MOSS exposure rises 35.4%, and speed bias worsens.
- Clean full-population T7200 capture passed on 225 mapped roads per date.
- T7200 totals: 9,776 vehicles / 8,166 waiting (83.53%) on Date 1; 10,113 / 8,504 (84.09%) on Date 2. These are descriptive, not overload thresholds.
- Accepted preload/accumulation classification: `D_DISTINCT_CORRIDOR_MECHANISM_CLASSES`; P120 carry-in evidence is `MIXED_BY_CORRIDOR`.
- Strict empirical classes: preload-like 1,226 rows / 38 roads; post-boundary accumulation-like 344 / 23; discharge-like 169 / 22.
- H51-west preload evidence applies only to its three-road mechanism-evidence subset, not all 12 mapped H51-west roads.
- H55 south is the post-boundary accumulation representative; H12 west remains discharge/local-road-like; H52 east and H6 south are controls.
- The additive native telemetry extension is behaviorally inert and exactly conservative. Module SHA-256 `c0b5e6bde589aa7d788715c29776c4741dff1c5136c12d556288f0f4b55c1927`, 79,110,792 bytes; status `SCIENTIFICALLY_ADMISSIBLE_FOR_DIAGNOSTIC_USE`, distinct from the clean baseline.
- Inertness/accounting proof: all P5–P60 semantic milestones matched; 62,620 roads × 12 intervals = 751,440 exact road/interval closure checks; zero nonzero residuals.
- Representative reconciliation freezes four domains: full station, full mapped road, mechanism evidence, and topology-safe aggregation.
- Forty full-mapped roads retained. H51 is 12 full / 3 mechanism / 0 topology-safe; H55 10 / 10 / 2; H12 8 / 8 / 0; H52 6 / 6 / 0; H6 4 / 4 / 0.
- H12 station 1095 remains `UNMAPPED_STATION_NO_MOSS_ROAD` / `NO_VALID_MOSS_ROAD_MAPPING` and never enters road accounting.
- Thirty-eight roads are branch/auxiliary; only H55 roads `200061419` and `200061420` are topology-safe for one-to-one aggregation.
- No accepted road among these 40 is disconnected, terminal/stub, continuation-identity-change, or topology-unresolved. A generic map-cutoff problem is not supported for this domain.
- Counters are exact per-road marginal arrivals/departures but lack source-destination pair identity. Exact external corridor flow is unavailable. The experiment is per-road exact transition accounting, not exact corridor flow.

**AUTHORITATIVE EVIDENCE:**

- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-RHO0-SPEED-ERROR-VS-LOCAL-VEHICLE-EXPOSURE-V1/accepted`
- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-RHO0-SPEED-EXPOSURE-VS-PORTAL-VOLUME-V1/accepted`
- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-CLEAN-FULL-POPULATION-T7200-MAPPED-ROAD-BOUNDARY-CAPTURE-V1/campaign-validation/summary.json`
- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-CLEAN-T7200-PRELOAD-VS-ACCUMULATION-DIAGNOSIS-V1/accepted`
- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-ROAD-TRANSITION-TELEMETRY-BEHAVIORAL-INERTNESS-V1/accepted`
- `/home/tyler/CODEX/manual-runs/results/HIST-MOSS-REPRESENTATIVE-CORRIDOR-DOMAIN-RECONCILIATION-V1/accepted`

**CURRENT RUN:** `HIST-MOSS-RHO0-REPRESENTATIVE-CORRIDOR-STOCK-ARRIVAL-DISCHARGE-DIAGNOSTIC-V1`.

- Date 1 (`2024-12-18`): `RUNNING / NOT ACCEPTED` at reconciliation time.
- Date 2 (`2025-05-29`): `PENDING / NOT ACCEPTED`.
- The supplied `simulation_second=16500/27000` heartbeat is operational progress only, not scientific evidence.
- Owner launch SHA-256: `f2774febe7c5be59d9b9d089643439c63bd232222a90c9dcdade8ad5607a25fc`.
- Runtime contract: `SEALED_SCOPE_PATH_V2`.
- No date is accepted until independent candidate validation and campaign acceptance artifacts exist.

**WHAT REMAINS:** Review accepted two-date per-road output, if and when Tyler supplies it, and determine whether arrivals/departures make the calibration family actionable.

**GATE TO NEXT STAGE:** Stop gathering mechanism evidence if this exact telemetry result is sufficient. Do not invent another diagnostic layer by default.

## Stage 5 — Parameter-family decision

**STATUS:** ⬜ NEXT

**PURPOSE:** Convert accepted mechanism evidence into the smallest defensible calibration intervention family.

**WHAT WAS PROVEN / DECIDED:** Current evidence rules out treating one global demand multiplier or uniform speed offset as established. The decision must preserve distinct corridor mechanism classes unless exact transition evidence supports consolidation.

**AUTHORITATIVE EVIDENCE:** Accepted Stage-4 artifacts plus the pending exact per-road campaign result when independently accepted.

**WHAT REMAINS:** Choose among pre-boundary loading/carry-in, observation-window temporal loading, spatial demand allocation, local signal/junction discharge, lane/car-following/road behavior, or explicitly mixed corridor classes. Do not set values yet. A global demand multiplier or uniform speed offset is not justified by current heterogeneity.

**GATE TO NEXT STAGE:** Written causal hypothesis, representative controls, frozen outcomes, and explicit rejection conditions.

## Stage 6 — Targeted calibration experiments

**STATUS:** ⬜ FUTURE

**PURPOSE:** Test the selected family with the smallest controlled calibration-side changes.

**WHAT WAS PROVEN / DECIDED:** Experiments must preserve the accepted rho-zero/P120-diagnostic baseline and isolate one selected family or one explicit split-family comparison.

**AUTHORITATIVE EVIDENCE:** Stage-5 decision artifact; no targeted-calibration result exists yet.

**PLAN:** Change one justified family at a time; preserve rho-zero/P120 evidence as immutable control; score historical speed and exact stock/transition behavior; split loading-like and discharge-like roads when needed; use calibration dates only; never treat MOSS output as historical observation.

**GATE TO NEXT STAGE:** Candidate improves preregistered calibration outcomes without compensating failures elsewhere.

## Stage 7 — Historical calibration closure

**STATUS:** ⬜ FUTURE

**PURPOSE:** Freeze a historically credible calibration candidate before held-out evaluation.

**WHAT WAS PROVEN / DECIDED:** No overall historical realism threshold has yet been frozen, so current baseline error cannot be relabeled pass/fail beyond the accepted descriptive diagnosis.

**AUTHORITATIVE EVIDENCE:** Future accepted calibration campaign and preregistered closure contract.

**WHAT REMAINS:** Define thresholds before claiming success; repeat frozen calibration evidence; require temporal, spatial, congestion-state, and mechanism closure; freeze model, observation operator, missingness policy, objectives, and thresholds.

**GATE TO NEXT STAGE:** Calibration-side closure accepted and immutable.

## Stage 8 — Continuous 24-hour historical traffic world

**STATUS:** ⬜ FUTURE — MAJOR DIRECTION CHANGE

**PURPOSE:** Replace the afternoon-only diagnostic construction with a causal, reusable all-day historical world.

**WHAT WAS PROVEN / DECIDED:** The afternoon P120 world is diagnostic only. Production requires causal all-day state and honest source provenance rather than a larger afternoon snapshot.

**AUTHORITATIVE EVIDENCE:** Accepted nonconvergence, historical speed, and mechanism evidence summarized in Stages 3–4; no 24-hour production world exists yet.

**PLAN:**

- Represent 96 causal 15-minute states per day.
- Carry traffic state across buckets and midnight where scientifically supported.
- Preserve per-value provenance: `OBSERVED`, `MODEL_CONSTRAINED`, `MODEL_EXTRAPOLATED`, or `UNKNOWN`.
- Use PORTAL history, adjacent observations, weekday/date context, topology, and validated dynamics to constrain gaps.
- Never label model-generated values as historical observations.
- Reduce dependence on artificial P120; overnight/carry-in initialization remains unresolved.
- Do not freeze file format, interpolation algorithm, day taxonomy, or objective weights yet.

**GATE TO NEXT STAGE:** Frozen causal all-day state contract with honest provenance and initialization semantics.

## Stage 9 — Full-day and multi-date calibration

**STATUS:** ⬜ FUTURE

**PURPOSE:** Establish temporal/seasonal coverage without prematurely simulating every date.

**WHAT WAS PROVEN / DECIDED:** The frozen 20/20/20 calibration/development/blind split remains, but the exact representative full-day date set and day taxonomy are intentionally unselected.

**AUTHORITATIVE EVIDENCE:** Future Stage-8 all-day contract and calibration-date selection artifact.

**PLAN:** Select representative dates/day classes after the 24-hour contract exists. Calibrate onset, peak, dissipation, overnight carry-in, weekday, and seasonal variation. Exact date count and taxonomy remain open.

**GATE TO NEXT STAGE:** Frozen candidate and held-out protocol.

## Stage 10 — Development validation

**STATUS:** 🔒 UNINSPECTED

**PURPOSE:** Test generalization after calibration-side choices are frozen.

**WHAT WAS PROVEN / DECIDED:** The development partition exists and remains uninspected.

**AUTHORITATIVE EVIDENCE:** Frozen split manifests only; no development result has been opened.

**WHAT REMAINS:** Freeze the model, operator, missingness policy, metrics, thresholds, and evaluation plan before access.

**RULES:** Development evidence may not select calibration architecture opportunistically. Opening requires a frozen model, observation operator, missingness policy, metrics, thresholds, and evaluation plan.

**GATE TO NEXT STAGE:** Frozen development contract passes, or candidate returns to calibration without viewing blind evidence.

## Stage 11 — Blind validation

**STATUS:** 🔒 SEALED

**PURPOSE:** One final traffic-fidelity evaluation with no blind-driven tuning.

**WHAT WAS PROVEN / DECIDED:** The blind partition exists and remains sealed.

**AUTHORITATIVE EVIDENCE:** Frozen split/sealing provenance only; no blind result has been opened.

**WHAT REMAINS:** Complete calibration and development closure before one blind evaluation.

**RULES:** Open only after calibration freeze and development closure. Blind identities and outputs remain inaccessible until then.

**GATE TO NEXT STAGE:** One accepted blind evaluation under the frozen contract.

## Stage 12 — WorldPack freeze

**STATUS:** ⬜ FUTURE

**PURPOSE:** Compile validated historical traffic consequences into portable, reusable local artifacts.

**WHAT WAS PROVEN / DECIDED:** PC/MOSS/WorldPack/Mac separation works and WorldPack v1.2 remains the representation benchmark, but the source traffic world is not yet historically trustworthy.

**AUTHORITATIVE EVIDENCE:** Existing WorldPack v1.2 representation-fidelity artifacts and future validated traffic-world results.

**WHAT REMAINS:** Freeze the traffic world first, then choose format/compression and prove representation fidelity.

**PLAN:** Retain WorldPack v1.2 as the representation benchmark until a candidate beats it on the same evaluation; separate observed evidence, model-constrained estimates, extrapolation, and unknowns; preserve movement/topology and queue-tail semantics; precompute reusable worlds; leave exact format/compression open.

**GATE TO NEXT STAGE:** Frozen WorldPack contract, provenance, loader, and representation-fidelity evidence.

## Stage 13 — Local Stage-1 web app integration

**STATUS:** ⬜ FUTURE

**PURPOSE:** Expose trustworthy traffic worlds through a local-first map product.

**WHAT WAS PROVEN / DECIDED:** The app architecture is feasible, but product integration is deliberately downstream of traffic-world credibility.

**AUTHORITATIVE EVIDENCE:** Existing local routing/WorldPack architecture proofs; no production historical-world integration exists yet.

**PLAN:** Map-first origin/destination and departure-time workflow; later arrive-by; closures/restrictions; scenario comparison; alternatives; ETA; supported reliability/arrival risk; save/load; Google Maps handoff/export; local/LAN operation; no required cloud/API keys; no per-query MOSS.

**DECISION:** The frontend could be built now, but it is intentionally not the current blocker because the traffic world is known to be wrong.

**WHAT REMAINS:** Freeze and load a trustworthy reusable WorldPack, then implement the local-first query workflow without per-query MOSS.

**GATE TO NEXT STAGE:** Product queries a frozen reusable WorldPack with acceptable local latency.

## Stage 14 — Scenario and closure validation

**STATUS:** ⬜ FUTURE

**PURPOSE:** Validate intervention behavior separately from baseline historical calibration.

**WHAT WAS PROVEN / DECIDED:** Baseline historical calibration alone cannot establish closure/spillover credibility.

**AUTHORITATIVE EVIDENCE:** Future frozen scenario-validation contract and accepted scenario artifacts.

**PLAN:** Use synthetic/known closures and historical incidents where suitable evidence exists to test rerouting, spillover, queue formation, route switching, ETA changes, and recovery.

**WHAT REMAINS:** Select evidence-backed scenarios and thresholds after baseline WorldPack freeze.

**GATE TO NEXT STAGE:** Scenario responses satisfy a frozen credibility contract.

## Stage 15 — Product polish and performance

**STATUS:** ⬜ FUTURE

**PURPOSE:** Make the validated local product fast, robust, and usable.

**WHAT WAS PROVEN / DECIDED:** Scientific validity precedes cosmetic and packaging optimization.

**AUTHORITATIVE EVIDENCE:** Future accepted WorldPack/app/scenario artifacts.

**PLAN:** Query latency, WorldPack loading/caching, alternatives, map/scenario UX, error handling, persistence, packaging, Mac/LAN workflow, resource use, and documentation.

**WHAT REMAINS:** Execute the product-performance work after upstream scientific gates close.

**GATE TO NEXT STAGE:** Final local/LAN product readiness; no later roadmap stage is currently defined.

## Superseded, merged, split, and moved work

### ↪ SUPERSEDED / NO LONGER PRIMARY

- Exact OD recovery from PORTAL → `PORTAL_CONSTRAINED_HISTORICAL_TRAFFIC_STATE` plus `COMPACT_STRUCTURAL_BACKGROUND_V1`.
- SUMO as behavioral oracle → structural/topology lineage only.
- WorldPack v0 road-average speed as production primitive → rejected; movement-aware v1.2 remains benchmark.
- XML/RouteConverter normal replay materialization → direct route-template materialization; XML remains golden reference.
- Live incremental person ingestion → blocked by fixed-population architecture; bounded reconstruction proven instead.
- Pre-roll selection by convergence → failed; P120 is only a nonconverged diagnostic causal prefix.
- Waiting counts as substitute for historical speed → rejected.
- Exact external corridor flow from marginal road counters → unavailable; per-road exact transition accounting is current.
- Generic map cutoff as explanation for the 40 representative roads → unsupported by accepted topology evidence.
- Frontend as current blocker → moved after traffic-world scientific validity.

### MERGED / SPLIT

- Historical prediction generation split into station-speed capture, realism diagnosis, exposure/volume analysis, T7200 capture, and exact transition accounting.
- Warm-up selection reframed as causal-prefix diagnosis plus future continuous-state initialization.
- Traffic calibration split into mechanism localization, parameter-family decision, targeted calibration, and closure.
- WorldPack work split into 24-hour traffic-world design, multi-date validation, WorldPack freeze, app integration, and scenario validation.

## Completed scientific milestones

- Frozen observation panel and no-imputation policy.
- Identifiability analysis and compact structural background decision.
- Direct route-template population materialization.
- P60 reconstruction correctness proof.
- P120 nonconvergence diagnosis.
- Two-date rho-zero station-speed capture and artifact-only recovery.
- Systematic overcongestion diagnosis.
- Exposure, PORTAL-volume, and clean-T7200 mechanism analyses.
- Exact telemetry extension, isolated build, behavioral inertness, and conservation proof.
- Topology-aware representative-domain reconciliation.

Detailed failures and superseded experiments remain immutable in their original result namespaces; they are not active roadmap steps.

## Scientific discipline and stop rules

- Current work is calibration-only. Development remains uninspected. Blind remains sealed.
- “Historical observation” means PORTAL measurement. Model output is a model-constrained estimate or extrapolation.
- `UNKNOWN` remains unknown; no zero filling, interpolation, date substitution, or station substitution without a frozen method.
- MOSS map coverage, PORTAL coverage, full mapped-road inventory, mechanism-evidence subset, and topology-safe subset are distinct.
- Do not tune rho, demand, routes, signals, lanes, car-following, road speeds, or causal-prefix duration before a parameter-family decision.
- If the running telemetry campaign resolves that decision, proceed to Stage 5 instead of adding another diagnostic.
- WorldPack calibration must never hide a traffic-model error.

## Immediate sequence

1. Do not interfere with Tyler’s currently running Date-1 exact per-road campaign.
2. Review only an accepted output or preserved failure supplied after the run.
3. Complete Date 2 through existing atomic resume logic if Date 1 is accepted.
4. Review the accepted two-date result and make the Stage-5 parameter-family decision.
5. Author the smallest targeted calibration experiment; do not tune globally.

No campaign command is included because the campaign is already running.

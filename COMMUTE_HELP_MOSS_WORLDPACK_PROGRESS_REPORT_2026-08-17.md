# Commute Help / MOSS GPU / WorldPack Research Checkpoint

**Checkpoint date:** 2026-08-17  
**Primary PC workspace:** `~/moss-gpu-test` under WSL Ubuntu 22.04  
**Primary simulation hardware:** NVIDIA Quadro P4000  
**Current status:** WorldPack architecture proven; road-average-speed representation rejected; movement-aware WorldPack v1 topology and holdout validation working; WorldPack v1.2 remains the official representation baseline. The historical Golden I-5 PORTAL pilot is now running. An artificial MOSS source-boundary queue was corrected, a controlled SUMO-vs-MOSS comparison isolated a strategic route-aware lane-positioning defect in MOSS, and a causal `next_road_lane_plans` experiment removed the premature first-fork collapse. The diagnostic propagation over-constrains upstream A traffic, so the next simulator task is a production-quality multi-road route-aware lane-planning fix rather than more route-split tuning.

---

## 1. Executive summary

The project began with a practical question: can a Windows PC with a Quadro P4000 perform the expensive traffic-simulation work for Commute Help while the Mac remains the application-development and routing machine?
 
The answer is now **yes**.

The architecture has been demonstrated end to end:

```text
WINDOWS PC / WSL / P4000
        |
        | MOSS traffic physics
        v
WorldPack compiler
        |
        | portable immutable traffic representation
        v
MAC / COMMUTE HELP
        |
        | passive single-probe routing
        v
ETA / route choice
```

The PC computes the traffic world. The Mac does not need CUDA, MOSS, SUMO, or a continuously running microscopic simulation to answer a route query. It only needs the compiled WorldPack.

The first WorldPack representation, v0, stored road-level average speed over time. That proved the architecture, but it also exposed a serious physical modeling defect: a single stopped vehicle near the end of a long road could make the entire road appear stopped. The most important demonstrated example was a roughly 494 m road where one vehicle was stopped about 1 m from the end. WorldPack v0 treated the whole 494 m road as effectively stopped.

That finding forced a representation change rather than another smoothing patch.

WorldPack v1 now treats the important primitive as approximately:

```text
(from_road_id, to_road_id, entry_time) -> arrival_time
```

This movement-specific model can represent different delays for different turns leaving the same road and is much better aligned with junction behavior.

A complete static movement universe has now been extracted from the MOSS CityProto map:

- 18,425 WorldPack roads
- 6,950 junctions
- 55,559 unique legal road-to-road movements
- 59,467 connector-lane references
- 0 missing connector lanes
- 0 connector-parent mismatches
- 0 multi-junction duplicate movement pairs
- 0 mixed-turn movement pairs
- 0 self movements

A 20% person-level holdout test has also produced the first quantitative WorldPack accuracy scoreboard.

WorldPack v1.2 linear-interpolation baseline:

- median absolute error: **2.84 s**
- mean absolute error: **18.34 s**
- p90 absolute error: **60.80 s**
- p95 absolute error: **119.21 s**
- mean bias: **-16.21 s**
- RMSE: **44.72 s**

For essentially uncongested movement observations where true delay was at most 2 s:

- median absolute error: **0.53 s**
- mean absolute error: **0.77 s**
- p95 absolute error: **1.73 s**

The major remaining weakness is severe queueing. For observations with more than 60 s of true delay:

- mean absolute error: **47.95 s**
- bias: **-45.03 s**
- RMSE: **79.18 s**

This means WorldPack is usually very good in free-flow and mild-delay conditions, but frequently underestimates large queues because there is not yet enough spatial queue-state evidence.

The next major modeling experiment is therefore **lane-level queue geometry**, not another road-average-speed refinement.

At the same time, the project plan has been adjusted so real PORTAL historical data enters sooner. Rather than wait until WorldPack is “finished,” a small historical golden slice should be used to calibrate and validate MOSS while WorldPack itself is also being validated against MOSS. This creates two independent accuracy boundaries:

```text
PORTLAND HISTORICAL DATA
        |
        | reality validation
        v
MOSS
        |
        | representation validation
        v
WORLDPACK
        |
        v
COMMUTE HELP
```

Since the 2026-08-16 checkpoint, the project has moved from planning the historical golden slice to actively running it. The selected Golden I-5 pilot uses real PORTAL observations from **Thursday 2024-02-01**, with a 13:30-18:30 context window, a 14:30-18:30 scoring window, 15-minute historical buckets, and a 60-minute warmup. The first historical demand world contains 18,726 vehicles split across two observed upstream sources (A: 9,900; B: 8,826).

The historical work uncovered two simulator-side effects that had to be separated from real traffic calibration. First, spawning A traffic directly on the historical detector road created an artificial birth queue; moving the generation boundary to a farther-upstream four-lane predecessor restored roughly historical A throughput. Second, MOSS delayed route-required lane positioning until the first physically discriminating fork, producing an early artificial bottleneck that SUMO did not reproduce. A controlled metadata-only propagation of direct-compatible lane ranges moved the route knowledge upstream, drove wrong-side fork entries to 0%, and restored near-free-flow fork speeds. That experiment is causal evidence for a specific MOSS strategic lane-horizon limitation, not a reason to discard MOSS wholesale.

The long-term target remains a calibrated Traffic WorldAtlas with weekday, time-of-day, month, season, year-over-year, calendar, and scenario-aware traffic worlds.

---

## 2. Primary objective

Commute Help should eventually answer questions such as:

> If a driver leaves at a particular time, under a particular closure or traffic scenario, what route should they take and what ETA should they expect?

The intended runtime model is **not** to launch a fresh microscopic simulation for every user query.

Instead:

1. Historical and network data create a realistic baseline traffic world.
2. MOSS runs the expensive traffic physics on the P4000.
3. Traffic consequences are compiled into a portable WorldPack.
4. Commute Help on the Mac routes a passive probe through the already-computed traffic world.
5. Scenario worlds can later branch from appropriate baseline states.

The working metaphor has been:

> The PC computes the ocean. WorldPack records the ocean. The Mac drops one new droplet into it.

The probe car should not materially change the traffic world around it.

---

## 3. Hardware and execution environment

### Windows PC

Known graphics devices:

- NVIDIA Quadro P4000
- NVIDIA GT 1030
- Intel UHD 630

The Quadro P4000 is the relevant CUDA simulation device.

Known environment:

- Windows 11
- WSL Ubuntu 22.04
- WSL user: `tyler`
- project: `~/moss-gpu-test`
- CUDA-capable custom MOSS build
- Python virtual environment: `~/moss-gpu-test/.venv`

Custom MOSS/CUDA work has included Pascal compute capability 6.1 support, WSL-related guards, NVTX3 compatibility, and corrected route-conversion vehicle attributes.

### Mac

Commute Help remains primarily on the Mac.

Architecture direction:

- FastAPI / Python orchestration
- local files and SQLite
- NetworkX planning/routing infrastructure
- MapLibre/React frontend
- no MOSS or CUDA requirement inside FastAPI
- WorldPack as the simulator/runtime transfer boundary

---

## 4. P4000 feasibility result

The P4000 is viable for the current pilot simulation workload.

Representative corrected MOSS real-time factors observed:

| Simulation | Approx. RTF |
|---|---:|
| 300 s | 16.74x |
| 500 s | 16.79x |
| 1,000 s | 16.02x |
| 25k persons / 600 s | 6.18x |
| 25k persons / 4,200 s | 6.28x |

Important limitation: tests previously described as 100k or 1M-person scaling tests were actually capped at about 25k persons. Genuine scaling above 25k has not been established.

---

## 5. Corrected MOSS speed-recognition defect

An early MOSS problem was traced to converted person/vehicle attributes rather than an inherent failure of MOSS itself.

The corrected `mosstool RouteConverter` defaults were:

```python
"lane_max_speed_recognition_deviation": 1.0,
"headway": 1.0,
```

Known patch artifact:

```text
artifacts/patches/mosstool-sumo-speed-recognition-v1.patch
```

Known patch SHA-256:

```text
7f274694e611e6010ea835c8cf387b94501fc6fd48e1a33cefa93a535ae8c1a9
```

Known corrected persons SHA-256:

```text
455f90f5d2311d2e8593d5a4fb316740952309129c12811e15c5e5205a3df448
```

The earlier deviation value of zero could create a zero target speed and pathological IDM behavior. This was treated as a converter-data defect, not evidence that native MOSS needed another patch.

---

## 6. WorldPack v0 architecture proof

### Purpose

WorldPack v0 answered:

> Can MOSS run on the PC, produce a compact portable traffic representation, and let Commute Help route through it later on the Mac?

Yes.

### Representative 4,200-second WorldPack

Artifact name:

```text
portland-pilot-25k-4200-speedfix-v1
```

PC/WSL source:

```text
~/moss-gpu-test/artifacts/worldpacks/portland-pilot-25k-4200-speedfix-v1
```

Mac destination used during integration:

```text
/Users/tylersmith/coding/Projects/PERSONAL/SMALL/commute-help/data/worldpacks/portland-pilot-25k-4200-speedfix-v1
```

World ID:

```text
wp0-945ed633dfc7950ab19dc77d
```

Export configuration:

- 25,000 persons
- 4,200 simulation seconds
- 5-second snapshots
- seed 43
- CUDA device 0

Representative validation:

```text
WORLDPACK V0 VALIDATION PASS
world id      : wp0-945ed633dfc7950ab19dc77d
snapshots     : 841
roads         : 18,425
matrix cells  : 15,495,425
output size   : 11.39 MiB
simulation RTF: 6.280x
road speed    : min=0.000, median=0.000, max=27.708 m/s
peak road vehicles: 22,887
source mappings: 18,425/18,425
```

This proved the portable representation could be compact enough for practical transfer and loading.

---

## 7. GPU nondeterminism observation

The nominally identical 4,200-second simulation was accidentally run twice.

Both runs had the same v0 world ID because v0 identity represented deterministic configuration rather than exact compiled content.

However, the GPU simulation traces eventually diverged slightly:

- snapshot streams were identical through about t=355 s
- they diverged around t=360 s by one vehicle
- end-state road-attributed vehicle counts differed

This established an experimental rule:

> Evidence that will be compared directly should come from the same stochastic simulation realization whenever possible.

WorldPack v1 moved toward content-addressed identity so a world ID reflects compiled content rather than only configuration.

This also affects queue-model validation. Queue geometry from a new MOSS run should not simply be compared to trajectories from an older run as if the underlying traffic world were identical.

---

## 8. Mac routing proof with WorldPack v0

A passive FIFO router was integrated on the Mac.

Representative benchmark:

| Depart | ETA min | Arrival | Miles | Edges | Wall ms |
|---:|---:|---:|---:|---:|---:|
| 60 | 39.42 | 2425 | 11.27 | 155 | 1297.88 |
| 180 | 37.42 | 2425 | 11.72 | 160 | 1287.20 |
| 300 | 35.42 | 2425 | 11.72 | 160 | 1601.38 |
| 480 | 32.66 | 2440 | 11.16 | 160 | 1297.50 |

This proved:

- the Mac can load WorldPack;
- a passive probe can route through the precomputed world;
- query-time routing does not require a running simulator.

The route behavior also exposed suspicious arrival convergence, leading directly to the v0 representation investigation.

---

## 9. Why WorldPack v0 was rejected for fidelity

WorldPack v0 used approximately:

```text
road + time -> arithmetic mean vehicle speed
```

This is too spatially coarse.

### Core pathology

A long road can contain one stopped car near an intersection while most of the road remains physically traversable.

Important example:

- MOSS road `200016846`
- length approximately 494.38 m
- free-flow traversal approximately 55.30 s

Observed person state:

```text
t=580      person 24892  s=487.31m  distance-to-end=7.06m  v=5.514
t=585..715 person 24892  s=493.32m  distance-to-end=1.06m  v=0
t=720      road empty
```

WorldPack v0 effectively converted this into:

> the whole 494 m road has speed 0

That is not what MOSS simulated.

The problem is not merely numerical smoothing. It is a wrong primitive.

### Sparse-stop audit

Across the 4,200-second v0 pack:

```text
all road/time cells       : 15,495,425
occupied cells            : 1,806,886
fully stopped cells       : 1,179,977
sparse stopped <=2 veh    :   497,643
share stopped that sparse : 42.17%
roads ever sparse-stopped : 13,000 / 18,425
```

Sparse-stop episodes:

- 43,469 total
- median duration: 45 s
- p95 duration: 170 s
- maximum duration: 3,495 s
- 15,081 episodes >= 60 s
- 5,840 episodes >= 120 s
- 93 episodes >= 300 s

Only 464 affected roads were at least 200 m long. This adds an important nuance:

- on tiny links, one stopped vehicle can legitimately occupy much of the link;
- on longer links, whole-road averaging is especially dangerous.

### Decision

Road-average speed is retained only as historical diagnostic evidence.

It is not the production WorldPack traffic primitive.

---

## 10. Confirmation that MOSS exposes longitudinal position

The installed MOSS API was inspected directly.

Relevant `fetch_persons` fields include:

```text
id
enable
status
lane_id
lane_parent_id
s
v
schedule_index
trip_index
departure_time
traveling_time
total_distance
...
```

The important discovery was `s`, longitudinal position on the current lane.

This enables spatial queue reconstruction.

The project also confirmed that:

```python
from moss.engine import DRIVING
```

is needed; `DRIVING` is not exported from the top-level `moss` package.

---

## 11. WorldPack v1 conceptual shift

The preferred traffic primitive became:

```text
(from_road_id, to_road_id, entry_time) -> arrival_time
```

Instead of asking:

> What was the average speed on this road?

WorldPack asks:

> If a probe enters this road intending to continue to this next road at this time, when should it arrive there?

This naturally represents intersection delay and turn-specific queueing.

### Important empirical example

The first movement prototype showed substantially different costs from the same approach road depending on the downstream path.

The originally observed 1-second sampled trajectories showed:

```text
200016846 -> 200002449
  min about 55.30s
  max about 57.99s

200016846 -> 200014280
  max about 195.69s
```

Later topology analysis showed the second apparent direct movement was temporally aliased and the legal path is:

```text
200016846 -> 200014279 -> 200014280
```

with `200014279` only about 1.56 m long.

Even with that correction, the evidence supports the core principle: downstream movement/path choice can experience radically different traffic consequences from the same approach road.

---

## 12. Empirical traversal audit

A 900-second run produced:

```text
completed traversals       : 334,492
roads with >=1 traversal   : 16,417 / 18,425
roads >=2                  : 14,255
roads >=5                  : 9,183
roads >=10                 : 6,273
roads >=25                 : 3,603
roads >=50                 : 1,878
observed road/time buckets : 202,918
```

Not all observations were full-road traversals because trips can begin or end partway through a road.

Eligibility audit:

```text
raw observations          : 334,492
FULL                      : 165,609 (49.51%)
PARTIAL_START             : 74,930 (22.40%)
PARTIAL_END               : 49,394 (14.77%)
PARTIAL_BOTH              : 44,559 (13.32%)
```

Full-traversal support:

```text
roads with full traversal : 11,597 / 18,425
roads >=2 full            : 9,318
roads >=5                 : 6,201
roads >=10                : 4,184
roads >=25                : 1,858
roads >=50                : 718
```

This demonstrated that empirical trajectories are valuable anchors but are not dense enough to be the entire WorldPack field.

---

## 13. WorldPack v1 first movement compiler

The first 900-second movement compiler produced:

```text
world id                  : wp1-7511bf858db935b3d1fe8bf7
raw movement observations : 317,073
eligible full observations: 154,993
movement keys             : 15,000
sample times              : 181
direct anchor cells       : 114,038
FIFO propagated cells     : 365,127
free-flow fallback cells  : 2,235,835
expired pending movements : 15,128
```

The compiler:

1. tracked person entry to a road;
2. tracked the transition off that road;
3. waited through internal/junction state;
4. finalized `(from_road, to_road)` on the next road;
5. grouped observations into 5-second buckets;
6. used median direct arrival within a bucket;
7. imposed a static free-flow lower bound;
8. applied cumulative maximum over time to produce a FIFO-safe arrival curve.

Source codes:

```text
0 = free_flow_fallback
1 = direct_empirical_anchor
2 = fifo_propagated
```

The world ID became content-addressed from canonical compiled arrays.

---

## 14. The 60-second junction-gap cutoff defect

The first movement compiler used a 60-second maximum pending junction gap.

An audit showed the observed gap distribution was still populated all the way to the cutoff:

```text
p50   :  2s
p90   :  6s
p95   :  9s
p99   : 42s
p99.5 : 52s
p99.9 : 59s
max   : 60s
```

Counts near the cutoff were not collapsing:

```text
45s : 128
46s : 176
47s : 159
48s : 192
49s : 189
50s : 163
51s : 190
52s : 180
53s : 201
54s : 194
55s : 172
56s : 180
57s : 208
58s : 194
59s : 189
60s : 174
```

Conclusion:

> 60 s was not a natural distribution boundary. It was censoring legitimate congestion.

The cutoff was replaced with trip-continuity identity using:

```text
same person
+ same schedule_index
+ same trip_index
```

A long watchdog remained only as a technical safety mechanism.

---

## 15. WorldPack v1.1 result

The corrected same-trip compiler produced:

```text
world id                  : wp1-532d1937c59e70f7b25ca572
raw movement observations : 328,882
eligible full observations: 162,396
movement keys             : 15,119
sample times              : 181
direct anchor cells       : 118,582
FIFO propagated cells     : 406,945
free-flow fallback cells  : 2,211,012
expired pending watchdogs : 0
trip identity breaks      : 772
```

Compared with the first v1 compiler:

```text
raw movement observations    +11,809
eligible full observations    +7,403
movement keys                   +119
direct anchor cells           +4,544
FIFO propagated cells        +41,818
free-flow fallback cells     -24,823
```

The old 60-second expiration count was 15,128. Under trip identity:

- watchdog expirations: 0
- actual identity breaks: 772

This strongly confirmed that the 60-second semantic cutoff had been discarding valid traffic behavior.

---

## 16. Complete legal movement topology

A major v1 limitation remained: only movements actually seen in the 900-second run existed in the movement field.

That is unsafe for routing because a legal turn with no sampled vehicles must remain a legal movement with fallback cost rather than disappear from the network.

The CityProto map schema was inspected.

### Road

```text
id
name
lane_ids[]
next_road_lane_plans[]
```

### Lane

```text
id
type
turn
max_speed
length
predecessors[]
successors[]
parent_id
...
```

### JunctionLaneGroup

```text
in_road_id
in_angle
out_road_id
out_angle
lane_ids[]
turn
```

`JunctionLaneGroup` is the canonical source for legal road-to-road movements.

Static compiler result:

```text
WorldPack roads                : 18,425
junctions                      : 6,950
junctions with driving groups  : 6,950
legal driving-lane groups      : 55,559
unique legal movement pairs    : 55,559
roads with outgoing movement   : 18,400
roads with incoming movement   : 18,400
connector lane references      : 59,467
missing connector lanes        : 0
connector parent mismatches    : 0
movement pairs >1 junction     : 0
movement pairs >1 turn type    : 0
self movements                 : 0
```

Turn distribution:

```text
LEFT      : 27,068
RIGHT     : 13,080
STRAIGHT  : 15,411
```

This static topology is now treated as authoritative.

---

## 17. Temporal aliasing discovery

When v1.1 observed movements were compared with static legal topology:

```text
observed v1.1 movement pairs   : 15,119
observed found in static map   : 14,145
observed missing from static   : 974
```

At first this looked like a topology inconsistency.

A bounded graph audit proved otherwise.

All 974/974 supposedly illegal observed jumps resolved through the legal static graph within at most five edges.

```text
recoverable <=6 edges : 974 / 974
unresolved            : 0
```

Path-length distribution:

```text
2 edges : 793
3 edges : 157
4 edges : 23
5 edges : 1
```

Skipped intermediate-road distance:

```text
median total skipped : 2.06m
p90 total skipped    : 8.12m
p95 total skipped    : 10.50m
<= 1m                : 37.89%
<= 5m                : 74.13%
<= 10m               : 94.15%
<= 20m               : 99.59%
```

Known example:

```text
1-second observer:
200016846 -> 200014280

legal topology:
200016846 -> 200014279 -> 200014280

200014279 length: ~1.56m
```

Conclusion:

> The 1-second person sampler can skip tiny road segments completely.

This is temporal aliasing, not broken map topology.

Decision:

> CityProto topology is authoritative. Observed jumps that violate direct topology must be reconciled against the static graph, not promoted as legal movements.

---

## 18. WorldPack v1.2 complete-topology compiler

WorldPack v1.2 compiled all 55,559 legal movements.

Result:

```text
world id                 : wp1-871b61e142900dcdb3f52aaf
legal movements          : 55,559
sample times             : 181
movement/time cells      : 10,056,179
```

Observation accounting:

```text
raw rows                 : 328,882
full rows                : 162,396
direct legal rows        : 155,895
aliased rows excluded    : 6,501
```

The accounting closes exactly:

```text
155,895 direct legal
+ 6,501 aliased
= 162,396 full observations
```

Compiled field:

```text
direct anchor cells      : 113,275
FIFO propagated cells    : 372,373
free-flow fallback cells : 9,570,531
supported field share    : 4.83%
```

Static free-flow diagnostics:

```text
movement FF median       : 8.77s
movement FF p95          : 21.83s
connector FF median      : 2.06s
connector FF p95         : 2.58s
connector speed misses   : 0
```

The lower supported-field percentage is expected because the denominator now contains the entire legal movement universe rather than only movements that happened to receive traffic.

---

## 19. Person-level 80/20 holdout methodology

The first real WorldPack accuracy test used a deterministic five-fold person split.

One fold, approximately 20% of people, was hidden from training.

Important design choice:

> Split by person, not by row.

This prevents one person's movement observations from leaking into both training and test data.

Resulting split:

```text
training persons        : 19,652
holdout persons         : 4,949
training observations   : 124,430
holdout observations    : 31,465
person leakage          : 0
```

The test asked:

> If WorldPack has not seen these people, how accurately can it predict the arrival times they actually experienced in MOSS?

Three lookup policies were compared:

- left / causal sample
- linear interpolation
- right / ceil sample

### Lookup-policy result

```text
left / causal:
median = 4.98s
mean   = 22.44s
p90    = 77.84s
p95    = 125.82s
bias   = -20.96s
RMSE   = 49.82s

linear:
median = 2.84s
mean   = 18.34s
p90    = 60.80s
p95    = 119.21s
bias   = -16.21s
RMSE   = 44.72s

right / ceil:
median = 2.92s
mean   = 16.87s
p90    = 56.01s
p95    = 116.97s
bias   = -11.95s
RMSE   = 43.23s
```

The difference shows that 5-second temporal discretization itself matters.

Linear interpolation was used as the principal analysis baseline, while the right/ceil result suggests future arrival-curve representation work may improve time reconstruction further.

---

## 20. Holdout accuracy by coverage

### Movement seen in training

```text
n      = 30,321
median = 2.82s
mean   = 17.33s
p90    = 58.08s
p95    = 116.73s
bias   = -15.12s
RMSE   = 43.22s
```

### Movement unseen in training

```text
n      = 1,144
median = 8.13s
mean   = 45.12s
p90    = 124.49s
p95    = 176.25s
bias   = -45.11s
RMSE   = 74.20s
```

### Traffic-supported prediction

```text
n      = 17,496
median = 3.55s
mean   = 13.07s
p90    = 30.35s
p95    = 73.75s
bias   = -9.25s
RMSE   = 34.16s
```

### Free-flow fallback prediction

```text
n      = 13,969
median = 1.39s
mean   = 24.94s
p90    = 111.64s
p95    = 129.84s
bias   = -24.93s
RMSE   = 55.17s
```

Interpretation:

- free-flow fallback is excellent when traffic is actually free-flow;
- fallback is poor when an unobserved queue exists;
- movement evidence improves predictions substantially;
- missing congestion evidence, rather than free-flow physics, is the dominant remaining problem.

Only 13 of 31,465 holdout observations, about 0.04%, were more than 0.5 s below the static free-flow lower bound. This strongly supports the static free-flow construction.

---

## 21. Holdout accuracy by actual delay

### True delay <= 2 s

```text
n      = 9,052
median = 0.53s
mean   = 0.77s
p90    = 1.49s
p95    = 1.73s
bias   = -0.49s
RMSE   = 3.24s
```

### True delay 2..15 s

```text
n      = 7,645
median = 3.39s
mean   = 5.01s
p90    = 7.29s
p95    = 9.52s
bias   = -2.36s
RMSE   = 12.45s
```

### True delay 15..60 s

```text
n      = 5,574
median = 5.16s
mean   = 16.30s
p90    = 51.74s
p95    = 56.38s
bias   = -13.23s
RMSE   = 26.82s
```

### True delay > 60 s

```text
n      = 9,194
median = 7.62s
mean   = 47.95s
p90    = 134.37s
p95    = 180.84s
bias   = -45.03s
RMSE   = 79.18s
```

This is the clearest current diagnosis.

The representation is already strong in free-flow and modest-delay conditions.

The major remaining fidelity problem is severe queue state.

---

## 22. FIFO cumulative-maximum limitation

The current prototype makes arrival curves FIFO-safe with:

```python
np.maximum.accumulate(arrival, axis=0)
```

This prevents a later departure from implausibly arriving before an earlier departure on the same movement.

However, it is intentionally crude.

It creates two possible failure modes:

1. **Underprediction:** a queue exists but there is no direct evidence in the relevant movement/time cell, so WorldPack falls back toward free-flow.
2. **Overprediction:** a large direct anchor can remain influential too long through cumulative maximum and smear a queue-release event.

The holdout's worst errors included both large negative errors and at least one roughly +399 s overprediction.

Queue geometry is expected to help with both:

- determining where a probe actually encounters the queue;
- determining when the queue actually releases.

---

## 23. Alias-aware v1.3 experiment

A controlled A/B experiment reconstructed temporally aliased training observations onto the first legal movement in their static path.

Importantly:

- aliased holdout observations stayed excluded;
- the holdout target set remained identical;
- only training evidence changed.

Result:

```text
alias train recovered   : 5,203
alias holdout excluded  : 1,296
alias >20m excluded     : 2
alias unresolved        : 0
alias invalid           : 0
```

Main comparison:

| Metric | v1.2 | v1.3 | Change |
|---|---:|---:|---:|
| median | 2.84 s | 2.82 s | -0.02 s |
| mean | 18.34 s | 18.23 s | -0.11 s |
| p90 | 60.80 s | 60.61 s | -0.19 s |
| p95 | 119.21 s | 119.06 s | -0.15 s |
| bias | -16.21 s | -15.94 s | +0.27 s |
| RMSE | 44.72 s | 44.46 s | -0.26 s |

Severe-delay comparison:

```text
v1.2 >60s delay mean : 47.95s
v1.3 >60s delay mean : 47.43s

v1.2 >60s bias       : -45.03s
v1.3 >60s bias       : -44.41s

v1.2 >60s RMSE       : 79.18s
v1.3 >60s RMSE       : 78.48s
```

Decision:

> Alias reconstruction is logically valid and useful for provenance/diagnostics, but it is not currently an important accuracy lever.

WorldPack v1.2 remains the principal baseline.

---

## 24. Next modeling candidate: lane-level queue geometry

The next proposed evidence capture records, every 5 seconds:

```text
lane_id
parent road
lane length
lane max speed
vehicle count
waiting vehicle count
queue tail longitudinal position
queue front longitudinal position
```

Waiting classification is intended to use approximately:

```text
v < 0.1 m/s
```

This threshold is a classification boundary only. It must not become a fake probe travel speed.

The intended physical model is:

```text
road entrance
|
|    free-moving usable lane
|-------------------------------> queue tail
                                   |
                                   | wait / discharge
                                   v
                              queue front
                                   |
                                   v
                           intended movement
```

For the known 494 m case, a probe should be able to traverse the first approximately 493 m normally before encountering a stopped queue near the road end.

This is fundamentally different from v0's whole-road stop interpretation.

### Important experimental rule

Because repeat GPU runs can diverge, queue geometry and validation trajectories should be captured from the **same MOSS realization**.

The next queue-capture run should therefore record both:

- person movement observations;
- lane-level queue state.

The resulting data can then be split by person for a frozen 80/20 holdout.

---

## 25. Historical PORTAL data strategy

A major planning refinement is to introduce historical data sooner in small controlled slices.

The current synthetic 25k-person stress world is useful for representation research, but it is **not calibrated Portland traffic truth**.

Two questions must remain separate:

### Representation fidelity

> Can WorldPack reproduce what MOSS simulated?

### Traffic fidelity

> Can MOSS reproduce what Portland historically did?

The desired validation chain is:

```text
HISTORICAL PORTAL DATA
        |
        | external reality test
        v
MOSS
        |
        | representation test
        v
WORLDPACK
        |
        v
COMMUTE HELP
```

---

## 26. Recommended historical “golden slice”

Instead of immediately building 24/5 worlds, begin with one small real-world slice.

Suggested characteristics:

- one important Portland-area corridor;
- one real weekday or weekday class;
- approximately 3 to 4 hours surrounding a peak period;
- multiple sequential detectors;
- 15-minute observations preferred over hourly aggregates;
- speed and occupancy included if available;
- some detectors/times deliberately withheld from calibration.

Example concept:

```text
05:30 -------------------------- 09:30
             morning peak
```

Spatial cross-validation concept:

```text
A ---- B ---- C ---- D ---- E ---- F

calibrate: A, C, E
validate : B, D, F
```

Temporal validation is also useful:

```text
calibrate:
06:00 through 08:00

validate:
08:15 through 09:00
```

Eventually, stronger validation should include unseen days.

---

## 27. Why 15-minute data matters

Hourly VPH averages can hide peak formation.

For example, an hourly average of 1,500 VPH might actually contain:

```text
07:00-07:15    900
07:15-07:30  1,300
07:30-07:45  1,900
07:45-08:00  1,900
```

Those profiles can create very different queue behavior despite the same hourly average.

Preferred hierarchy:

```text
raw detector observations
        |
        v
15-minute historical profiles
        |
        v
weekday-specific profiles
        |
        v
month / season
        |
        v
year-over-year adjustments
        |
        v
calibrated MOSS demand
```

Where finer raw resolution exists and is trustworthy, it should not be unnecessarily averaged away.

---

## 28. VPH is a calibration constraint, not a complete OD model

A detector's VPH tells how many vehicles passed that observation point.

It does not directly reveal:

- trip origin;
- trip destination;
- upstream entry;
- downstream exit;
- route choice;
- turn proportions;
- rerouting behavior.

Therefore calibration should use historical measurements as network constraints rather than simply injecting detector VPH as if it were the full demand model.

Conceptually:

```text
OD / route / departure demand
        |
        v
MOSS
        |
        v
simulated detector values
        |
        +------ compare ------ historical detector values
                         |
                         v
                 calibration update
```

The stronger test is not whether MOSS matches only the observations it was fitted against.

The stronger test is whether MOSS also reproduces withheld locations, withheld times, and eventually withheld days.

---

## 29. Pattern validation should go beyond total VPH

Historical cross-reference should examine traffic shape and propagation, not just aggregate counts.

Useful questions include:

- Does demand rise at the historically observed time?
- Does flow saturate at the same bottleneck?
- Does congestion appear in the same corridor region?
- Does the slowdown propagate upstream?
- Does the simulated peak occur at approximately the right time?
- Does the queue dissipate in the right order?
- Does throughput recover at approximately the right rate?

If historical speed and occupancy are available, queue validation becomes much stronger.

Volume alone still supports:

- flow-pattern validation;
- peak timing;
- bottleneck throughput;
- some propagation signatures.

It should not be treated as direct proof of physical queue length.

---

## 30. Long-term Traffic WorldAtlas target

The long-term system remains:

```text
calibrated historical inputs
        |
        v
MOSS simulation / calibration
        |
        v
Traffic WorldAtlas
        |
        +-- Monday
        +-- Tuesday
        +-- Wednesday
        +-- Thursday
        +-- Friday
        |
        +-- time of day
        +-- month
        +-- season
        +-- year-over-year trend
        +-- holidays / school / events
        +-- weather overrides
        +-- construction / closure scenarios
        +-- exact-date resolver
        |
        v
Commute Help
```

Future dates must be labeled as modeled forecasts rather than historical truth.

Baseline and scenario worlds must remain distinguishable.

The first expansion target is 24/5, with 24/7 available later if justified.

A naive dense 5-second matrix for 18,425 roads across 24/5 becomes enormous, so future WorldAtlas representation must use compact movement fields, temporal compression, and sensible day/world partitioning rather than blindly extending the v0 matrix.

---

## 31. Current architecture rules

The following rules have emerged from the research and should be treated as design constraints unless new evidence overturns them.

### Rule 1: CityProto topology is authoritative

Observed trajectories may be undersampled. They may not invent legal movements.

### Rule 2: road-average speed is not the production traffic primitive

It can remain a diagnostic.

### Rule 3: movement-specific arrival/traversal consequence is the primary v1 primitive

Approximately:

```text
(from road, to road, entry time) -> arrival
```

### Rule 4: empirical trajectories are anchors, not the whole field

Unobserved legal movements still need valid static fallback behavior.

### Rule 5: zero speed means zero progress

The old v0 0.1 m/s technical floor should not become a physical traffic assumption.

### Rule 6: FIFO behavior matters

The router must not produce a later departure that overtakes an earlier departure through an impossible time-dependent edge model.

### Rule 7: synthetic demand must remain explicitly labeled

Current 25k-person test worlds are architecture/fidelity stress tests, not Portland truth.

### Rule 8: expensive evidence should be immutable

Raw observations, topology outputs, queue evidence, benchmark logs, and known-pathology examples should not be silently overwritten.

### Rule 9: same-run evidence should be used for direct stochastic comparisons

GPU nondeterminism prevents assuming two nominally identical simulations are exactly the same world.

### Rule 10: every model improvement must beat a frozen validation baseline

Cleverness alone is not sufficient.

---

## 32. Current official benchmark

WorldPack v1.2 remains the principal benchmark before the next queue-state candidate.

```text
world id:
wp1-871b61e142900dcdb3f52aaf
```

Primary holdout baseline:

```text
linear median absolute error :   2.84s
linear mean absolute error   :  18.34s
linear p90                   :  60.80s
linear p95                   : 119.21s
linear bias                  : -16.21s
linear RMSE                  :  44.72s
```

Severe-delay baseline:

```text
true delay >60s

median absolute error :   7.62s
mean absolute error   :  47.95s
p90                   : 134.37s
p95                   : 180.84s
bias                  : -45.03s
RMSE                  :  79.18s
```

The next candidate should attack the tail and bias. Shaving hundredths of a second from the median is not the goal.

---

## 33. Current PC artifact inventory of interest

Known important files and directories include:

```text
~/moss-gpu-test/
```

Important subtrees:

```text
portland-pilot/
artifacts/
artifacts/worldpacks/
artifacts/patches/
```

Known key WorldPacks:

```text
artifacts/worldpacks/portland-pilot-25k-4200-speedfix-v1
artifacts/worldpacks/portland-pilot-25k-900-movement-v1.1
artifacts/worldpacks/portland-pilot-25k-900-movement-v1.2
```

Known important evidence/artifacts:

```text
artifacts/worldpack-v1.1-movement-900-observations.csv
artifacts/worldpack-v1-static-movements.npz
artifacts/worldpack-v1-static-movements.json
artifacts/worldpack-v1-static-movements.log
artifacts/worldpack-v1.2-compile.log
artifacts/worldpack-v1.2-holdout.log
artifacts/worldpack-v1.3-alias-holdout.log
```

Known scripts created or modified during current research include variants of:

```text
portland-pilot/compile-worldpack-v1-movement-900.py
portland-pilot/compile-worldpack-v1.1-movement-900.py
portland-pilot/compile-worldpack-v1-static-movements.py
portland-pilot/compile-worldpack-v1.2-from-observations.py
portland-pilot/validate-worldpack-v1.2-holdout.py
portland-pilot/validate-worldpack-v1.3-alias-holdout.py
```

The accompanying checkpoint script archives the entire `~/moss-gpu-test` tree, so this inventory is informational rather than exhaustive.

---

## 34. Cleanup strategy

The project should clean up **along the way**, not pause now for a large refactor.

Recommended rhythm:

```text
experiment
    |
    v
validate
    |
    v
decision
    |
    v
archive / checkpoint / light cleanup
    |
    v
next experiment
```

Do not prematurely build a large generic framework while the queue representation is still being tested.

However, preserve clear conceptual separation between:

```text
MOSS evidence capture
WorldPack compilation
validation / scoring
historical calibration
runtime routing
```

Old experiments should be archived, not deleted, when they document a rejected hypothesis or regression.

---

## 35. Immediate next plan

The historical Golden I-5 pilot has now advanced beyond slice selection and into simulator-fidelity debugging. The immediate plan is:

1. Preserve the WorldPack v1.2 benchmark, Golden Experiment Bundle v1.1, controlled upstream4 demand artifacts, SUMO cross-sim controls, and route-horizon causal results as immutable evidence.
2. Run one final narrow ablation that leaves A lane plans untouched while propagating direct-compatible lane knowledge only through MERGE and CONNECTOR. This tests whether the first-fork fix can be retained without creating the new A-road capacity restriction.
3. Stop broad static lane-plan tuning after that ablation. Static `next_road_lane_plans` are a diagnostic instrument, not the production solution.
4. Design a route-specific multi-road strategic lane-lookahead fix in MOSS or in a controlled Commute Help MOSS fork. The planner should prepare vehicles for a downstream movement before the final discriminating road, while reserving forced braking for genuinely late mandatory maneuvers.
5. Re-run the same controlled Golden I-5 demand in corrected MOSS and SUMO. Require the early fork artifact to remain absent and compare downstream congestion progression, especially DE7A_OUT.
6. Re-run the Golden I-5 historical scoring chain against PORTAL counts/speeds with the corrected simulator behavior. Do not calibrate route splits to compensate for a known lane-planning artifact.
7. Continue lane-level queue-geometry capture from the same MOSS realization as its validation trajectories. WorldPack v1.2 remains the representation baseline until a queue-aware candidate beats it on the frozen holdout.
8. Keep historical traffic fidelity and WorldPack representation fidelity as separate gates.
9. Only after both gates are healthy expand corridor duration, full day, Mon-Fri, month/season, and year-over-year worlds.

---

## 36. What has been proven

### Proven

- P4000 can run the pilot MOSS workload at useful speed.
- PC-to-Mac WorldPack architecture works.
- Mac can route a passive probe through a transferred WorldPack.
- v0 road-average-speed representation has a real physical fidelity defect.
- MOSS exposes per-person longitudinal lane position.
- movement-specific traffic consequences are representable.
- the 60-second junction-gap cutoff was censoring valid traffic.
- trip identity using schedule/trip index is a better movement-continuity rule.
- CityProto exposes complete legal road-to-road movement topology.
- all 974 apparently illegal sampled movements were explainable as temporal aliasing.
- v1.2 contains the full legal movement universe.
- deterministic person-level holdout validation works.
- WorldPack is already highly accurate in free-flow/mild-delay conditions.
- severe queueing is the dominant remaining representation problem.
- alias reconstruction alone produces only a small accuracy improvement.
- the Golden I-5 historical pilot can be generated and run from real 2024-02-01 PORTAL demand evidence.
- spawning A traffic directly on the historical detector road creates an artificial MOSS birth queue; moving A generation upstream to the four-lane predecessor removes that boundary artifact and restores approximately historical A throughput.
- SUMO and MOSS preserve the same physical first-fork lane topology for the audited Golden I-5 network.
- under the same direct100 demand, SUMO with route-compatible `allowed` insertion carries roughly the full A+B demand through the first fork at near free flow, while original MOSS collapses at the fork much earlier.
- MOSS v1.4.0 strategic lane planning uses only the immediately next route road when calculating its acceptable lane range.
- the first route-discriminating road in the Golden I-5 direct100 path is the 74.48 m fork; A, MERGE, and CONNECTOR expose no route-invalid lanes to MOSS before that point.
- an explicit `next_road_lane_plans` control that encodes the original topology is tracked-trajectory byte-equivalent to the original MOSS run.
- propagating direct-compatible lane ranges upstream drives observed wrong-side fork entries from 20.18% to 0% through 180 s and from 23.24% to 0% through 300 s.
- the same propagation restores fork speed from 11.44/9.51/7.35 m/s to 22.95/23.13/23.54 m/s across the 120-300 s windows.
- therefore the premature Golden I-5 first-fork collapse is causally attributable to MOSS's short strategic route-aware lane horizon in this experiment.
- the full propagated diagnostic is not a production solution because forcing all A traffic toward one lane creates a new upstream A capacity restriction.

### Not yet proven

- lane-level queue geometry materially improves WorldPack holdout accuracy;
- a production multi-road MOSS lane-lookahead fix reproduces the diagnostic improvement without creating upstream capacity artifacts;
- corrected MOSS fully matches Golden I-5 historical PORTAL traffic across the scoring window;
- the current historical demand/OD construction generalizes beyond this corridor and date;
- PORTAL-based demand calibration is accurate on withheld detector locations/times/days;
- the historical direct/alternate split is known from eligible detector evidence;
- 24-hour or 24/5 WorldPack storage/compression is final;
- 100k+ MOSS-person scaling;
- full WorldAtlas production performance;
- exact-date seasonal/yearly prediction accuracy.

---

## 37. Decision log

### Accepted

- P4000 as simulation worker for the current pilot.
- WorldPack as simulator/runtime separation boundary.
- movement-aware traversal/arrival representation.
- CityProto `JunctionLaneGroup` as movement-topology authority.
- content-addressed WorldPack identity.
- deterministic person-level holdout testing.
- explicit synthetic/calibrated classification.
- small historical golden-slice strategy.
- incremental cleanup and checkpointing.
- Golden I-5 2024-02-01 as the first controlled historical reality test.
- upstream4 A generation boundary as the corrected ingestion boundary for the current historical pilot.
- exact B-person preservation when comparing A-boundary placement variants.
- SUMO as an independent microscopic-model control for isolating MOSS behavior from synthetic demand/route assumptions.
- source-of-truth topology audits before interpreting simulator differences.
- route-horizon propagation as a causal diagnostic for MOSS strategic lane positioning.
- a production fix should be route-specific and multi-road aware rather than a static global lane-plan hack.

### Rejected or demoted

- road-average speed as production routing cost.
- 60-second semantic junction timeout.
- treating observed 1-second road jumps as authoritative legal movements.
- immediately scaling v0 to 24/5.
- assuming same-seed GPU runs are bit-for-bit identical.
- alias reconstruction as a major accuracy solution.
- calibrating against all historical observations with no untouched validation set.
- treating historical detector zeros on the direct path as proof of zero traffic or a historical route split.
- static original synthetic A-lane membership as a physical predictor after moving the A generation boundary upstream.
- current launch lane as a reliable predictor of eventual A lane choice.
- additional first-fork route-share sensitivity sweeps as the primary cure for the early MOSS collapse.
- changing reference speed caps to match transient detector observations; the audited A/B/mainline reference is already 80.467 km/h = 22.352 m/s, matching the MOSS cap.
- the fully propagated A/MERGE/CONNECTOR diagnostic map as a production map.

---

## 38. Core research methodology

The project is now following a repeatable scientific-development loop.

### 1. Form a concrete hypothesis

Example:

> Whole-road average speed may be causing route convergence.

### 2. Find a minimal diagnostic

Example:

> Inspect raw vehicle positions and speeds on known pathological roads.

### 3. Test against direct simulator evidence

Example:

> One vehicle is stopped 1.06 m from the end of a 494 m road.

### 4. Change the representation, not just the symptom

Example:

> Move from road-average speed to movement-specific arrival.

### 5. Establish an immutable baseline

Example:

> v1.2 person-level holdout metrics.

### 6. Add one candidate mechanism

Example:

> alias reconstruction or queue geometry.

### 7. Re-run the same validation

### 8. Keep the change only if it materially improves the metric that motivated it

This methodology should continue into PORTAL calibration.

---

## 39. Final checkpoint state

At this checkpoint, the project has crossed an important boundary: historical reality testing is no longer a future planning item. It is now producing concrete simulator-fidelity evidence.

The current state is:

- the PC/MOSS/WorldPack/Mac architecture still stands;
- WorldPack v1.2 remains the official representation benchmark;
- queue-state geometry remains the primary WorldPack representation candidate for severe-delay accuracy;
- the Golden I-5 historical pilot is active and uses real PORTAL observations from 2024-02-01;
- the original historical A source boundary was shown to create an artificial birth queue and has been corrected upstream;
- historical direct-path zero observations were rejected as insufficient route-split evidence rather than interpreted literally;
- random and static-lane first-fork route-split tuning did not solve the early collapse;
- an independent SUMO control demonstrated that the direct100 demand itself can pass the first fork near free flow;
- a runtime lane-position audit showed MOSS sends a substantial share of direct-bound A traffic toward alternate-only fork lanes while SUMO does not;
- source inspection and a static route-horizon audit showed why: MOSS only makes strategic lane-range decisions against the immediately next route road;
- an explicit equivalent lane-plan control reproduced the original trajectory exactly;
- propagating direct-compatible lane knowledge upstream eliminated wrong-side fork entries and restored near-free-flow fork behavior;
- that diagnostic also created an upstream A capacity restriction, proving that static road-level plans are not the production fix;
- after removing the artificial first-fork failure, MOSS downstream DE7A_OUT degradation became much closer to the independent SUMO control.

The next phase should therefore be treated as **simulator strategic-lane correction plus continued evidence expansion**, not as more demand tuning around the known artifact.

The revised growth path is:

```text
WorldPack v1.2 frozen representation baseline
        |
        +-----------------------------+
        |                             |
        v                             v
same-run lane queue evidence      Golden I-5 historical pilot
        |                             |
        v                             v
queue-aware WorldPack candidate   fix MOSS route-aware lane horizon
        |                             |
        +--------------+--------------+
                       |
                       v
          corrected historical MOSS world
                       |
                       v
            withheld PORTAL validation
                       |
                       v
            corridor / peak-period world
                       |
                       v
                    full day
                       |
                       v
                    Mon-Fri
                       |
                       v
                month / season
                       |
                       v
               year-over-year
                       |
                       v
          exact-date Traffic WorldAtlas
```

The guiding rule remains:

> Scale only what has already survived a measurable test.

---

## 40. Golden I-5 historical pilot now active

The project selected and froze a real historical Golden I-5 experiment rather than waiting until WorldPack work was complete.

### Frozen experiment bundle

Known experiment identity:

```text
Golden Experiment Bundle v1.1
historical date: 2024-02-01 (Thursday)
context window : 13:30-18:30
score window   : 14:30-18:30
historical bin : 900 s / 15 min
warmup         : 60 min
```

The frozen bundle should not be mutated. New diagnostics and sensitivity runs should live beside it.

The generated historical demand contains:

```text
A source vehicles : 9,900
B source vehicles : 8,826
total             : 18,726
```

The two principal historical source observations in the first Golden Slice bucket were approximately:

```text
A       : 2,660 vph
B       : 2,268 vph
combined: 4,928 vph
```

Representative first-bucket speeds were approximately:

```text
A        : 86.26 km/h
B        : 95.22 km/h
DE7A     : 81.51 km/h
mainline : 90.16 km/h
```

The historical slowdown develops gradually over tens of minutes. This became an important comparison against synthetic MOSS behavior that was collapsing at the first fork within only a few simulated minutes.

---

## 41. Historical evidence quality and route-split caution

The first historical route investigation found direct-successor detector/station records whose Golden Slice buckets contained zero volume/flow and `NaN` speed.

Those observations were quality-policy rejected as **insufficient direct evidence**.

Decision:

> Historical zeros at the direct-path detector are not proof that no traffic used the path and are not a valid direct/alternate split estimate.

The historical route split therefore remains an unknown calibration dimension. It should be inferred only from eligible evidence or external ODOT/TPAU information, not from missing/invalid detector observations.

This prevented a tempting but incorrect calibration shortcut.

---

## 42. Source-boundary artifact and upstream4 correction

The first historical MOSS runs spawned A traffic directly on the historical detector road. This created a visible artificial queue in the generation zone.

A kinematics audit established:

- persons activate with zero current speed;
- A/B historical road speed cap is approximately 22.35 m/s;
- the historical A/B/mainline `reference_speed_kph` is 80.467 km/h = 22.352 m/s;
- therefore the speed cap itself was not the mismatch;
- the problem was insertion directly onto a high-flow observed road from rest.

The A generation boundary was moved upstream to the farther four-lane predecessor:

```text
MOSS road : 200026430
SUMO edge : 5509112
```

The controlled route prefix became:

```text
5509112 -> 42230757 -> 5511109
```

A RouteConverter RNG confound initially changed B lane assignments while rebuilding the upstream4 artifact. That comparison was rejected. A controlled artifact was then built with:

- upstream4 A generation;
- exact baseline B Persons preserved byte-for-byte.

The controlled upstream4 world reproduced the boundary improvement.

Result:

> Moving A generation upstream removes the artificial birth queue and feeds the historical A road at roughly the historical 2,660 vph scale.

This is now the accepted historical ingestion boundary for the Golden I-5 pilot.

---

## 43. First-fork route sensitivity did not solve the collapse

After correcting the source boundary, the first-fork area still deteriorated early in MOSS.

Several direct/alternate route sensitivities were tested.

Random route shares included:

```text
100/0 direct/alternate
80/20
60/40
```

The direct100 case was actually the strongest of those simple variants at 300 s. More alternate routing did not produce a monotonic cure.

A later controlled A-subset envelope tested approximately:

```text
0% / 25% / 50% / 75% / 100%
of a selected A subset routed alternate
```

Equivalent overall alternate shares were about:

```text
0.00%
6.68%
13.37%
20.02%
26.68%
```

The 100%-subset-alternate case relieved the first fork most strongly early on, but congestion migrated downstream and intermediate variants were non-monotonic.

Two lane-membership hypotheses were also rejected:

1. original synthetic A source-lane membership became weakly related to runtime physical A lane after moving generation upstream;
2. current upstream4 launch lane predicted eventual A exit lane only about 55.71%, barely above the 52.86% majority baseline.

Decision:

> Stop using static source-lane membership as a routing rule and stop broad route-share tuning as the primary explanation for the early first-fork collapse.

---

## 44. SUMO-vs-MOSS Golden I-5 physics control

SUMO was installed and used as an independent microscopic-model control on the same Golden I-5 network and controlled demand.

The historical route XML contains 18,726 vehicles with inline routes and departure times, but no explicit `departLane`, `departPos`, or `departSpeed` attributes.

Two SUMO insertion controls were built:

```text
SUMO-FIRST
  departLane = first
  departPos  = 5
  departSpeed= 0

SUMO-ALLOWED
  departLane = allowed
  departPos  = 5
  departSpeed= 0
```

The SUMO `first` policy throttled both A and B to about 20 actual departures/minute each despite roughly 44 A and 37-38 B scheduled per minute. This independently demonstrated that departure-lane policy can create a severe artificial capacity boundary.

The SUMO `allowed` policy admitted essentially the full scheduled demand:

```text
120-180 s actual A/B: 44 / 38
180-240 s actual A/B: 45 / 38
240-300 s actual A/B: 44 / 37
```

Most importantly, SUMO-ALLOWED carried roughly 4,900 vph through MERGE -> FORK -> DIRECT -> DE7A_IN at essentially free-flow speed while original MOSS was already collapsing at the fork.

Representative comparison for 120-180 s:

| Metric | MOSS direct100 | SUMO allowed |
|---|---:|---:|
| A out | 2,340 vph | 2,640 vph |
| B out | 2,160 vph | 2,280 vph |
| MERGE speed | 20.74 m/s | 24.44 m/s |
| FORK speed | 11.44 m/s | 24.59 m/s |
| DIRECT speed | 19.72 m/s | 24.59 m/s |
| DE7A_IN speed | 23.45 m/s | 24.59 m/s |

SUMO did eventually develop downstream congestion at DE7A_OUT:

```text
120-180 : 24.24 m/s
180-240 : 21.40 m/s
240-300 : 15.17 m/s
t=300   : 10.05 m/s
```

This was a crucial result:

> The direct100 route/demand is not inherently incapable of passing the first fork. SUMO can carry it. The early MOSS fork collapse therefore requires a simulator-behavior explanation rather than another route-share assumption.

---

## 45. Exact first-fork topology and lane-position discrepancy

A SUMO source-of-truth lane audit confirmed the fork geometry:

```text
MERGE        : 4 lanes
EXPANSION    : 5 lanes
FORK         : 5 lanes
DIRECT       : fork lanes 2,3,4 -> 3 direct lanes
ALTERNATE    : fork lanes 0,1   -> alternate
```

The corresponding MOSS topology preserves the same structure.

Therefore the converter did not invent the fork split.

A direct runtime lane-position comparison then found the behavioral discrepancy.

### MOSS merge entry through 180 s

```text
A: lane0 32, lane1 31
B: lane2 42, lane3 44
```

### SUMO merge entry through 180 s

```text
A: lane1 70
B: lane2 88
```

For MOSS vehicles that entered merge lane 0 and completed the merge traversal:

```text
entered lane0 : 25
escaped       :  3  (12%)
remained      : 22  (88%)
```

At first fork entry:

```text
MOSS: 23 / 114 = 20.18% on alternate-only lanes 0/1
SUMO:  0 / 129 =  0.00% on alternate-only lanes 0/1
```

All 23 MOSS wrong-side arrivals were A traffic in that sample.

This established that the first-fork difference is a route-aware lane-positioning problem, not a physical topology mismatch.

---

## 46. MOSS strategic lane-horizon root cause

The installed simulator is:

```text
python-moss 1.4.0
```

The controlled persons use:

```text
lane_change_length = 10.0 m
```

Source inspection showed that this protobuf field is not the main route-lookahead knob. Required lane-change activation is based on a speed-derived length:

```text
lc_length = max(vehicle_length, vehicle_speed * 3)
```

More importantly, `UpdateLaneRange()` determines the target lane range using only:

```text
route[route_index + 1]
```

That means MOSS strategically evaluates only the **immediately next route road**.

The Golden I-5 route-horizon audit demonstrated:

```text
A          -> MERGE      : 0 route-invalid lanes
MERGE      -> CONNECTOR  : 0 route-invalid lanes
CONNECTOR  -> FORK       : 0 route-invalid lanes
FORK       -> DIRECT     : 2 route-invalid lanes
```

The first route-discriminating road is therefore the fork itself.

Fork length:

```text
74.48 m
```

For the two incompatible fork lanes, the calculated mandatory correction is approximately:

```text
offset 3: one-lane correction, trigger about 73.8 m from end
offset 4: two-lane correction, nominal requirement about 147.5 m
```

So the worst lane enters a 74.48 m fork already needing roughly 147.5 m of route-required lane correction.

Forced lane-change mode also applies normal braking and can escalate braking when rear-vehicle conflicts exist. This provides a plausible microscopic mechanism for the observed shockwave.

---

## 47. Route-horizon propagation causal experiment

To test the horizon mechanism without modifying the MOSS binary or physical map connectivity, two diagnostic map copies were created.

### Explicit control

`next_road_lane_plans` were added that exactly reproduced the topology-derived lane ranges already used by original MOSS.

### Propagated direct diagnostic

Direct-compatible lane ranges were propagated upstream through A, MERGE, and CONNECTOR.

Important invariants:

```text
physical successor graph : unchanged
persons                  : unchanged
MOSS binary               : unchanged
direct100 routes          : unchanged
departure schedule        : unchanged
```

The explicit control produced the exact same tracked trajectory digest as original:

```text
original : 50595d3849255c9a6b412c5e57bdeec614216f3fcc33e7be1cb274ec5193ab0f
explicit : 50595d3849255c9a6b412c5e57bdeec614216f3fcc33e7be1cb274ec5193ab0f
```

Therefore adding `next_road_lane_plans` by itself did not cause the result.

### Wrong-side fork entry

| Variant | Through 180 s | Wrong | Share | Through 300 s | Wrong | Share |
|---|---:|---:|---:|---:|---:|---:|
| original | 114 | 23 | 20.18% | 241 | 56 | 23.24% |
| explicit | 114 | 23 | 20.18% | 241 | 56 | 23.24% |
| propagated | 109 | 0 | **0.00%** | 221 | 0 | **0.00%** |

### Fork speed

| Window | Original | Explicit | Propagated |
|---|---:|---:|---:|
| 120-180 | 11.44 m/s | 11.44 m/s | **22.95 m/s** |
| 180-240 | 9.51 m/s | 9.51 m/s | **23.13 m/s** |
| 240-300 | 7.35 m/s | 7.35 m/s | **23.54 m/s** |

### Direct-hazard lane at road exit

| Variant | A hazard | MERGE hazard | CONNECTOR hazard* |
|---|---:|---:|---:|
| original | 52.86% | 23.17% | 16.82% |
| explicit | 52.86% | 23.17% | 16.82% |
| propagated | **0.00%** | **0.00%** | **0.00%** |

`*` The connector is only about 6.5 m long, so 1-second sampling can miss vehicles there. The connector percentage is diagnostic rather than a primary metric.

### Causal conclusion

> In this Golden I-5 experiment, the premature MOSS first-fork bottleneck is caused by the simulator's short strategic route-aware lane horizon. Moving the route-compatible lane requirement upstream removes wrong-side fork arrivals and restores near-free-flow fork behavior.

This conclusion is stronger than correlation because the explicit-control map is trajectory-equivalent to original while the propagated map changes only strategic lane-range metadata.

---

## 48. Why the propagated diagnostic is not the production fix

The propagated diagnostic fixed the fork but over-constrained A traffic.

A throughput degraded:

```text
window      original    propagated
120-180       2340          2040 vph
180-240       2340          1680 vph
240-300       2220          1260 vph
```

At t=300:

```text
A speed original   : 10.86 m/s
A speed propagated :  4.11 m/s
```

The diagnostic had globally declared only one A lane route-compatible. That successfully eliminated downstream hazard exits but created a new upstream lane-concentration problem.

Therefore:

> Static road-level `next_road_lane_plans` can prove the horizon mechanism, but they are too blunt to serve as the production solution for mixed traffic and mixed routes.

The production behavior needs to be **route-specific**, not globally attached to a road.

---

## 49. Corrected downstream behavior is now closer to SUMO

One of the most encouraging results appeared after the artificial fork collapse was removed.

DE7A_OUT speed comparison:

| Window | Corrected MOSS propagated | SUMO allowed |
|---|---:|---:|
| 120-180 | 23.58 m/s | 24.24 m/s |
| 180-240 | 21.68 m/s | 21.40 m/s |
| 240-300 | 16.11 m/s | 15.17 m/s |

The two independent microscopic models now tell a similar downstream story:

```text
MERGE -> FORK -> DIRECT -> DE7A_IN
                 remains healthy
                        |
                        v
                   DE7A_OUT
                 degrades later
```

This does not prove that either simulator is historical truth, but it materially increases confidence that MOSS remains useful once the specific strategic lane-planning artifact is corrected.

The current interpretation is therefore **not** "MOSS physics is unusable." It is:

> MOSS has a specific strategic route-aware lane-positioning limitation that must be corrected or compensated for before historical route calibration is trusted at complex forks.

---

## 50. Updated simulator-fidelity design target

The desired behavior separates early strategic positioning from late forced positioning.

Current effective behavior:

```text
all next-road lanes acceptable
        |
        v
wait until discriminating fork
        |
        v
mandatory lane correction
        |
        v
forced braking / shockwave
```

Desired behavior:

```text
look multiple route roads ahead
        |
        v
identify next meaningful lane constraint
        |
        v
strategically prefer compatible lanes early
        |
        v
normal safe lane changes
        |
        v
reserve forced braking for genuinely late correction
```

A production implementation should preserve route diversity. A direct-bound vehicle, alternate-bound vehicle, and exiting vehicle on the same road must be allowed to have different strategic lane targets.

The likely implementation options are:

1. a controlled MOSS source modification that computes a multi-road route-feasibility horizon;
2. a Commute Help preprocessing/controller layer that supplies route-specific strategic lane guidance without globally restricting the road;
3. a hybrid where static topology precomputes downstream feasible lane sets while per-person route state selects the relevant set at runtime.

The next narrow diagnostic is to leave A completely untouched and propagate only MERGE/CONNECTOR direct-compatible ranges. If that preserves the fork fix while restoring A ingestion, it will help estimate the minimum useful strategic horizon. It should be treated as the last static map-hack experiment before moving to the real route-specific solution.

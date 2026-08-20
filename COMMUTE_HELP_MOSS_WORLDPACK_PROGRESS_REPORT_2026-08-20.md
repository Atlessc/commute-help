# Commute Help / MOSS GPU / WorldPack Research Checkpoint

**Checkpoint date:** 2026-08-20  
**Primary PC workspace:** `~/moss-gpu-test` under WSL Ubuntu 22.04  
**Primary simulation hardware:** NVIDIA Quadro P4000  
**Current status:** The PC/MOSS/WorldPack/Mac architecture remains proven and WorldPack v1.2 remains the frozen representation baseline, but the active critical path has moved into regional historical calibration. The project now has a frozen 2024-2026 PORTAL corpus, a route-induced Portland/Vancouver MOSS network, a 60-date calibration/dev/blind split, exact detector-to-station aggregation semantics, 388/389 usable historical station mappings, and a structurally adequate 49,186-route path library. A first route/operator identifiability audit found only 278 provisional independent edge-touch dimensions, proving that per-route historical weights are massively underdetermined. More importantly, station-health audits found persistent fully sampled zero blocks that contradict active same-operator peers, so historical demand fitting is intentionally paused while a time-varying zero-block health mask is defined from calibration dates and validated on development dates. A separate unresolved blocker is that the converted MOSS map appears to contain roughly 11,366 more controlled junctions than the reduced SUMO network, so synthesized signal control must be resolved before serious historical MOSS fidelity claims.

---

## 1. Executive summary

The project has now crossed from architecture proof into **historical-world construction and validation**.

The original architectural question remains answered:

> Can the Windows/WSL/P4000 machine run the expensive traffic physics, compile a portable traffic world, and let Commute Help answer passive route queries without rerunning the microscopic simulator for every user?

**Yes.** The PC/MOSS/WorldPack/Mac separation still stands, and WorldPack v1.2 remains the frozen representation baseline.

The active critical path, however, is no longer basic WorldPack plumbing. It is now:

```text
PORTAL raw observations
        |
        v
observation health / provenance
        |
        v
historical station/operator targets
        |
        v
historical demand inference
        |
        v
MOSS historical replay
        |
        +------ traffic-fidelity gate ------ historical reality
        |
        v
validated MOSS world
        |
        +------ representation-fidelity gate ------ WorldPack
        |
        v
Commute Help baseline/scenario/probe workflow
```

Major progress since the 2026-08-17 checkpoint:

- A neutral MOSS source baseline is frozen at commit `f539e5afade4809c43cd041cd741e47db45080ce`, tagged `commute-help-p4000-baseline-2026-08-17`, with historical-calibration work isolated on `commute-help-historical-calibration-v1`.
- The regional historical profile contains **16,961,000 edge/day/15-minute rows across 675 weekdays from 2024-01-01 through 2026-07-31**.
- The H2.8d historical split is frozen at **20 calibration dates + 20 development dates + 20 target-blind holdout dates**. The blind set remains sealed.
- A 1,800-second regional stress network was reduced to the 62,620 route-used edges, producing a 53.7 MiB MOSS map with 62,620 roads, 221,673 lanes, and 39,415 junctions.
- A structural route library of **49,186 routes** has full support for the 270 exact historical app-edge operators currently used by the route-basis audit.
- The Feb. 1 station-level extraction now reproduces the production detector-to-station semantics before the compiler's station-to-edge median collapse: **389 stations × 8 buckets = 3,112 station-bucket rows** for 14:30-16:30.
- Historical station mapping is **388/389 usable**, with station `10520` explicitly unmapped.
- Those 388 stations collapse to **294 unique SUMO edge-touch operators** in the present route-incidence audit.
- Of the 49,186 candidate routes, 30,319 touch at least one observed operator and 18,867 touch none. The nonzero routes collapse to **3,899 observable route signatures**.
- The provisional route/operator incidence rank is **278**, leaving a **3,621-dimensional null space at the observable-class level** and **48,908 dimensions at the raw-route level**. This proves that 49,186 independent historical route weights must not be fitted.
- A lane-set audit found that the current virtual station cross-section artifact spans the complete mapped SUMO edge lane set for all 388 usable station mappings. This is useful for simulation-operator construction, but it should not by itself be read as proof that every source PORTAL detector physically covers every real roadway lane; earlier Golden I-5 detector work demonstrated that source detector lane subsets can matter.
- Repeated-station auditing exposed a more immediate data-quality problem: 65 SUMO operators have multiple historical stations, and 218/520 shared operator-buckets disagree by more than 20%; 96 contain a zero-vs-positive contradiction.
- Across the 2024-01-29 through 2024-02-04 source week, **33 fully observed stations are all-zero for all 478 represented buckets**, and **16 of those have an active same-operator peer carrying substantial traffic**. These zero channels have positive sample counts and no quality flags.
- The project therefore will not blacklist station IDs ad hoc. The next observation-quality layer will mask **pathological contiguous zero blocks in time**, preserving legitimate short low-traffic zero runs and healthy periods from otherwise flaky stations.
- Demand fitting is intentionally paused until this health mask is derived on calibration dates and checked on development dates.
- A separate simulator-control blocker remains: the reduced SUMO network contains 2,370 `tlLogic` programs while the converted MOSS map exposes 13,736 controlled junctions, suggesting roughly **11,366 additional synthesized MOSS signal controls**. Historical signal timing itself is not currently known, so this must be resolved before serious historical replay conclusions are accepted.

The project is therefore around the middle-to-late-middle of the first trustworthy end-to-end product. The architecture and data plumbing are mostly established; the largest unfinished work is historical-demand inference, MOSS-vs-reality validation, warm-start convergence, and then scenario/WorldPack productization.

The governing principle is now stricter than before:

> Do not tune simulator behavior, route weights, signal behavior, or WorldPack representation to make one diagnostic date look better. Every inferred quantity must have provenance, every candidate rule must be derived on calibration evidence, and every accepted change must improve held-out validation.

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

The immediate plan has changed materially since the 2026-08-17 Golden I-5 lane-horizon checkpoint. The current critical path is regional historical-calibration evidence quality, followed by demand inference and only then serious historical MOSS replay.

1. **Freeze the current Feb. 1 diagnostic artifacts.** Preserve the station-level slice, route/operator identifiability audit, lane/operator audit, repeated-station consistency audit, and week-long station-health diagnostic by content hash.
2. **Build a time-varying historical station-health mask.** Detect contiguous fully sampled zero blocks rather than permanently blacklisting station IDs. The rule must distinguish plausible short low-traffic zero periods from long contradictory zero runs.
3. **Derive health-rule thresholds only on the 20 frozen calibration dates.** Do not choose run-length or contradiction thresholds from Feb. 1 because Feb. 1 is diagnostic evidence, not a tuning set.
4. **Validate the frozen health rule on the 20 development dates.** Measure how much observation coverage is removed, how often repeated same-operator measurements become coherent, and whether the rule behaves similarly across dates.
5. **Keep the 20 target-blind holdout dates sealed.** They are not available for health-rule selection, regularization selection, warm-start selection, signal assumptions, or demand-model tuning.
6. **Reconstruct health-aware physical observation targets.** For each operator/time bucket, aggregate only healthy station observations; if all contributing stations are masked, the target is `UNKNOWN`, not zero.
7. **Recompute the route/operator identifiability matrix after the health policy is frozen.** The current rank-278 result is provisional because the trusted observation surface is not yet frozen.
8. **Audit source-lane semantics where necessary.** The virtual cross-section artifact spans full SUMO edge lane sets, but this does not automatically prove the source PORTAL station measures all physical lanes. Resolve any source-lane subset cases before treating a historical count as a full-road flow equation.
9. **Choose a constrained historical-demand formulation.** Do not estimate 49,186 free route weights. Use observable route classes or another reduced latent basis, nonnegativity, a structural route prior, and regularization chosen by development-date performance.
10. **Resolve the signal-control blocker before accepting MOSS traffic fidelity.** Determine why the converted MOSS map contains about 11,366 more controlled junctions than the reduced SUMO network and regenerate/reconfigure if the extra controls are converter-synthesized.
11. **Run warm-start convergence experiments.** Candidate historical starts should include 15:30, 15:00, and 14:30 while scoring the same 16:00+ state. Choose the shortest pre-roll whose scored state has stabilized against a longer pre-roll under a frozen convergence criterion.
12. **Run calibration-date MOSS-vs-history replay.** Score volume, speed, occupancy where trustworthy, peak timing, bottleneck throughput, and congestion propagation. Simulator parameters change only where residual structure demands it.
13. **Validate on development dates.** Reject changes that improve calibration dates but fail to generalize.
14. **Open the blind holdout only after the complete historical pipeline is frozen.** This is the first real no-cheating traffic-fidelity exam.
15. **Only after traffic fidelity is healthy, return to WorldPack representation fidelity and scenario productization.** WorldPack v1.2 remains the frozen representation benchmark in the meantime.

The old static `next_road_lane_plans` experiments remain valuable causal evidence about MOSS route-aware lane horizon, but they are not the current historical-calibration mechanism and should not be used to compensate for observation or demand errors.

---

## 36. What has been proven

### Proven

- P4000 can run the pilot and current route-induced regional MOSS workloads at useful speed.
- PC-to-Mac WorldPack architecture works.
- Mac can route a passive probe through a transferred WorldPack.
- v0 road-average-speed representation has a real physical fidelity defect.
- movement-aware WorldPack v1.2 remains a valid frozen representation benchmark.
- deterministic person-level holdout validation works for WorldPack representation fidelity.
- CityProto topology exposes the complete legal movement universe and remains authoritative over undersampled trajectory jumps.
- the 60-second junction-gap cutoff censored legitimate movement observations and trip-identity continuity is superior.
- severe queueing remains the dominant WorldPack v1.2 representation error tail.
- the Golden I-5 first-fork experiment causally demonstrated a short MOSS strategic route-aware lane horizon; a static propagated lane-plan hack fixes that fork but over-constrains upstream traffic and is not a production solution.
- the regional historical profile contains 16,961,000 rows covering 675 weekdays from 2024-01-01 through 2026-07-31.
- the historical evaluation split is frozen at 20 calibration, 20 development, and 20 target-blind holdout dates.
- detector-to-station aggregation semantics have been recovered directly from the production compiler rather than guessed.
- station-to-edge historical aggregation uses the median across station accumulators, including the quality flag `multiple_stations_median_not_flow_sum` when multiple stations contribute.
- the Feb. 1 14:30-16:30 station-level extraction yields exactly 3,112 rows = 389 stations × 8 buckets, with all 389 stations represented in every bucket.
- 388/389 historical stations have usable direction-compatible SUMO cross-section mappings; station `10520` is explicitly unmapped.
- the current route library contains 49,186 routes; every one of the 270 exact scorable app-edge operators in the earlier route-support gate has structural route support.
- in the 294-edge route-incidence audit, 30,319 routes touch at least one observed operator, 18,867 touch none, and none of the touching routes traverses the same observed operator more than once.
- the 30,319 observed routes collapse to 3,899 nonzero observable signatures.
- the provisional numerical real rank of the 3,899-class × 294-operator incidence system is 278.
- therefore raw-route demand is massively non-identifiable: 49,186 - 278 = 48,908 route-level null dimensions; even after collapsing to nonzero observable classes, 3,899 - 278 = 3,621 class-level null dimensions remain.
- the current virtual cross-section artifact spans all mapped SUMO lanes for all 388 usable station mappings, with no lane-ID mismatch in that artifact.
- 65 SUMO operators have multiple historical stations in the Feb. 1 slice.
- repeated-station measurements are not always interchangeable: 218/520 shared operator-buckets differ by more than 20%, and 96/520 contain zero-vs-positive contradictions.
- 295/3,104 mapped Feb. 1 station-buckets have volume zero while still having positive sample counts; none of those zero rows carries a station-accumulator quality flag.
- over the 2024-01-29 through 2024-02-04 source week, 33 stations are fully observed and all-zero for all 478 represented buckets.
- 16 of those all-zero stations share a modeled operator with one or more active peers that carry substantial weekly traffic. These are strong persistent-zero contradictions rather than ordinary missing-data gaps.
- the persistent-zero stations and active peers share the same high mapping-confidence / virtual-station provenance pattern; there is no simple provenance flag that cleanly separates dead-looking channels from active ones.
- therefore a historical value of zero cannot automatically be treated as physical zero flow merely because it has complete samples and no quality flag.
- a time-varying zero-block mask is preferable to a permanent station blacklist because it can preserve healthy periods from partially failing stations.
- the reduced SUMO signal program set is internally preserved by the current pruning path: 2,370 shared signal programs match exactly after the phase-clearing hash bug was corrected.
- the converted MOSS map nevertheless reports 13,736 controlled junctions, far more than the 2,370 reduced SUMO `tlLogic` controls, creating an unresolved signal-synthesis blocker.

### Not yet proven

- the final zero-block health rule, run-length threshold, peer-contradiction threshold, or other health-classification boundary;
- that the health rule generalizes from calibration dates to development dates;
- which all-zero stations without an active same-operator peer are instrument failures versus legitimate zero-flow observations;
- that every historical station count can be interpreted as full-road flow rather than a source detector lane subset;
- the final trusted physical operator count after health masking and source-lane semantic checks;
- the final route/operator rank after the trusted observation surface is frozen;
- a unique historical OD or route-demand solution;
- the regularization strength or latent route basis for inverse demand;
- the within-15-minute departure-time distribution; 15-minute totals do not identify sub-bucket timing;
- the minimum historical pre-roll/warm-start duration;
- historical signal timing or control policy for the modeled network;
- whether the approximately 11,366 extra MOSS controlled junctions are entirely converter-synthesized and how much historical distortion they cause;
- corrected MOSS historical fidelity across calibration and development dates;
- MOSS historical fidelity on the target-blind holdout;
- lane-level queue geometry materially improves WorldPack holdout accuracy beyond v1.2;
- 24-hour / 24x5 WorldPack storage and compression strategy;
- 100k+ person scaling on the P4000;
- full WorldAtlas exact-date seasonal/yearly prediction accuracy.

---

## 37. Decision log

### Accepted

- P4000 as the current simulation worker.
- WorldPack as the simulator/runtime separation boundary.
- WorldPack v1.2 as the frozen representation benchmark until a candidate beats it on the same holdout.
- movement-aware traversal/arrival representation rather than road-average-speed routing cost.
- CityProto legal topology as authoritative for WorldPack movement structure.
- content-addressed artifacts and immutable experiment evidence.
- separate **traffic-fidelity** and **representation-fidelity** gates.
- frozen historical calibration/development/blind date split.
- raw detector corpus and production compiler semantics as the authority for historical aggregation.
- station-level observation reconstruction before station-to-edge median collapse for demand-fit diagnostics.
- route library as a **structural path basis**, not historical truth.
- explicit observation/operator identifiability audits before fitting demand.
- reduced/regularized latent demand rather than 49,186 unconstrained per-route weights.
- explicit `UNKNOWN` when no healthy observation exists; never silently replace unavailable historical evidence with zero.
- time-varying zero-block health masking rather than permanent station blacklisting.
- health-rule derivation on calibration dates and validation on development dates.
- keeping the target-blind holdout sealed until health, demand, warm-start, signal, and scoring choices are frozen.
- source-lane semantics must be audited where detector lane subsets are possible.
- warm-start/pre-roll selection by historical-state convergence, not by arbitrary elapsed time.
- resolve signal-control provenance before treating historical MOSS replay as valid traffic physics.
- static `next_road_lane_plans` propagation remains diagnostic evidence only; any production strategic-lane fix must preserve route-specific behavior.

### Rejected or demoted

- road-average speed as a production WorldPack traffic primitive.
- 60-second semantic junction timeout.
- treating undersampled observed road jumps as authoritative legal movements.
- assuming nominally identical GPU runs are bit-for-bit identical.
- treating the proxy 49,186-route regional demand as calibrated Portland truth.
- fitting 49,186 independent route weights.
- claiming 389 independent station constraints without collapsing shared physical operators and auditing matrix rank.
- treating a fully sampled historical zero as automatically trustworthy physical zero flow.
- permanently blacklisting stations merely because Feb. 1 looks bad.
- imputing suspicious zero blocks with guessed traffic values for calibration; masked evidence should remain unavailable unless a separate justified imputation model is created.
- letting Feb. 1 choose health thresholds, regularization, pre-roll duration, signal assumptions, or other model hyperparameters.
- opening target-blind holdout dates during model selection.
- modifying demand, route splits, WorldPack costs, or MOSS lane rules merely to hide a known simulator/control artifact.
- using the synthetic SUMO signal program as historical agency timing truth.
- broad static lane-plan tuning as a production cure for MOSS strategic-lane behavior.

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

At the 2026-08-20 checkpoint, the project is no longer blocked on basic architecture, basic MOSS execution, basic historical file ingestion, or basic route coverage. It is blocked on **trustworthy historical observation semantics and simulator control provenance**, which is exactly where the project should be before attempting large-scale demand fitting.

Current state:

- the PC/MOSS/WorldPack/Mac architecture remains valid;
- WorldPack v1.2 remains the official representation baseline;
- the historical data plane is now regional rather than limited to the original Golden I-5 thought experiment;
- the 2024-2026 PORTAL historical edge corpus and frozen 60-date evaluation split are in place;
- route-induced MOSS conversion has produced a tractable regional network and 49,186-route structural path library;
- regional performance stress tests show the P4000 can run the dense route-induced world, though real-time factor declines with active population and long saturated runs remain expensive;
- production historical aggregation semantics have been recovered directly from source code and reproduced at station level;
- the Feb. 1 station slice is structurally complete and cross-section mapping is nearly complete;
- the route basis is structurally broad enough but mathematically far too underdetermined for per-route fitting;
- the current provisional operator system has 294 edge-touch dimensions but only rank 278;
- repeated station observations expose severe historical zero-channel pathology that the existing compiler quality flags do not identify;
- 16 week-long all-zero stations have active same-operator peers, making them strong instrument/data-health contradictions;
- the correct response is a time-varying zero-block health mask, not a hard-coded station blacklist and not guessed replacement traffic;
- historical demand inference remains intentionally paused until the health policy is frozen and validated;
- serious MOSS historical replay also remains gated by unresolved signal-control synthesis/provenance;
- the blind holdout remains untouched.

The revised critical path is:

```text
frozen PORTAL corpus
        |
        v
calibration-date zero-block health modeling
        |
        v
development-date health validation
        |
        v
trusted station/operator observation surface
        |
        v
recomputed identifiability / latent route basis
        |
        v
regularized nonnegative historical demand inference
        |
        +---------------------------+
        |                           |
        v                           v
signal-control provenance      warm-start convergence
        |                           |
        +-------------+-------------+
                      |
                      v
             historical MOSS replay
                      |
                      v
          calibration-date traffic fidelity
                      |
                      v
           development-date validation
                      |
                      v
              blind holdout exam
                      |
                      v
             validated MOSS baseline
                      |
                      v
       WorldPack representation-fidelity gate
                      |
                      v
          closure/scenario WorldPacks
                      |
                      v
              Commute Help product
```

A rough project-management estimate, not a scientific completion metric, is approximately:

```text
Architecture / simulator-runtime separation   ~90%
Historical data plumbing / provenance         ~85%
Historical mapping / structural operators     ~90%
Historical calibration model                  ~45%
Historical validation                         ~25%
Final scenario/webapp UX                      ~60%
Overall trustworthy end-to-end product        ~60%
```

The most important near-term milestone is no longer “make MOSS run.” It is:

> Produce the first historical replay whose observation mask, demand inference, signal assumptions, warm-start state, and scoring rules were all frozen before development/blind evaluation, then show that MOSS reproduces held-out Portland traffic within explicitly measured error.

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

---

## 51. Neutral historical-calibration source baseline

The current historical-calibration work is intentionally separated from earlier simulator-source experiments.

### MOSS source identity

```text
source tree:
~/moss-gpu-test/moss-p4000-src

neutral baseline commit:
f539e5afade4809c43cd041cd741e47db45080ce

tag:
commute-help-p4000-baseline-2026-08-17

working branch:
commute-help-historical-calibration-v1
```

The only known untracked source backup is:

```text
src/mem/mem.cu.pre-wsl-uvm
```

It must not be accidentally committed as a historical-calibration change.

Earlier V1/V2/V2.1/V2.2 source experiments are frozen/rejected as historical evidence rather than silently folded into the new baseline. The V2.2 experiment commit remains:

```text
7eca71dad4924caf04ef6ca5965875ccfaafc374
```

### Neutral isolated Python runtime

Historical diagnostics use the isolated runtime:

```text
~/moss-gpu-test/historical-calibration/runtime/moss-baseline-f539e5a
```

Known native module SHA-256:

```text
4256427199a4cb63d2a76f4d7dd62eb77f6296e2063bde04e6b6c013caeedf82
```

The system Python available in the current WSL environment is Python 3.10.12. The production historical compiler imports `datetime.UTC`, which is a Python 3.11+ API. Rather than mutate source code or rebuild the existing environment, diagnostics currently use a process-local compatibility shim:

```python
import datetime
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
```

The compiler then imports successfully as:

```text
compiler     : historical_calibration_v2_compiler
code version : phase-1.3-v1
```

This shim is runtime-only evidence plumbing. It is not a source-code fix.

---

## 52. Regional historical dataset and frozen evaluation split

### Historical edge profile

Primary profile:

```text
data/traffic/processed/calibration-v2/
historical-edge-profiles-v1/edge-day-15m.parquet
```

Current scale:

```text
rows      : 16,961,000
weekdays  : 675
date span : 2024-01-01 through 2026-07-31
resolution: 15 minutes
```

Important fields include:

```text
app_edge_id
local_date
weekday
bucket_start_minute
volume_count
flow_vph
speed_kph
occupancy_percent
reference_speed_kph
slowdown
station_count
detector_count
sample_count
quality_flags_json
evidence_level
calibration_status
```

Historical rows are evidence inputs, not proof that every field is independently trustworthy. Observation health remains part of the model.

### Frozen H2.8d date split

```text
calibration dates : 20
development dates : 20
blind holdout     : 20
```

The blind holdout remains sealed.

Role separation is strict:

```text
calibration
    -> derive model structure / thresholds / parameters

development
    -> choose among candidates / reject overfit

blind holdout
    -> final predeclared evaluation only
```

Feb. 1, 2024 remains a **diagnostic date** for the current structural investigation. It is not allowed to select the final zero-block threshold, demand regularization, signal policy, or warm-start duration.

---

## 53. Route-induced regional network and H3.3 stress result

The full Portland/Vancouver SUMO network is approximately 692.8 MiB:

```text
data/sumo/networks/pv-sumo-2026-08-08-v1/metro.net.xml
SHA-256:
17bb45772143fbe98241796d4205cf70f7c302130f9573dfaa25357417d06234
```

A full MapConverter run on the complete network was not practical on the current machine because it entered swap thrash at roughly 9 GiB RSS plus 7.69 GiB swap and accumulated about 14.7 million major page faults.

The solution was not to weaken the experiment. It was to construct the route-induced subnetwork from the 49,186 structural routes.

### Route-induced SUMO network

```text
used route edges : 62,620
network size     : ~159 MiB
SHA-256          : 7c49100f4b5431b8c940948158ffd8aca005caf5f954ea054dd1077a38b20088
```

All required route edges are present.

### Route-induced MOSS map

```text
roads     : 62,620
lanes     : 221,673
junctions : 39,415
size      : 53.7 MiB
SHA-256   : 131da35d95aeb6e6cf34daf3706d8d0a80a1035866922ccedade7e93144cf198
```

A flat namespace collision for SUMO ID `37572496` was repaired in the road ID mapping; the corrected road-fixed mapping is preserved separately.

### Structural persons / routes

```text
persons/routes : 49,186
SHA-256        : c33386730c0001deba7e1121014a73cf68ec1c80f2872b86458a739eb9735c93
```

These routes are **structural path candidates**, not historical demand truth.

### Regional performance stress

The neutral 1,800-second regional stress run completed in about 560 seconds wall time:

```text
real-time factor : 3.213x
RSS              : ~1.54 GiB
swap             : 0
GPU utilization  : ~92-95%
VRAM             : ~690-699 MiB
```

Population scaling showed wall time tracking active population almost perfectly:

| Sim horizon | Wall s | Approx. RTF | Running | Finished | On road | Waiting |
|---:|---:|---:|---:|---:|---:|---:|
| 300 | 72.4 | 4.14x | 8,071 | 62 | 7,461 | 3,854 |
| 600 | 80.9 | 3.71x | 16,222 | 272 | 14,937 | 5,979 |
| 900 | 89.1 | 3.37x | 23,972 | 725 | 22,220 | 12,192 |
| 1,200 | 96.8 | 3.10x | 31,369 | 1,403 | 28,910 | 15,596 |
| 1,500 | 103.9 | 2.89x | 38,676 | 2,281 | 34,691 | 19,705 |
| 1,800 | 111.6 | 2.69x | 45,686 | 3,468 | 42,258 | 28,373 |

The wall-time versus running-population correlation is approximately 0.9999 in this stress series.

### Important `start_step` semantic result

Tests with `start_step` = 0, 300, 900, and 1,800 all initialized with zero running/finished/on-road persons and the full person population in the waiting state.

Therefore:

> `start_step` changes the simulation clock but does not reconstruct a historical traffic state.

Historical replay must use actual pre-roll or an explicit reconstructed state.

---

## 54. Historical detector-to-station aggregation semantics

The historical compiler semantics are now known directly from source.

### Detector-to-station volume

For station \(s\), bucket \(t\), and detector set \(D_s\):

$$
V_{s,t} = \sum_{d \in D_s} V_{d,t}
$$

The same summation applies to station flow:

$$
F_{s,t} = \sum_{d \in D_s} F_{d,t}
$$

This is important because detector channels at one station are lane/channel components, not independent station-level traffic constraints.

### Detector-to-station speed

When positive detector volume exists, station speed is volume weighted:

$$
S_{s,t}
=\frac{\sum_{d \in D_s} V_{d,t} S_{d,t}}
       {\sum_{d \in D_s} V_{d,t}}
$$

When the positive-volume weighting condition is unavailable, the compiler uses the defined median fallback rather than inventing a weighted speed from zero-volume channels.

### Detector-to-station occupancy

For present detector occupancy observations:

$$
O_{s,t}
=\frac{1}{|D^{O}_{s,t}|}
 \sum_{d \in D^{O}_{s,t}} O_{d,t}
$$

### Station-to-app-edge collapse

After detector channels have been accumulated into physical station observations, the compiler groups stations by app edge and uses the median across stations:

$$
V_{e,t}
=\operatorname{median}_{s \in S_e} V_{s,t}
$$

Likewise:

$$
F_{e,t}
=\operatorname{median}_{s \in S_e} F_{s,t}
$$

$$
S_{e,t}
=\operatorname{median}_{s \in S_e^{S}} S_{s,t}
$$

$$
O_{e,t}
=\operatorname{median}_{s \in S_e^{O}} O_{s,t}
$$

The production quality flag for multiple stations explicitly says:

```text
multiple_stations_median_not_flow_sum
```

This distinction matters enormously for historical demand fitting. The demand-fit observation basis should begin from station-level physical measurements, not from the already-collapsed app-edge median.

### Feb. 1 station extraction

Target diagnostic slice:

```text
date   : 2024-02-01
window : 14:30-16:30, end-exclusive
buckets: 8
```

Result:

```text
station-bucket rows : 3,112
unique stations     : 389
unique app edges    : 271
stations complete   : 389 / 389
```

Artifact:

```text
~/moss-gpu-test/historical-calibration/warm-start-v1/
2024-02-01-1430-1630-station-15m.parquet
SHA-256:
85cd806c0c64a53b42d5def94e49b109dd7d5690c9a3d7ed87650ae52d3437f5
```

---

## 55. Historical mapping and physical observation surface

### Canonical app/SUMO relation

Known canonical edge map:

```text
data/sumo/networks/pv-sumo-2026-08-08-v1/edge-map.parquet
rows             : 233,474
accepted percent : 93.916%
SHA-256          : 2db8fce3b5101a357193c58039ff5311463eb858059b164c0e31a4bcd714ad97
```

### Station cross sections

```text
station cross-section rows : 426
historical stations        : 389
usable mapped              : 388
unmapped                   : 1
unmapped ID                : 10520
```

Historical station IDs are normalized by removing the `portal-station-` prefix before comparison.

### Feb. 1 observation mapping

```text
historical app edges          : 271
historical stations           : 389
station-ID mapping coverage   : 389/389 before direction/operator eligibility
usable station mapping        : 388/389
exact app-edge route operator : 270/271
historical row operator map   : 2,160/2,168
volume coverage               : ~99.52%
```

The targeted relation currently divides the 271 historical app edges into:

```text
single_edge                     : 126
ordered_chain_with_side_connections: 144
unmapped/problem                : 1
```

Only the `single_edge` class was previously considered immediately primary-flow eligible; the ordered-chain class still needs detector cross-section alignment rather than silent coercion.

### Lane-set caution

The current station cross-section artifact reports that all 388 usable virtual station operators span the complete lane set of the mapped SUMO edge, with zero lane-ID mismatches.

That result validates the **virtual simulation operator definition**. It does not automatically prove that every real PORTAL source station physically observes every roadway lane. Earlier Golden I-5 work on station `1020` demonstrated a measured-lane-subset case. Therefore source detector lane coverage remains a semantic property that must be verified where it affects the flow equation.

---

## 56. Route-basis support and identifiability

The 49,186-route library was audited as a structural path basis against the historical observation surface.

### Route support

For the earlier 270 exact scorable app-edge operators:

```text
operators with route support : 270 / 270
zero-support operators       : 0
minimum routes/operator      : 16
p10                          : ~428-440, depending operator audit version
median                       : ~1,081-1,120
p90                          : 1,717
maximum                      : 2,453
```

This proves **structural coverage**, not historical route-share truth.

### Current station/SUMO operator universe

After removing station `10520` and collapsing stations that map to the same SUMO edge:

```text
historical stations       : 389
usable stations           : 388
unique SUMO operators     : 294
shared operators          : 65
max stations per operator : 7
```

### Route visibility

```text
candidate routes                 : 49,186
routes touching >=1 operator     : 30,319
routes touching no operator      : 18,867
routes repeating observed op     : 0
```

The absence of repeated observed operators means the current incidence coefficient is binary in this route library, although the audit intentionally preserves traversal multiplicity so this assumption would fail loudly if a looping route appeared later.

Define route/operator incidence:

$$
A_{e,r}
= \sum_k \mathbf{1}[\text{route }r\text{ crosses operator }e\text{ at step }k]
$$

For the present library:

$$
A_{e,r} \in \{0,1\}
$$

because no route repeats an observed operator.

### Observable route classes

Routes with the same complete operator-incidence signature are observationally indistinguishable under this measurement system.

```text
unique signatures incl. zero : 3,900
unique nonzero signatures    : 3,899
zero-signature routes        : 18,867
singleton nonzero classes    : 1,831
median routes/class          : 2
largest class                : 648
```

### Numerical rank

For one time bucket, the idealized static flow equation is:

$$
y_t \approx A x_t
$$

where:

- \(y_t\) is the trusted historical operator-count vector;
- \(A\) is route/operator incidence;
- \(x_t\) is latent route-class demand.

The current provisional matrix has:

```text
operator dimensions : 294
numerical real rank : 278
```

The rank was computed from the Gram matrix because:

$$
\operatorname{rank}(A)
=\operatorname{rank}(A^\top A)
$$

up to the declared numerical tolerance.

### Null-space consequence

At raw-route level:

$$
49{,}186 - 278 = 48{,}908
$$

unidentified dimensions remain.

At nonzero observable-class level:

$$
3{,}899 - 278 = 3{,}621
$$

unidentified dimensions remain.

This is the mathematical reason that a 49,186-free-weight least-squares fit is invalid even if an optimizer returns a numerically small residual.

The rank-278 result is **provisional** until the observation-health mask and source-lane semantics are frozen.

Artifact:

```text
~/moss-gpu-test/historical-calibration/warm-start-v1/
route-station-identifiability-v1.json
SHA-256:
f07d3679f6321c696648712d5e44800b02c25953eb85445b5d6e248c1fbd18bf
```

---

## 57. Repeated-station consistency failure

The production compiler's station-to-edge median assumes that multiple station measurements associated with one app edge can sensibly be combined. The Feb. 1 station-level reconstruction allowed that assumption to be tested directly.

Corrected shared-operator structure:

```text
historical stations            : 389
usable mapped stations         : 388
unique SUMO operators          : 294
shared SUMO operators          : 65
shared operator-buckets        : 520
shared SUMO ops / >1 app edge  : 0
```

So the contradiction is not caused by unrelated app edges accidentally collapsing to the same SUMO operator.

### Relative disagreement metric

For repeated measurements \(v_1,\ldots,v_n\) of one operator/bucket, define:

$$
R
=\frac{\max_i v_i - \min_i v_i}
       {\operatorname{median}_i(v_i)}
$$

when the median is positive.

The robust spread companion is:

$$
\operatorname{MADR}
=\frac{\operatorname{median}_i
       |v_i-\operatorname{median}(v)|}
       {\operatorname{median}(v)}
$$

Current result:

```text
>20% disagreement groups : 218 / 520
zero-vs-nonzero groups    : 96 / 520
finite relative range p50 : 0.083
finite relative range p90 : 1.835
finite relative range p95 : 2.000
```

The spatial span between stations collapsed to the same modeled SUMO edge is also nontrivial:

```text
median span : 574.4 m
p90 span    : 1,147.6 m
max span    : 4,151.2 m
```

This means "same modeled edge" should be treated as a measurement-operator approximation, not an assertion that colocated instruments are literally identical.

### Zero-channel contradiction

Within the 3,104 mapped station-bucket rows:

```text
volume = 0 rows          : 295
zero rows sample_count>0 : 295
zero rows sample_count=0 : 0
zero rows quality flags  : none
```

A representative example on SUMO edge `118658780` is:

```text
station 10798 : positive, roughly 385-590 vehicles/bucket
station 10804 : 0 in every diagnostic bucket
station 1606  : positive, roughly 390-594 vehicles/bucket
```

All three map to the same app edge and modeled SUMO operator.

This cannot be treated as three equally trustworthy independent flow equations.

---

## 58. Week-long station-health evidence

To distinguish a short plausible zero interval from a persistent data-channel failure, the exact production detector-to-station accumulator was run over the entire frozen source week containing Feb. 1.

Source week:

```text
2024-01-29 through 2024-02-04
streams                    : 24
stream bytes               : 43.97 MiB
represented date/buckets   : 478
historical stations        : 389
mapped stations            : 388
complete-week stations     : 379
shared SUMO operators      : 65
```

### Persistent-zero result

```text
fully observed all-zero stations : 33
Feb1 zero-vs-positive groups      : 96
Feb1 zero-side station IDs        : 20
persistent zero + active peer     : 16
```

The 16 strongest contradictions are stations that satisfy all of the following for the source week:

- represented in all 478 available buckets;
- total station volume = 0;
- positive sample count in every represented period;
- no station-accumulator quality flag;
- one or more same-operator peers carry positive weekly traffic.

Examples include:

```text
10395, 10797, 10799, 10800, 10802, 10803, 10804, 10811,
10813, 10815, 10817, 10818, 10819, 10821, 10825, 1631
```

Several almost-dead stations are also visible, for example:

```text
10827 : 475 zero / 478, 3 positive, week volume 3
10828 : 475 zero / 478, 3 positive, week volume 6
10812 : 475 zero / 478, 3 positive, week volume 12
10829 : 474 zero / 478, 4 positive, week volume 14
```

These should not be converted automatically into hard blacklists. They motivate a **block-based health model**.

Artifact:

```text
~/moss-gpu-test/historical-calibration/warm-start-v1/
2024-01-29-week-station-health-v1.json
SHA-256:
68d3bb03c5d5b83341b2a2ef0ca23333813898ecd45b5f79c99c991663be5300
```

### Provenance audit

All 33 fully observed all-zero stations and all 16 persistent contradictions have:

```text
virtual_station_group_id : present
mapping confidence        : high
projection status         : review_threshold_policy_unset
```

Active peers exhibit the same general metadata pattern.

Therefore there is no simple provenance field that currently separates the suspicious zero channels from healthy channels.

---

## 59. Proposed time-varying zero-block health model

This section defines the **candidate mathematical form**, not final thresholds.

A historical zero should not be discarded merely because it is zero. Early-morning or very-low-volume conditions can legitimately contain zero counts.

The unit of health classification should instead be a **contiguous block**.

### Zero-run definition

For station \(s\), let \(V_{s,t}\) be station volume and \(N_{s,t}\) its sample count.

A fully sampled zero indicator is:

$$
Z_{s,t}
=\mathbf{1}[V_{s,t}=0 \land N_{s,t}>0]
$$

A zero block \(B=(s,t_0,t_1)\) is a maximal contiguous interval for which:

$$
Z_{s,t}=1
\quad \forall t\in[t_0,t_1]
$$

For 15-minute buckets, block duration is:

$$
L(B)
= 15\text{ min}\times |B|
$$

### Peer contradiction

Let \(P(s)\) be other healthy-candidate stations mapped to the same physical modeled operator as station \(s\).

A per-bucket contradiction indicator is:

$$
C_{s,t}
=\mathbf{1}
\left[
V_{s,t}=0
\land
\max_{p\in P(s)} V_{p,t}>0
\right]
$$

For an entire zero block:

$$
\rho(B)
=\frac{1}{|B|}
\sum_{t\in B} C_{s,t}
$$

This is the fraction of the zero block contradicted by positive same-operator evidence.

### Candidate health mask

Let \(\tau\) be a minimum suspicious zero-run duration and \(\kappa\) a minimum contradiction fraction.

A candidate mask can be expressed as:

$$
m_{s,t}
=\begin{cases}
0,& t\in B,\;L(B)\ge\tau,\;\rho(B)\ge\kappa\\
1,& \text{otherwise}
\end{cases}
$$

This is only the simplest candidate. Additional evidence may justify separate rules for all-zero blocks without peers, change-point behavior, before/after flow continuity, speed/occupancy contradictions, or detector-channel disagreement.

**Crucial rule:** \(\tau\), \(\kappa\), and any extra classifier thresholds must be chosen using calibration dates, then frozen and checked on development dates. Feb. 1 must not select them.

### Health-aware operator target

Let \(g(s)=e\) map station \(s\) to physical operator \(e\). Then:

$$
y_{e,t}
=\operatorname{median}
\left\{
V_{s,t}:g(s)=e,\;m_{s,t}=1
\right\}
$$

If no healthy station contributes:

$$
y_{e,t}=\mathrm{UNKNOWN}
$$

not zero.

This is important because an unavailable constraint must not exert artificial pressure on the inverse-demand solver.

---

## 60. Historical-demand inference mathematics

After the health mask and source measurement semantics are frozen, historical demand can be treated as a constrained inverse problem.

### Static bucket equation

For one 15-minute bucket:

$$
y_t = A x_t + \epsilon_t
$$

where:

- \(y_t\in\mathbb{R}^{m}\): trusted historical operator counts;
- \(A\in\mathbb{R}^{m\times n}\): route-class/operator incidence;
- \(x_t\in\mathbb{R}_{\ge0}^{n}\): latent nonnegative route-class demand;
- \(\epsilon_t\): measurement/model mismatch.

The present rank audit proves \(n\gg\operatorname{rank}(A)\), so the inverse is not unique.

### Weighted observation loss

Unavailable or lower-confidence observations should not be treated as equally strong constraints.

With diagonal weight matrix \(W_t\):

$$
\mathcal{L}_{obs}(x_t)
=\|W_t(Ax_t-y_t)\|_2^2
$$

A masked/unknown operator receives zero weight rather than a fabricated target value.

### Structural prior regularization

The existing route library or regional proxy demand can provide a **structural prior** \(x_{0,t}\), not truth.

A candidate nonnegative regularized objective is:

$$
\min_{x_t\ge0}
\quad
\|W_t(Ax_t-y_t)\|_2^2
+\lambda D(x_t\|x_{0,t})
$$

A natural candidate divergence is generalized KL / relative entropy:

$$
D_{KL}(x\|x_0)
=\sum_i
\left[
x_i\log\frac{x_i}{x_{0,i}}
-x_i+x_{0,i}
\right]
$$

for strictly positive prior support where needed.

Interpretation:

- the first term asks the inferred demand to reproduce trustworthy historical counts;
- the second term prevents the solver from wandering arbitrarily through the enormous null space;
- \(\lambda\) controls the evidence-versus-prior tradeoff.

\(\lambda\) must be selected by development-date performance after fitting on calibration evidence, not by visual preference on Feb. 1.

### Observable-class reduction

If multiple raw routes have the same operator-incidence signature, historical count evidence cannot distinguish them.

Let equivalence relation:

$$
r_i \sim r_j
\iff
A_{:,r_i}=A_{:,r_j}
$$

Then demand fitting can operate on equivalence classes first, reducing unnecessary degrees of freedom. Distribution within an equivalence class should remain prior-driven or be determined by additional evidence, not invented by the count solver.

### Within-bucket timing remains latent

A 15-minute count \(x_{r,t}\) does not identify individual departure seconds.

If departures \(\delta_{r,t,k}\) are later generated inside a bucket, then:

$$
\sum_k 1 = x_{r,t}
$$

constrains only the total count, not the distribution of \(\delta\) inside the 900-second interval.

Any uniform or shaped sub-bucket departure model is therefore synthetic/inferred and must be sensitivity-tested rather than mislabeled as observed history.

---

## 61. Validation equations and acceptance metrics

The project now has two distinct validation boundaries. They should never be collapsed into one vague "accuracy" claim.

### Gate A: traffic fidelity, MOSS vs historical reality

For operator \(e\), time bucket \(t\):

$$
r^{traffic}_{e,t}
=\hat y^{MOSS}_{e,t}-y^{hist}_{e,t}
$$

Useful aggregate metrics include:

#### Mean absolute error

$$
MAE
=\frac{1}{N}\sum_i |r_i|
$$

#### Root mean squared error

$$
RMSE
=\sqrt{\frac{1}{N}\sum_i r_i^2}
$$

#### Bias

$$
Bias
=\frac{1}{N}\sum_i r_i
$$

#### Weighted absolute percentage error

For nonnegative historical volume targets:

$$
WAPE
=\frac{\sum_i |\hat y_i-y_i|}
       {\sum_i y_i}
$$

WAPE should be reported only where the denominator and observation-health policy make it meaningful.

### Gate B: representation fidelity, WorldPack vs validated MOSS

For held-out movement observation \(i\), with actual MOSS arrival \(a_i\) and WorldPack prediction \(\hat a_i\):

$$
e_i^{WP}=\hat a_i-a_i
$$

The existing WorldPack benchmark reports median absolute error, mean absolute error, p90, p95, bias, and RMSE over these \(e_i^{WP}\).

The two residuals answer different questions:

```text
MOSS - history
    = traffic model / demand / control fidelity

WorldPack - MOSS
    = representation/compression/routing fidelity
```

A good WorldPack cannot rescue an unrealistic MOSS world, and a realistic MOSS world cannot rescue a poor WorldPack representation.

### FIFO validation

WorldPack v1.2 enforces a FIFO-safe arrival field using a cumulative maximum:

$$
A^{FIFO}_m(t_i)
=\max_{j\le i}\tilde A_m(t_j)
$$

for movement \(m\).

The invariant being protected is:

$$
t_1<t_2
\implies
A_m(t_1)\le A_m(t_2)
$$

for the same time-dependent movement cost model.

### Static free-flow lower bound

A movement's dynamic travel time should not beat its physically constructed free-flow lower bound except within declared numerical/measurement tolerance.

Conceptually:

$$
T^{ff}
=\sum_{\ell\in path}
\frac{L_\ell}{v^{max}_\ell}
$$

and:

$$
T^{pred}(t)\ge T^{ff}
$$

The v1.2 holdout strongly supports this lower-bound construction: only 13/31,465 holdout observations were more than 0.5 s below it.

### Real-time factor

Simulation performance is reported as:

$$
RTF
=\frac{T_{simulated}}{T_{wall}}
$$

so \(RTF>1\) means faster than real time.

### Warm-start convergence

Historical replay cannot use `start_step` as state reconstruction. Pre-roll must be validated by convergence.

Let \(p\) be pre-roll duration and \(z_p\) the scored state vector at a common evaluation time, containing selected flow/speed/occupancy or simulator-state summaries.

A generic normalized convergence distance is:

$$
\Delta(p_i,p_j)
=\sqrt{
\frac{1}{K}
\sum_{k=1}^{K}
\left(
\frac{z_{p_i,k}-z_{p_j,k}}
     {s_k}
\right)^2
}
$$

where \(s_k\) is a predeclared scaling term for metric \(k\).

The accepted warm-start is the **shortest** candidate whose state is stable relative to a longer pre-roll under a criterion frozen before development/blind evaluation.

Current candidate starts are approximately:

```text
15:30
15:00
14:30
```

while scoring the same 16:00+ state.

---

## 62. Signal-control provenance blocker

Signal-control provenance is now one of the highest-value unresolved simulator questions.

### Reduced SUMO signal preservation

After correcting the phase-clearing hash bug:

```text
full SUMO tlLogic programs    : 2,460
reduced SUMO tlLogic programs : 2,370
shared programs               : 2,370
exact phase programs          : 2,370
changed shared programs       : 0
removed programs              : 90
```

Retained reduced movements remain subsets of the full network and preserve their original link-index semantics.

### MOSS controlled-junction count

The corresponding MOSS map reports:

```text
controlled junctions : 13,736
```

Difference from reduced SUMO `tlLogic` count:

$$
13{,}736 - 2{,}370 = 11{,}366
$$

This strongly suggests that the converter may be synthesizing signal control at many junctions that are not explicitly signalized in the reduced SUMO source.

### Why this matters

A demand fit could mathematically compensate for artificial capacity restrictions created by extra signals. That would produce a low residual for the wrong reason and poison scenario behavior.

Therefore demand, routes, or WorldPack costs must not be tuned to hide this blocker.

### Historical timing is also unknown

The SUMO network itself was generated with default actuated signal behavior rather than verified historical agency timing. Therefore:

```text
SUMO signal programs
    != proven historical ODOT / city timing
```

The project should treat signal control as:

```text
synthetic control baseline
```

until historical timing evidence is obtained.

The next signal task is to determine whether MOSS synthesis can be disabled while preserving imported SUMO controls, then regenerate a control-faithful map and compare historical replay before any signal-timing calibration is attempted.

---

## 63. Current artifact inventory for the regional historical-calibration phase

Important evidence now includes:

```text
~/moss-gpu-test/historical-calibration/
```

### Feb. 1 station slice

```text
warm-start-v1/2024-02-01-1430-1630-station-15m.parquet
SHA-256:
85cd806c0c64a53b42d5def94e49b109dd7d5690c9a3d7ed87650ae52d3437f5
```

### Route/station identifiability diagnostic

```text
warm-start-v1/route-station-identifiability-v1.json
SHA-256:
f07d3679f6321c696648712d5e44800b02c25953eb85445b5d6e248c1fbd18bf
```

### Week-long station-health diagnostic

```text
warm-start-v1/2024-01-29-week-station-health-v1.json
SHA-256:
68d3bb03c5d5b83341b2a2ef0ca23333813898ecd45b5f79c99c991663be5300
```

### Route-induced regional network

```text
regional-load-test/h33d-1800-v1/
regional-1800s-route-induced.net.xml
SHA-256:
7c49100f4b5431b8c940948158ffd8aca005caf5f954ea054dd1077a38b20088
```

### Route-induced MOSS map

```text
regional-1800s-route-induced-map.pb
SHA-256:
131da35d95aeb6e6cf34daf3706d8d0a80a1035866922ccedade7e93144cf198
```

### Corrected flat ID mapping

```text
regional-1800s-route-induced-id2uid-roadfixed.json
SHA-256:
1005edf0ef06b38696c452d361decfe81ac49409d6dceb7851362e1036f8e3ca
```

### Structural person/route population

```text
regional-1800s-49186-persons.pb
SHA-256:
c33386730c0001deba7e1121014a73cf68ec1c80f2872b86458a739eb9735c93
```

### Historical profile manifest provenance

```text
campaign manifest SHA-256:
114012559515f2da0a715d83d11b5d11ffade932529e4937cc5d40f1602735f2

source corpus digest:
09a80fc2e419db11d19925fa078e370afccba4469af476bc1da0e4707844a37b
```

These identities should continue to be copied into new diagnostic manifests so every result can be traced back to the exact network, corpus, route basis, source build, and observation policy that produced it.

---

## 64. Updated project completion interpretation

The project should not be measured only by lines of code or UI completeness. The expensive risk is whether the webapp's baseline and scenario answers are grounded in a validated traffic world.

A useful current decomposition is:

| Workstream | Approx. state | Interpretation |
|---|---:|---|
| Architecture / PC-Mac separation | ~90% | proven and stable enough to build on |
| Simulator execution / GPU plumbing | ~90% | working; performance characterized for current regional world |
| Historical corpus / provenance | ~85% | frozen corpus and split exist; observation health layer unfinished |
| Network / station / route mapping | ~90% | broad structural coverage; some semantic/operator caveats remain |
| Historical demand inference | ~35-45% | mathematical structure understood; fitting intentionally paused |
| MOSS historical traffic fidelity | ~20-30% | diagnostic evidence exists, but signal and demand gates remain |
| WorldPack representation fidelity | ~70-80% | v1.2 benchmark is mature; severe queue tail remains |
| Scenario / closure productization | ~50-60% | architecture exists; should wait for validated baseline worlds |
| Final trustworthy webapp | ~60% | middle-to-late-middle, not production-calibrated yet |

These percentages are project-management estimates, not validation metrics.

The next genuine product milestone is:

> A frozen calibration pipeline that produces a historical MOSS world from trustworthy observations, passes development-date validation, survives the blind holdout, and can then be compiled into a WorldPack used by the existing Commute Help route/scenario UX.

At that point the project stops being primarily a traffic-model research program and becomes primarily a scenario-product engineering program.


---

## 65. Neutral Feb. 1 DE7A historical replay diagnostic

Before the broader station/operator work, a neutral baseline replay was used to test whether MOSS could reproduce one well-understood historical cross-section without hiding the result behind route or lane-subset assumptions.

### DE7A mapping

```text
PORTAL station : 1020
SUMO edge      : 186974990
edge fraction  : 0.522818782
MOSS road      : 200021718
```

The historical DE7A observation in this diagnostic represented a measured two-lane subset of a three-lane modeled road. The corresponding MOSS lane mapping used the two measured lanes explicitly while also auditing all three lanes.

This is the concrete reason the newer virtual-cross-section lane-set audit must not be interpreted as universal proof that every source station measures every real lane.

### Full neutral run

```text
simulated horizon : 18,000 s
wall time         : 5,605 s
15-min buckets    : 20
scored buckets    : 16
score window      : 14:30-18:30
```

The effective real-time factor over the full run was:

$$
RTF = \frac{18{,}000}{5{,}605} \approx 3.21
$$

but runtime degraded substantially during the long saturated simulation, approaching roughly 1.49x real time near the end.

### Volume comparison

Historical measured two-lane volume across the scored window:

```text
5,523 vehicles
```

MOSS measured-two-lane result varied by diagnostic extraction between approximately:

```text
2,011 to 2,089 vehicles
```

Coverage ratio:

$$
C_{2lane}
=\frac{V^{MOSS}_{2lane}}{V^{hist}_{2lane}}
\approx 0.364\text{ to }0.378
$$

Even counting all three MOSS lanes:

```text
3,022 to 3,100 vehicles
```

so:

$$
C_{3lane}
=\frac{V^{MOSS}_{3lane}}{V^{hist}_{2lane}}
\approx 0.547\text{ to }0.561
$$

This is an important negative result:

> The neutral modeled road under-carries the real historical two-lane measurement even when the simulation is credited with all three modeled lanes.

Therefore the earlier mismatch cannot be explained primarily by choosing the wrong simulated lane subset.

### Speed comparison

```text
historical mean speed : 22.44 km/h
MOSS mean speed       : 15.30 km/h
```

Mean speed bias:

$$
Bias_S
=15.30-22.44
=-7.14\text{ km/h}
$$

The combination of lower throughput and lower speed is consistent with an effective-capacity restriction somewhere in or upstream of the modeled cross-section. The exact mechanism is not yet proven, and the project will not change demand, lane rules, or control behavior merely to force this one diagnostic to match.

---

## 66. Calibration-v2 corpus build and provenance checkpoint

The historical corpus construction itself has advanced far beyond a one-date pilot.

Known calibration-v2 campaign identity:

```text
campaign:
portland-vancouver-core-corridor-v2-full-day

campaign manifest SHA-256:
114012559515f2da0a715d83d11b5d11ffade932529e4937cc5d40f1602735f2

source corpus digest:
09a80fc2e419db11d19925fa078e370afccba4469af476bc1da0e4707844a37b
```

The campaign compiler partitions the observation corpus into date/chunk work units and resumes idempotently.

A representative progress checkpoint recorded:

```text
partitions total      : 3,240
reusable/promoted     : 175
pending               : 3,065
committed at boundary : 174
claims                : 2,314,580
conflicts             : 0
projected storage     : ~10.75 GiB
free storage then     : ~58.60 GiB
```

The resume boundary was tested for idempotence rather than assumed.

The status path was also optimized from an initial approximately 3.89-second report to about 0.48 seconds using O(1) counters, avoiding repeated full-corpus counting during progress checks.

This matters because the historical calibration system is intended to become a repeatable experiment engine, not a one-off Feb. 1 script.

The relevant evidence hierarchy is now:

```text
raw PORTAL detector corpus
        |
        v
frozen campaign partitions
        |
        v
production detector->station compiler semantics
        |
        v
station-level diagnostic evidence
        |
        v
health-aware operator targets
        |
        v
calibration / development / blind experiments
```

The process is deliberately provenance-first: a downstream calibration result is only trustworthy if its source corpus, campaign manifest, network identity, mapping artifact, health policy, demand basis, simulator build, and scoring policy are all recoverable.

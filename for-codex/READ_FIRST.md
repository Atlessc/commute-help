# Commute Help Codex Cockpit Card

**Last updated:** 2026-08-21  
**Mode:** CODEX AUTHORING MODE

Read `/home/tyler/AGENTS.md` first. Its restrictions govern this workspace.

## Mission

Build the remaining Commute Help system while keeping all expensive or potentially sticky execution under explicit human control.

Codex:

```text
READ + REASON + DESIGN + WRITE + HANDOFF
```

Tyler:

```text
RUN + OBSERVE + RETURN SMALL RESULTS
```

## Workspace

```text
/home/tyler/
├── AGENTS.md
├── wsl-home-topology.json
├── COMMUTE_HELP_MOSS_WORLDPACK_PROGRESS_REPORT.md
├── CODEX/
├── commute-help-full/
├── moss-gpu-test/
│   ├── historical-calibration/
│   └── moss-p4000-src/
└── moss-gpu-checkpoints/
```

### Main roles

```text
commute-help-full/
    main web app, backend, frontend, traffic corpus, OSM/SUMO artifacts, WorldPacks

moss-gpu-test/
    MOSS/GPU experiments, historical calibration, runtime evidence

moss-gpu-test/moss-p4000-src/
    native MOSS C++/CUDA source

moss-gpu-checkpoints/
    recovery/checkpoint storage; do not modify casually
```

## Current scientific state

Already frozen or established:

- 20 calibration dates;
- 20 development-validation dates;
- 20 sealed target-blind dates;
- health v4 frozen;
- health tuning closed;
- no imputation on the accepted health path;
- detector-to-station historical aggregation semantics recovered;
- 362 supported station observation operators;
- 63 unresolved station cases kept explicit;
- corrected measured MOSS detector geometry for the supported operator set;
- 49,186 free historical route weights rejected as non-identifiable;
- PORTAL historical observations outrank unfinished SUMO behavioral assumptions;
- SUMO route-gap/signal findings are diagnostic, not prerequisite blockers;
- no additional Portland data acquisition is required before continuing.

## Current critical path

```text
[ ] finish global all-362 neutral MOSS detector-semantics implementation
[ ] freeze corrected dynamic observation operator
[ ] build calibration-date MOSS prediction matrix
[ ] choose reduced identifiable demand representation
[ ] calibrate on calibration evidence
[ ] validate on development dates
[ ] freeze full historical pipeline
[ ] open blind holdout once
[ ] return to WorldPack representation improvements
[ ] productize baseline/scenario/preprocessor/web-app stages
```

## Immediate safety rules

```text
NO tests
NO simulations
NO MOSS runs
NO SUMO runs
NO CUDA runs
NO builds
NO compilers
NO preprocessors
NO calibration
NO benchmarks
NO linters/formatters
NO package installs
NO servers/watchers
NO background jobs
NO blind-data inspection
NO automatic next task
```

Codex may write any of the code needed for those activities. Codex may also write tests and manual runners. It does not execute them.

## Evidence hierarchy

```text
1. PORTAL historical observations
2. reproducible observation processing / health policy
3. MOSS predictions through the same observation operator
4. residual-driven calibration + held-out validation
5. OSM/SUMO/route artifacts as structural scaffolding
6. unresolved microscopic mechanics remain explicit uncertainties
```

## Search behavior

- Prefer exact known paths.
- Use targeted reads.
- Do not use `rg`.
- Avoid broad recursive scans.
- Do not crawl caches, `.venv`, Boost, MOSS build output, or checkpoint trees unless specifically required.

## Completion behavior

A burst is complete when the one atomic objective in `CURRENT_TASK.md` is implemented as far as possible without runtime execution.

Then:

1. update `HANDOFF.md`;
2. state `Not executed; runtime verification pending` where applicable;
3. recommend one next burst;
4. STOP.

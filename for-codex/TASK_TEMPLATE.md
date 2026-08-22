# Codex Atomic Task

**Task ID:** `<short-id>`  
**Status:** READY  
**Created:** `<YYYY-MM-DD>`  
**Owner:** Tyler  
**Burst type:** AUTHORING ONLY

## Objective

One sentence. Exactly one atomic objective.

> `<objective>`

## Why this task exists

Briefly connect the task to the current project critical path.

## Read scope

Codex may inspect these paths:

```text
/path/one
/path/two
```

Additional targeted reads are allowed only when necessary to resolve a direct dependency of this objective. Do not broaden into project archaeology.

## Write scope

Codex may create or modify only:

```text
/path/to/file-or-directory
```

Any other path is read-only unless Tyler explicitly expands scope.

## Required outputs

- [ ] `<implementation file>`
- [ ] `<manual-run script, if runtime evidence will be needed>`
- [ ] `<small result summarizer or schema, if useful>`
- [ ] `/home/tyler/CODEX/HANDOFF.md`

## Explicitly forbidden for this burst

- [ ] Do not run tests.
- [ ] Do not run project code.
- [ ] Do not run MOSS/SUMO/CUDA.
- [ ] Do not build or compile.
- [ ] Do not run preprocessors/calibration/benchmarks.
- [ ] Do not install or update dependencies.
- [ ] Do not start servers/watchers/background jobs.
- [ ] Do not inspect target-blind data.
- [ ] Do not begin the next task.

The unchecked boxes above are prohibitions, not work items. Leave them unchecked.

## Known constraints / frozen decisions

```text
<task-specific constraints>
```

## Acceptance by static reasoning

Codex should be able to explain:

- what changed;
- why it matches the requested semantics;
- what invariants are preserved;
- what remains runtime-unverified.

No runtime claim is required or allowed.

## Manual validation handoff

If runtime validation is needed, create a manual-run artifact under:

```text
/home/tyler/CODEX/manual-runs/pending/
```

The manual artifact must comply with `MANUAL_RUN_CONTRACT.md`.

## Stop condition

Stop when:

```text
<objective-specific stop condition>
```

Do not continue into any downstream phase.

## Expected handoff

Update `/home/tyler/CODEX/HANDOFF.md` using the handoff template and STOP.

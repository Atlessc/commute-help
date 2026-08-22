# Manual-Run Artifact Contract

**Last updated:** 2026-08-21

Manual-run artifacts are scripts Codex may **write** but must **never execute**.

They are executed by Tyler in the appropriate WSL environment.

## Required header

Every generated shell runner should begin with a compact header:

```bash
# SCRIPT: <short-name>
# TYPE: MANUAL RUN ONLY
# PURPOSE: <one sentence>
# CODEX-EXECUTION: FORBIDDEN
# INPUTS: <paths>
# OUTPUTS: <paths>
```

If it is a retry, label it:

```bash
# TYPE: RETRY
# PURPOSE: Retry after <specific bug/failure>; <specific fix>
```

## Runtime behavior

Manual runners should:

- execute in the foreground;
- have an explicit working directory;
- use explicit input paths;
- use explicit output paths;
- avoid overwriting frozen evidence;
- use unique run/output IDs where needed;
- avoid detached child processes;
- avoid watchers;
- avoid daemons;
- avoid `&`, `nohup`, `disown`, `screen`, and `tmux`;
- exit nonzero on failure;
- print the final output paths clearly;
- create a small result summary whenever practical;
- handle interruption/cleanup if temporary resources or child processes are created;
- support resume rather than restart for expensive jobs when the underlying workflow supports it.

Do not use `set -euo pipefail` by default. In this project it can make diagnostic shells exit in ways that obscure the real failure. Use explicit error checks where behavior matters.

## Long-running work

For MOSS, CUDA, large traffic preprocessing, calibration, or other long jobs:

- the script should not require Codex to remain attached;
- the script should not background itself;
- Tyler launches it manually;
- progress/logging should go to a known file or foreground output;
- expensive work should be restart-safe or resumable where practical;
- completion should write a compact summary artifact.

## Result separation

Prefer:

```text
run-<id>/
├── raw/ or large outputs
├── runtime.log
├── provenance.json
└── summary.json
```

Codex should read `summary.json` first on the next burst.

## Summary principles

A summary should answer only what the next decision needs.

Examples:

```json
{
  "status": "PASS",
  "run_id": "...",
  "input_digest": "...",
  "output_digest": "...",
  "counts": {},
  "flagged_items": [],
  "next_decision_fields": {}
}
```

Do not copy giant tables into summaries when counts, quantiles, IDs of exceptions, and artifact paths are sufficient.

## Commands in handoffs

A handoff command must state:

```text
Application: Windows Terminal -> Ubuntu-22.04
Working directory: /home/tyler/...
Command: ...
```

Do not assume PowerShell when the command is Bash/WSL.

Do not assume WSL when the command is actually Windows/PowerShell.

## No auto-runs

Creating a runner never authorizes Codex to execute it.

The only default authorized executor is Tyler.

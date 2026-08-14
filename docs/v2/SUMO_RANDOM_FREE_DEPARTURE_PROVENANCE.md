# Phase 2.2f — SUMO `random_free` departure provenance

## Result

SUMO 1.27.1 exposes a resolved `departPos` in native `vehroute-output` for the
execution in which a vehicle is inserted. At the departure step, libsumo also
exposes the actual vehicle ID, lane, edge, lane position, and departure time.
The first mesoscopic observation cannot substitute for departure provenance:
it is post-step state without an insertion-position contract. It happened to
equal insertion in the corrected fixture, but the implementation does not rely
on that coincidence.

The initial restart-equivalence failure was caused by the diagnostic harness,
not SUMO save/load. It ran multiple independent libsumo campaigns inside one
Python process, allowing process-global native RNG/route-handler state to carry
into the checkpoint campaign. With production-equivalent process isolation—one
fresh process for the uninterrupted run and each disposable child—all nine
vehicles had identical departure times and resolved positions.

`tripinfo-output.write-unfinished` is not a workaround. After load-state,
unfinished records can report the loaded/current segment position as
`departPos` instead of the original insertion position. Completed tripinfo
records retain the actual departure information, but they arrive too late to
repair checkpoint-safe online crossing decisions without retaining uncertainty.

## Safe semantics

- Same native vehicle/traversal plus identical departure provenance may be
  deduplicated across fragments.
- Conflicting provenance is a hard error; no winner may be selected.
- Within-edge stations evaluate each station independently against actual
  insertion position.
- An insertion exactly at the station counts according to the established
  Phase 2.2c inclusive crossing rule.
- A direct insertion onto the `to` member of an edge-transition station does
  not establish predecessor-to-successor traversal.
- Completeness belongs to `station × variant × 900-second interval`:
  `flow_measurement_complete` is true exactly when that row has zero unresolved
  direct departures.

The accepted Phase 2.2e artifact is unchanged. Its 161 unresolved direct
departures per variant cannot be reconstructed because vehroute/tripinfo
departure provenance was not enabled and checkpointed during that run.

## Production integration

Departure-child vehroute capture is integrated into the existing checkpoint
transaction and is enabled only with station telemetry. The native record is
parsed after libsumo closes and before Python checkpoint state and checkpoint
metadata are written. Numeric provenance is retained; a later loaded-active
`departPos=-1` placeholder cannot overwrite it. Missing expected numeric
evidence remains an unresolved station-local count, while conflicting numeric
evidence hard-fails checkpoint promotion.

Vehroute audit output is written as gzip-compressed native XML;
the reconciled JSON fragment retains only station-relevant direct departures.
In the bounded regional run this reduced two variants of vehroute evidence from
14.9 MB uncompressed to 2.77 MB compressed, while the two durable reconciled
fragments totaled 3.9 KB.

Rerouted vehicles are represented by nested `routeDistribution` elements. The
production parser deliberately accepts the first nested native route to recover
the departure edge rather than assuming a direct `<route>` child.

The corrected feasibility artifact is schema version 2, producer
`phase-2.2f-v2`, with deterministic content digest
`61e53d7827eb546236dfec16a54eba5b0c5cbd042990f33fcf3f0b2fc9c7c076`.
The obsolete `d74814...` artifact is superseded.

In the bounded 100-second regional validation, each variant had five
station-relevant direct departures, five numeric records recovered, and zero
unresolved station-local records. The 100-second profile intentionally does not
finalize a 900-second row; the process-isolated fixture proves complete
900-second reconstruction, and production telemetry now writes explicit
resolved/unresolved counts and `flow_measurement_complete` on every completed
row.

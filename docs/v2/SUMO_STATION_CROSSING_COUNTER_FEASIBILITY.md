# Phase 2.2c mesoscopic station crossing counter feasibility

This phase evaluates a custom observer only. It does not modify the regional
worker, Phase 2.1 telemetry, comparator-v1, or the Phase 2.2b station projection
artifact.

## Result

A positional crossing count is physically defensible in SUMO 1.27.1
mesoscopic mode when the observer retains consecutive vehicle states and uses
the station edge, directed lane-position coordinate, and route occurrence.
Mesoscopic lane and XY position advance monotonically on the controlled through
movement, but updates are coarse: a one-second observation step still produced
position jumps as large as 100 m. The observer can prove that a station lies
inside a bracket; it cannot claim exact crossing time or centimeter-scale
position.

The deterministic same-edge predicate is:

```text
previous_position < station_position <= current_position
```

The emitted identity is vehicle + station + station-edge route index. That
prevents resume duplication while permitting the same vehicle to cross the
station again on a later route occurrence. Edge-transition detection requires
the route index to advance and the first station-edge position to be downstream
of the station.

## Direct departures

The first observed mesoscopic lane position is not a reliable substitute for
requested `departPos`; the fixture observed direct vehicles at positions that
differed from their requested insertion positions. Future counting therefore
requires authoritative demand departure-position provenance.

- requested insertion upstream: count when subsequent state brackets/passes the
  station;
- requested insertion exactly at the station: count once;
- requested insertion downstream: do not count;
- missing departure-position provenance: do not infer a crossing.

## Checkpoint contract

The minimum observer checkpoint contains previous relevant vehicle state,
emitted vehicle/station/route-occurrence identities, partial 900-second events,
and authoritative direct-departure positions. Nine disposable 100-second
children reproduced the uninterrupted 900-second event set with no duplicate
events. A vehicle upstream at one checkpoint and downstream after resume was
counted exactly once.

The completed station interval exposes crossing count, VPH, unique contributing
vehicles, and crossing-speed sample statistics. It intentionally does not
fabricate occupancy, density, or travel time; Phase 2.1 edgeData remains the
edge-state source.

## Speed limitation

Native speed at the first downstream observation is retained as a crossing-step
sample. Linear interpolation is recorded only as diagnostic evidence because it
assumes linear motion between coarse mesoscopic states. Neither is yet approved
as detector-equivalent instantaneous speed.

## Performance and next gate

The bounded implementation calls seven vehicle getters for every active vehicle
at every step. It is semantically useful but not a production regional design.
Regional integration should first evaluate subscriptions or filtering to edges
containing accepted stations and must repeat a bounded scaling gate. The station
projection candidates still require a separate reviewed distance/boundary
policy before comparator-v2 can consume any cross-section evidence.

Teleport, reroute-loop, and short-edge production distributions were not
exercised beyond deterministic unit-level route-occurrence and edge-transition
logic. Those remain pre-production hardening gates.

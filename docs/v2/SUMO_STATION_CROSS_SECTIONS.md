# Phase 2.2b station-to-SUMO cross-section characterization

Phase 2.2b preserves the historical measurement grain as a PORTAL station on
an accepted directed app edge, then projects that station only onto the SUMO
member geometry already accepted by the versioned Phase 2.2a relation. It does
not search the wider SUMO network or choose a different road because it is
spatially convenient.

## Production mapping state

The versioned output is
`data/sumo/networks/active/station-cross-sections-v1/`. It references, without
modifying, the Phase 2.2a relation artifact and the original edge map.

- 426 accepted historical stations are retained independently.
- All 426 participate in Phase 1 historical profiles after canonicalizing raw
  numeric station IDs to the matcher form `portal-station-{id}`.
- 190 stations are associated with a single SUMO edge relation and 235 with a
  proven ordered chain. One station is not projectable because its accepted app
  edge has no accepted SUMO relation.
- Multiple longitudinal stations are preserved: 85 app edges contain more than
  one station, covering 213 stations.
- No production cross-section is accepted. The raw projection-distance and
  nearest-member-boundary distributions are evidence for a later reviewed
  policy; Phase 2.2b does not invent thresholds to maximize coverage.

The mapping artifact stores the chosen candidate member, member and whole-chain
position, lateral projection distance, upstream/downstream member-boundary
distance, direction evidence, detector support, lane identities, and immutable
source identities. A candidate mapping ID excludes mutable measurements.

## Historical aggregation contract

Phase 1 collapses raw detector/lane rows to a station by summing vehicle counts
and flow, using positive-volume-weighted speed (median fallback), and taking the
arithmetic mean of present occupancy. It then uses the median across multiple
stations on the same app edge/date/bucket. A future simulated cross-section
measurement must preserve the same two-stage spatial grain rather than summing
traffic repeatedly along a chain.

## Installed SUMO E1 feasibility

The bounded installed-SUMO 1.27.1 mesoscopic proof used a two-lane synthetic
network, 18 vehicles traversing the measured edge, two vehicles departing
directly onto it, co-located lane E1 definitions, a downstream E1 definition,
and nine 100-second save/load child runs reconstructing one 900-second window.

The result is a rejection, not partial support:

- save/load fragment reconstruction is exact and produces no duplicate fragment
  identities;
- each lane E1 reports the same 18 through vehicles, so summing lanes triples
  segment traffic in the fixture;
- moving E1 downstream on the same mesoscopic segment reports the same count;
- the two vehicles departing directly onto the measured edge are not counted;
- reported speed and occupancy are mesoscopic segment statistics, not proven
  point-crossing measurements.

Therefore native E1 is **not approved** as the PORTAL-equivalent point flow or
speed mechanism under the current mesoscopic, checkpointed architecture. It is
not wired into the regional worker. Phase 2.1 edge telemetry remains unchanged.

## Next measurement question

Comparator-v2 remains physically blocked. The next bounded investigation should
evaluate a checkpoint-safe cross-section counter derived from vehicle edge/lane
transition events (including direct departures) or a deliberately microscopic
detector submodel. Either option must prove exact 900-second counting, no resume
duplicates, point-speed semantics, and the Phase 1 station aggregation mirror
before it can consume the candidate station projections.

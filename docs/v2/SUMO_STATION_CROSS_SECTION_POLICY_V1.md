# Phase 2.2d station cross-section policy-v1

Policy `phase-2.2d-policy-v1` converts the immutable 426-row Phase 2.2b
characterization into explicit flow-location decisions. It does not modify the
source artifact, collect telemetry, approve point speed, or calibrate SUMO.

## Rules

Direction compatibility is a hard gate. A projectable station with lateral
projection distance above 25 m remains `review_projection_distance`. The 25 m
limit is not a percentile cutoff: production examples above it include ramps,
interchange geometry, divided-carriageway offsets, and mappings whose selected
cross-section would require physical review. The limit deliberately retains
uncertainty rather than maximizing coverage.

For a projection at most 25 m from the selected relation:

- a station at least 25 m from either member boundary is
  `accepted_within_edge` with `cross_section_type=within_edge_position`;
- a station within 5 m of an internal boundary shared by two sequential members
  of the proven directed chain is `accepted_edge_transition`; the policy stores
  both transition members and does not pretend the station belongs definitively
  inside either edge;
- any other station within 25 m of a selected member boundary remains
  `review_boundary`. This includes single-edge endpoints, outer chain endpoints,
  and points 5–25 m from an internal boundary where equivalence is not tight
  enough for automatic promotion;
- absence of an accepted relation remains `unmatched_relation`.

Road name, ref, road class, original station-to-app-edge confidence, detector
support, and location text remain diagnostic provenance. Missing or generic road
names do not override the already accepted directed app/SUMO relation. All 425
projectable source candidates are direction compatible and all 426 source
station-to-app-edge mappings have high matcher confidence.

## Measurement eligibility

Accepted within-edge and edge-transition geometries are flow-location eligible
because Phase 2.2c proved positional and transition crossing counts. Every speed
eligibility value remains false: mesoscopic crossing-step speed is bounded
diagnostic evidence, not approved detector-grade point speed.

Multiple stations remain independent. Direct-profile coverage requires every
station contributing to the Phase 1 app-edge profile to be flow eligible; partial
station coverage is not silently substituted for Phase 1's median-across-stations
target.

## Production outcome

- Accepted: 356 of 426 stations (83.57%)
- Accepted within-edge: 344
- Accepted edge-transition: 12
- Review: 69
- Unmatched: 1
- Flow eligible: 356
- Speed eligible: 0
- Direct profiles with all contributing stations accepted: 88,585 of 112,265
- Human-review package: 70 rows, including the unmatched station

The review package is
`station-cross-section-policy-v1/station-cross-section-review.parquet`. It is
bounded and includes station location, coordinates, road/ref/direction, app edge,
SUMO member and transition evidence, projection and boundary distances, proposed
state, and reason.

Phase 2.2e may use the accepted station/type counts to design a bounded regional
observer retrieval strategy. Comparator-v2 remains blocked until that integration
and its real regional acceptance gate pass.

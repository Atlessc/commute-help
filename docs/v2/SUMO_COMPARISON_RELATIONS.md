# Phase 2.2a — accepted app-edge ↔ SUMO-edge comparison relations

Status: complete analysis. This phase does not modify the accepted edge map or comparator-v1 and does not authorize calibration.

## Source identities

- Graph: `2026-08-08-portland-vancouver-frozen-v2`
- SUMO network: `pv-sumo-2026-08-08-v1`
- Original edge-map SHA-256: `2db8fce3b5101a357193c58039ff5311463eb858059b164c0e31a4bcd714ad97`
- SUMO network SHA-256: `17bb45772143fbe98241796d4205cf70f7c302130f9573dfaa25357417d06234`
- Resolver: `app_sumo_comparison_relation_resolver / phase-2.2a-v1`
- Algorithm: `directed-member-topology-v1`

The resolver reads accepted relation members from the immutable edge map, proves directed topology from SUMO edge `from`/`to` junctions, and reads lane count, lane length, speed limit, connectivity, internal status, and geometry provenance from the frozen network/map artifacts. Source row order is never topology evidence.

## Classification rules

| Class | Deterministic meaning | Comparison use |
|---|---|---|
| `single_edge` | One distinct accepted non-internal SUMO member | Existing comparator-v1 relation |
| `ordered_linear_chain` | One weak component, exactly one directed start/end, one predecessor/successor per internal member, all members visited once | Topology eligible |
| `ordered_chain_with_side_connections` | Same directed-path proof, with non-member connections at internal chain positions | Topology eligible; flow location remains unresolved |
| `parallel_candidates` | At least two accepted members share directed endpoints | Excluded; no winner |
| `branching_candidates` | A member has multiple selected predecessors or successors | Excluded; no branch selected |
| `disconnected_candidates` | Selected members form multiple weak components | Excluded |
| `cycle_candidates` | Members form a directed cycle without a unique start/end | Excluded |
| `direction_conflict` | Chain reverses app-edge identity or a member reverses direction | Excluded |
| `identity_conflict` | Duplicate/inconsistent provenance or internal member | Excluded |
| `unresolved` | Required evidence is absent or traversal order cannot be uniquely proven | Excluded |

Side connections do not invalidate traversal order. They do invalidate any simplistic claim that entrance flow equals flow everywhere along the chain.

## Production result

The 14,271 one-to-many app edges contain 33,125 accepted relation members:

| Relation class | App edges |
|---|---:|
| Ordered chain with side connections | 13,829 |
| Parallel candidates | 307 |
| Disconnected candidates | 130 |
| Branching candidates | 3 |
| Cycle candidates | 2 |
| Unresolved | 0 |

No one-to-many production relation qualified as a side-connection-free linear chain. That is plausible in a road network: intermediate SUMO junctions commonly retain turn, ramp, continuation, or U-turn connections even when the selected members themselves form one unambiguous path.

Cardinality:

| SUMO members | App edges |
|---:|---:|
| 2 | 11,212 |
| 3 | 2,234 |
| 4 | 475 |
| 5 | 208 |
| 6 | 80 |
| 7 | 27 |
| 8 | 9 |
| 9 | 12 |
| 10 | 2 |
| 13 | 4 |
| 14 | 4 |
| 18 | 2 |
| 23 | 2 |

## Metric semantics

### Flow

Sequential member flow MUST NOT be summed. A vehicle traversing `A → B → C` would otherwise receive three votes.

The only defensible chain-wide v1 approximation currently available is entrance inflow:

```text
entrance count = first_member.entered + first_member.departed
entrance VPH = entrance count × 3600 / interval_seconds
```

This includes vehicles emitted directly onto the entrance member. It does not capture on-ramp entries later in the chain and does not represent downstream flow after exits. More importantly, accepted station points have not been projected through a versioned station-to-SUMO-segment contract. The entrance quantity therefore remains an approximation and is **not primary detector-comparable flow** in this artifact.

### Speed and travel time

For a proven ordered chain:

```text
observed chain travel time = Σ segment travel time
chain traversal speed = Σ segment length / Σ segment travel time
```

This is the physically appropriate distance-over-time (length-weighted harmonic) treatment. Segment speeds are never arithmetic-averaged. If any required segment length/travel-time evidence is absent or nonpositive, traversal speed and dependent metrics remain null.

### Reference time and slowdown

```text
reference chain travel time = Σ(segment length / authoritative segment speed limit)
chain slowdown = observed chain travel time / reference chain travel time
```

This is equivalent to the project's `reference_speed / observed_speed` multiplier. Segment slowdown ratios are never averaged. A future comparator must explicitly reconcile this SUMO-network reference definition with the historical app-edge reference definition before scoring slowdown.

## Direct-profile coverage

- Direct profiles: 112,265
- Existing unique-one-edge comparable: 52,262
- Additional profiles on proven ordered chains: 59,525
- Review/absent: 478
- Hypothetical topology-comparable: 111,787 (99.57%)
- Primary flow-comparable under the current detector-alignment contract: still 52,262

The 442 topology ambiguities occur outside the current direct-evidence profile set. This explains why all 59,525 formerly ambiguous direct profiles become topology-resolved without claiming that their flow cross-section is resolved.

Corridor diagnostics:

| Corridor | Current unique | Ordered chain | Hypothetical topology total |
|---|---:|---:|---:|
| I-5 | 15,642 | 21,624 | 37,266 |
| I-205 | 8,924 | 12,374 | 21,298 |
| Interstate Bridge | 0 | 960 | 960 |
| Glenn L. Jackson Memorial Bridge | 0 | 1,453 | 1,453 |
| Marquam Bridge | 0 | 900 | 900 |
| OR-217 | 2,084 | 2,317 | 4,401 |
| I-84 / US-30 | 7,069 | 7,013 | 14,082 |
| US-26 | 3,353 | 4,497 | 7,850 |

## Artifact

`data/sumo/networks/active/comparison-relations-v1/` contains:

- `app-sumo-comparison-relations.parquet`
- `comparison-relation-summary.json`
- `COMPARISON_RELATION_ANALYSIS.md`
- `comparison-relation-manifest.json`

Each row retains deterministic relation identity, accepted and proven ordered member IDs, junction topology, member lengths/lanes/speed limits/internal status, side-connection count, app/SUMO geometry hashes, OSM-way provenance, mapping scores, station support, eligibility dimensions, exclusion reason, and source hashes. Construction is pending-directory based and atomically promoted only after typed manifest validation.

## Phase boundary

A comparator-v2 can safely implement ordered-chain **speed and travel-time aggregation** from this artifact. It must not promote chain-entrance flow to detector-comparable primary scoring until a versioned station/detector-to-SUMO cross-section relation is accepted. Phase 2.3 remains blocked; the next decision is whether to build that cross-section mapping or deliberately support metric-specific comparator coverage.

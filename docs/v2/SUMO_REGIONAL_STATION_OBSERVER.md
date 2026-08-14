# Phase 2.2e regional station crossing observer

Station telemetry is an optional flow-only evidence layer. It loads exactly the 356 accepted Phase 2.2d cross-sections, preselects routed vehicles whose route intersects a station or necessary predecessor edge, and uses libsumo subscriptions for road ID, lane position, and route index. Speed is deliberately not subscribed or promoted.

Each disposable 100-second child reconstructs subscriptions and restores the versioned observer state from the atomically promoted Python checkpoint. State contains previous relevant observations, active traversal identities, partial 900-second station accumulators, and bounded operational counters. Complete dense station rows are promoted only after both variants complete.

If both variants have a complete, contiguous promoted checkpoint chain through
the final 900-second boundary but the outer wrapper fails in a later unrelated
diagnostic, `promoted-checkpoint-chain-v1` can recover the station artifact
without running SUMO again. Recovery validates all 18 checkpoints and their
network, demand, seed, policy, observation-plan, station-fragment, native
departure, and parent-state lineage, then invokes the same normal station
materializer. The failed wrapper result remains failed; recovery lineage is
atomically promoted beside the telemetry artifact.

The clean matched 900-second regional benchmark measured 3,668.893 seconds without the observer and 3,709.795 seconds with it, a 1.1149% increase. Playback was byte-identical; Phase 2.1 edgeData contained identical metric cells. The observer is operationally viable.

## Direct departure limitation

Regional demand currently uses `departPos="random_free"`. This is not a resolved numeric insertion position. The observer therefore does not manufacture a crossing when a vehicle first appears on a relevant edge without numeric departure provenance. The benchmark saw 161 such relevant departures per variant. Performance evidence remains valid, but comparator-v2 primary flow remains blocked until these observations are captured authoritatively or marked incomplete at station/interval grain.

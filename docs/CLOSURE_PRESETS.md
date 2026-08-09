# Closure presets

Closure presets are public, reusable road-impact layouts. They contain no trip
origin, destination, private address, or saved scenario. The catalog lives at
`data/presets/closure-presets.json` and is served read-only through
`GET /api/closure-presets`.

## I-5 Rose Quarter southbound closure

The `i5-rose-quarter-southbound-fall-2026` preset represents the supplied ODOT
project map on graph version `2026-07-31-portland-vancouver-v1`:

- 8 directed full-closure sections
- 3 directed sections with one lane remaining from the I-405 split to the
  Broadway/Weidler exit approach
- no restriction on I-5 northbound
- no restriction on the I-5 southbound detour to I-405
- no restriction on I-84 westbound to I-5 southbound
- no restriction on the Broadway/Weidler exit

ODOT currently says the mainline closure is expected to start around 10 p.m. on
September 11, 2026, with some ramp closures possibly beginning at 9 p.m. It is
planned around the clock for up to five weeks, but the exact reopening time has
not been published. Because a guessed end time could silently make a simulated
closure inactive, the preset loads its restrictions as active for the trip time
the user selects. Add a schedule only after ODOT confirms the end.

Sources checked August 8, 2026:

- [Construction Travel Impacts](https://www.i5rosequarter.org/current-construction-travel-impacts/)
- [I-5 Rose Quarter FAQs](https://www.i5rosequarter.org/faqs/)

## Safety behavior

The backend compares the preset's graph version, directed `u`/`v`/`key`, edge
ID, and geometry fingerprint with the active graph. The Apply button is disabled
if any identity check fails. The preset must then be reviewed and rebuilt; it is
never silently remapped.

After an official closure-map revision or graph rebuild:

1. Review each full closure, lane restriction, direction, and explicitly open
   connection against the current ODOT map.
2. Select the replacement directed edges using the normal map workflow.
3. Update the preset snapshots and graph version.
4. Run `npm run test:backend` and `npm run test:frontend`.
5. Open a representative trip, apply the preset, and visually confirm all
   marked lines before relying on a simulation.

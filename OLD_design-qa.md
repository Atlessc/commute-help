# Design QA — Map-first simulation workspace

## Evidence

- Reference: `/Users/USERNAME/.codex/generated_images/019fbb08-4b54-7280-a61e-eef5536be811/exec-236c629d-9eb5-40b2-8c2a-86e6d5489b5c.png`
- Reference dimensions: 1484 × 1060 pixels
- Implementation capture: `/tmp/commute-help-final-desktop-bytes.jpg`
- Implementation dimensions: 1440 × 1024 pixels
- Combined comparison: `/tmp/commute-help-final-comparison.png`
- Browser viewport: 1440 × 1024 CSS pixels
- Secondary live viewport: 1280 × 720 CSS pixels
- Product state: I-5 Rose Quarter southbound closure, active modeled simulation, selected trip in progress, traffic samples visible, and traveled/projected route split visible

## Comparison history

1. Initial implementation exposed the full model form in the open Simulation module, retained the old six-step header during playback, used an overly wide command rail, and did not follow the selected SIM trip.
2. The next pass introduced the compact simulation header, reduced the command rail, added route-following playback, made timeline and speed presets directly selectable, and preserved the solid blue traveled path with a teal projected path.
3. The final pass replaced the exposed model form with active-scenario, trip, and route summary cards; kept model settings behind a disclosure; capped follow-camera zoom for regional context; and made the command rail independently scrollable at shorter laptop heights.

## Visual and interaction checks

- Layout and hierarchy: passed at 1440 × 1024 and 1280 × 720; the map remains the dominant surface and the playback dock does not overlap the command rail.
- Typography, color, surfaces, and icons: passed; existing design tokens and Lucide icons are used consistently.
- Route presentation: passed; the selected trip uses solid blue for traveled geometry, dashed teal for the remaining route, and a distinct SIM position marker.
- Traffic presentation: passed; dots stay on returned road geometries and use green, amber, and red congestion states with a visible legend.
- Controls: passed; restart, play, pause, timeline jumps, continuous speed control, and 1×/5×/10×/20×/50× presets were exercised through the real browser UI.
- Workflow modules: passed; Trip, Closures, Conditions, Simulation, and Save expand and collapse, with detailed model settings still reachable.
- Smaller laptop viewport: passed at 1280 × 720 with no horizontal overflow; the sidebar scrolls independently when vertical space is constrained.
- Mobile breakpoint: static CSS review passed for the existing 800-pixel breakpoint; the browser runtime used for this QA did not expose a sub-1280 live viewport.
- Accessibility: semantic buttons, labels, status regions, keyboard focus styles, and non-color route labels are present; playback begins paused.
- Browser diagnostics: passed in a fresh end-to-end run with no console errors or warnings.
- Automated verification: `npm test` passed all 7 campaign tests, 50 backend tests, frontend lint, TypeScript compilation, and the production build.

## Known model boundary

The visible traffic dots are deterministic samples tweened between the backend's baseline and scenario flow targets. They are not yet autonomous traveler agents with individual destination choice, patience, map-provider adoption, illegal-maneuver probability, crash generation, or within-run route replanning. The UI labels this output `Modeled · uncalibrated` and does not present it as historical or live traffic.

## Final result

**Passed.** No unresolved P0, P1, or P2 visual or core-interaction findings remain for this map-first aggregate playback slice.

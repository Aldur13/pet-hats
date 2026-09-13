# dronegoto

Give it a coordinate. It flies there, hovers, and comes home — under continuous
safety supervision, with every limit exposed as a setting.

```
dronegoto fly "51.5074, -0.1278" --alt 100 --hover 300
```

Accepts anything you can copy out of Google Maps: `51.5074, -0.1278`, a DMS pair
like `51°30'26"N 0°07'39"W`, or a full Maps URL (it prefers the pinned place over
the camera position).

## Before you start: will this fly your drone?

**If you have a DJI Mini 4K — no, and nothing here can change that.** DJI's own
product SDK compatibility table lists it as *SDK compatibility: No*. No Mobile
SDK v5, no MSDK v4, no Payload SDK, and no Waypoint mode even in the DJI Fly
app. Third-party apps like Litchi cannot fly it either, because they drive DJI
aircraft through the same SDK the Mini 4K does not expose.

| Aircraft | Status |
|---|---|
| PX4 / ArduPilot build | **Works today** via the MAVSDK backend |
| DJI Mini 4 Pro / Mini 3 / 3 Pro, Matrice, Mavic 3 Enterprise | Needs a Kotlin port — MSDK v5 is Android/iOS only |
| DJI Mini 4K, Air 3, Mavic 3 Classic, Mini 2 | **Not possible** — no SDK |

Full detail in [docs/HARDWARE.md](docs/HARDWARE.md).

Everything except the aircraft link is airframe-independent and runs right now
against the built-in simulator.

## Try it without hardware

```bash
pip install -e ".[dev]"

dronegoto check                              # validate config, print every setting
dronegoto rules                              # list the 16 safety rules
dronegoto preview "51.5074, -0.1278"         # plan it, get a GO/NO-GO verdict
dronegoto fly "51.5074, -0.1278" --backend sim
dronegoto sim-scenario list                  # 12 named failure scenarios
dronegoto sim-scenario impact                # watch a collision abort the mission
dronegoto replay flights/<timestamp>.jsonl   # read back what happened and why
```

## How safety works

Protection sits at three independent levels, each surviving the failure of the
one above it:

1. **This supervisor** — 16 rules, evaluated every tick.
2. **The aircraft's own geofence** — uploaded before arming.
3. **The aircraft's failsafe parameters** — battery, RTL altitude and flight-time
   limits written to the flight controller, so it protects itself even if the
   machine running this code dies mid-flight.

The engine is a pure function of `(telemetry, history, config, mission state)`.
No network, no clock, no aircraft. That is what lets all 16 failsafes be tested
on the ground, which is the entire reason this was built simulator-first.

### What it watches for

Battery (layered low → critical → emergency), **distance-aware return energy**,
collision, freefall, uncommanded descent, geofence, altitude ceiling, terrain
clearance, GPS quality and loss, stale telemetry, link loss, failure to make
progress, flight-time and hover budgets, and wind. Full table with defaults in
[docs/SAFETY.md](docs/SAFETY.md).

Four decisions are worth calling out, because the obvious implementation gets
each of them wrong:

- **A fixed battery percentage is not enough.** At 4 km out, 44% may already be
  too little to get home. A distance-aware estimator runs alongside the fixed
  threshold — whichever fires first wins — and it revises itself upward when the
  measured drain exceeds the configured figure (a headwind, a cold pack). It
  never revises downward.
- **Geofences that act after the breach have already spent the margin.** This one
  projects the current track forward and turns back before the boundary.
- **Most severe wins, never first-match.** Every rule runs every tick and the
  highest severity is taken, so rule ordering can never hide an emergency.
- **Absent telemetry is itself an alarm.** A monitor that only reacts to data it
  receives goes quiet exactly when it matters. Unknown values are treated as the
  worst case, not as nominal.

## Configuration

Every threshold lives in [`config/default.yaml`](config/default.yaml), annotated.
Unknown keys are **rejected at load time** rather than ignored — a typo like
`max_altitiude_m` would otherwise silently leave the default ceiling in force
while you believed you had changed it. Contradictory settings (a critical battery
threshold above the low one, a duration gate shorter than two control ticks) are
rejected the same way.

```bash
dronegoto fly "51.5074,-0.1278" --config my-airframe.yaml
```

## Testing

```bash
pytest                      # 160 tests, no hardware required
```

- Every rule tested for firing **and** for staying quiet on healthy telemetry.
- Arbitration tested exhaustively across all orderings of four severities.
- 12 fault-injection scenarios flying complete missions that break in one
  specific way each.
- A randomised chaos suite asserting the aircraft always reaches a defined safe
  terminal state under simultaneous faults.

The test suite has already earned its keep: it caught an impact rule whose
confirmation window was shorter than the control tick — so a collision could
never have been detected in flight — and a float-accumulation bug that silently
prevented freefall detection from ever confirming.

## Flying a real aircraft

```bash
dronegoto fly "51.5074,-0.1278" --backend mavsdk --address udp://:14540
```

Arming is the irreversible moment, so it asks for explicit confirmation and
refuses to run non-interactively without `--yes`.

Test in SITL first. Airspace, line of sight, altitude limits and Remote ID are
yours to comply with — most jurisdictions require visual line of sight, and
flying beyond it needs specific authorisation.

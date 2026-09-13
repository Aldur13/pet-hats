# Building a manual + click-flip + autonomous 5" quad

This is a different machine from the DJI-oriented system in the rest of this
repo (see [HARDWARE.md](HARDWARE.md)): a DIY 5-inch ArduCopter build capable of
fast manual flying, a software-triggered flip, and `dronegoto` autonomous
missions, all on one aircraft. Read the trade this forces before buying parts:

**Betaflight flies better by hand. ArduPilot flies itself.** There is no
firmware that wins both. Because you want manual + click-flip + autonomous on
one aircraft, this build standardizes on **ArduPilot Copter**, the only
firmware with a real autonomous-mission system (the one `dronegoto` already
drives) *and* a genuine single-flip mode. Manual ACRO-mode flying on ArduPilot
is tunable toward a Betaflight-like feel (see
[ACRO_TUNING.md](ACRO_TUNING.md)) but most pilots and ArduPilot's own
community describe it as a step below a dedicated Betaflight freestyle quad.
That is the price of unifying all three capabilities on one airframe.

## Bill of materials

5-inch, 6S, GPS-equipped. The GPS/compass module is the one addition a pure
freestyle build wouldn't carry - budget ~15-20g and a small hit to top speed
and flip crispness for it.

| Item | Budget | Mid-range | Notes |
|---|---|---|---|
| Frame | $30 (TBS Source One V6) | $90 | 6mm arms survive crashes |
| Flight controller + ESC stack | $70 (SpeedyBee F7 V3) | $100+ | **must run ArduPilot** - check the FC is on ArduPilot's supported hardware list, not a Betaflight-only AIO board |
| Motors (set of 4) | $68 (Xing-E Pro) | $100+ | |
| GPS + compass module | $25 | $45 (M10, faster lock) | required for autonomy and safe RTL |
| ELRS receiver | $15 | $25 | |
| Props/wiring/misc | $30 | $50 | |
| **Airframe subtotal** | **~$240** | **~$430** | |
| Battery (6S LiPo, 2 packs) | $60 | $100 | |
| Radio transmitter | $50 (BETAFPV) | $150 (RadioMaster) | |
| FPV goggles | $55 (analog) | $229+ (DJI O4 digital) | |
| **All-in, first build** | **~$400-450** | **~$800-950** | |

Start with the budget column. Get manual flight solid and cheap to crash
first; upgrade once flip and autonomy are proven.

## Software map

| Capability | What runs it | New code in this repo |
|---|---|---|
| Manual flying | ArduPilot ACRO mode, tuned via Mission Planner/QGC | none - configuration only, see [ACRO_TUNING.md](ACRO_TUNING.md) |
| Click-to-flip | ArduPilot's FLIP mode, triggered by a MAVLink mode change | `dronegoto/tricks.py`, `dronegoto flip` CLI |
| Autonomous goto | The existing safety engine + Mission API | `config/fastquad.yaml` retunes speed/battery/geofence for a fast airframe; everything else in the repo is unchanged |

The click-flip button does not implement a flip - it asks ArduPilot's own
FLIP mode to do it (`ArduCopter/mode_flip.cpp`), and only after checking
altitude, battery, GPS fix and level attitude first, the same preflight-gate
philosophy the rest of this repo uses before a mission arms. See the scope
note at the top of `tricks.py`: this gate is a standalone check meant for
ordinary hand-flying, not something that runs concurrently with an active
autonomous mission in the same process - flipping while a mission's own
safety engine is watching accelerometer data will look like a collision to
`rule_impact` and abort the mission, correctly, from its point of view.

## Build order

This is a sequenced build, not a simultaneous one. Doing all three
capabilities at once on a first build is how people crash on the maiden
flight.

1. **Assemble and hand-fly with no GPS module installed.** Prove the frame,
   motors, ESCs and a basic ACRO/STABILIZE tune are solid before adding
   anything else.
2. **Add GPS/compass.** Run ArduPilot's compass and GPS calibration in Mission
   Planner/QGroundControl. Confirm a healthy fix and correct home-position
   capture before trusting RTL or a mission.
3. **Enable FLIP mode on an RC auxiliary switch first** - this is stock
   ArduPilot behaviour, zero new code. Prove it in the air, at real altitude,
   conservatively, before any software trigger touches it.
4. **Wire up `dronegoto flip`.** Test the gate against the simulator first
   (`dronegoto flip --backend sim --airborne`), confirm it refuses correctly
   when altitude/battery/attitude are out of bounds, then try it for real -
   it should behave identically to the RC switch you already proved in step 3.
5. **Fly an autonomous mission.** Use `config/fastquad.yaml`, start with a
   short, low, slow waypoint (`--alt` well under the ceiling, a target a few
   hundred metres out) before trusting a full-speed leg. Re-measure your
   actual pack's drain curve (`battery.hover_drain_pct_per_min` and
   `cruise_drain_pct_per_km` in the config) rather than trusting the profile's
   priors - they are a starting point, not a substitute for your own numbers.

## What I can't verify from here

Everything above the flight line is a physical build - I cannot fly it, tune
it, or confirm a specific FC/ESC/motor combination behaves as documented.
Treat the parts list as a well-sourced starting point, the parameter names in
`ACRO_TUNING.md` as verified against ArduPilot's own documentation, and the
build order as the actual safety argument: never skip a step ahead of the one
before it being proven by hand.

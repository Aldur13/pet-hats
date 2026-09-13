# Tuning ArduCopter ACRO mode for manual freestyle flying

This is configuration guidance, not software - every parameter here is set
through Mission Planner or QGroundControl's parameter list (ArduPilot's
equivalent of the Betaflight Configurator), not code. Parameter names and
defaults below are taken from ArduPilot's own ACRO mode documentation
(`ardupilot.org/copter/docs/acro-mode.html`); values you'll actually want
depend on your frame/motor/prop combination and are something to tune in the
air, not guess up front.

## Read this first

ArduPilot's own community is direct about this: ACRO mode is tunable toward a
Betaflight-like feel, but most pilots describe flying aggressive freestyle on
stock ArduPilot as "fighting the firmware" compared to a dedicated Betaflight
build. This guide gets you as close as ArduPilot allows. If you decide the gap
matters more than the unified manual+flip+autonomous build, that's the
Betaflight trade-off described in [BUILD_FASTQUAD.md](BUILD_FASTQUAD.md).

## Core rate and expo parameters

| Parameter | What it controls | Default | Notes |
|---|---|---|---|
| `ACRO_RP_RATE` | Max roll/pitch rotation rate | 4.5 (~200°/s) | Betaflight racing rates often run higher (250-400°/s); raise gradually, don't jump straight to a racing preset |
| `ACRO_Y_RATE` | Max yaw rotation rate | 4.5 (~200°/s) | |
| `ACRO_RP_EXPO` | Expo curve on roll/pitch stick input | 0 (applies 30% expo) | Softens center-stick sensitivity while keeping full deflection rate - the same idea as Betaflight's rates/expo split |
| `ACRO_Y_EXPO` | Expo curve on yaw stick input | 0 (30% expo) | |

## Auto-leveling behaviour

| Parameter | What it controls | Default | Notes |
|---|---|---|---|
| `ACRO_TRAINER` | 0 = pure rate mode (no self-leveling), 1 = auto-level only, 2 = auto-level with a 45° lean limit | 2 | Freestyle pilots coming from Betaflight generally want **0** - full rate control, no invisible hand pulling it level mid-flip |
| `ACRO_BAL_ROLL` / `ACRO_BAL_PITCH` | Aggressiveness of auto-leveling when `ACRO_TRAINER` > 0 | 1.0 | Irrelevant once `ACRO_TRAINER=0` |

## Getting closer to Betaflight-style tuning

`ACRO_OPTIONS` bit 1 enables **"Rate Loop Only"** - pure gyro stabilization
with the rate controller's I-term scaling proportionally to the angle P gain.
ArduPilot's own documentation describes this as behaving "more like Betaflight
tuning" and is the specific option to look at if you're transitioning from a
racing-quad background and the default feel is fighting you.

## Snappiness and overshoot

| Parameter | What it controls |
|---|---|
| `ATC_ACC_R_MAX`, `ATC_ACC_P_MAX` | Max roll/pitch acceleration (centi-deg/s²) - reduces overshoot and bounce-back coming out of a hard stick input |
| `ATC_ACC_Y_MAX` | Same, for yaw |
| `ATC_ANGLE_MAX` | Lean angle limit outside ACRO (doesn't affect pure-rate ACRO with `ACRO_TRAINER=0`) |
| `ATC_THR_MIX_MAN` | Attitude vs. throttle control balance - raise for better "airmode"-like behaviour (motors keep authority at low/zero throttle) |

## A starting checklist, in order

1. Complete ArduPilot's standard ESC calibration and motor-direction test
   before touching any of the above - a rate/expo tune cannot fix a wiring
   problem.
2. Run **AUTOTUNE** first (ArduPilot's own automated PID tuning flight mode)
   to get a sane baseline `ATC_RAT_RLL_*` / `ATC_RAT_PIT_*` rate-controller
   tune for your specific airframe. Manual rate/expo tuning on top of a bad
   baseline tune will feel wrong no matter what you set.
3. Set `ACRO_TRAINER = 0` once you're comfortable manually recovering from any
   attitude - this is what actually removes the "invisible auto-level" feel.
4. Raise `ACRO_RP_RATE` / `ACRO_Y_RATE` in small increments, test-flying
   between each change, rather than jumping to a racing-quad preset value.
5. Try `ACRO_OPTIONS` bit 1 (Rate Loop Only) if the tune still feels sluggish
   compared to a Betaflight quad you've flown before.
6. Confirm FLIP mode on an RC switch (see BUILD_FASTQUAD.md step 3) only once
   the rate tune above feels controllable - flipping on top of a bad tune is
   how a first build gets bent.

## What I didn't invent

Specific numeric starting values for `ACRO_RP_RATE`, the `ATC_RAT_*` PID
gains, or `ATC_ACC_*` limits are not given here as concrete numbers, because
they are genuinely airframe-specific (motor KV, prop size and pitch, all-up
weight) and a wrong guess printed in a doc is worse than no guess - use
AUTOTUNE and ArduPilot's own tuning guide for your specific build rather than
a number copied from someone else's quad.

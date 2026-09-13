# Safety rules

Generated from the code by `python3 tools/gen_safety_docs.py`.
Do not edit by hand.

16 rule functions are evaluated on **every** tick. They emit named
triggers, and the arbiter takes the most severe action across all of them —
never first-match, so rule ordering can never hide an emergency.

## Severity ladder

```
NONE < WARN < HOLD < RETURN < LAND < TERMINATE
```

`RETURN` and above **latch**: once the mission is abandoned, a recovering
sensor reading cannot talk the aircraft into continuing. `WARN` and `HOLD`
do not latch, because transient conditions should not ground an aircraft.

`TERMINATE` is downgraded to `LAND` unless `behaviour.allow_terminate` is set.
Cutting the motors is the one action guaranteed to destroy the aircraft.

## Rules

| Rule function | What it catches |
|---|---|
| `battery_levels` | Layered thresholds. An unknown battery is treated as a low battery. |
| `return_energy` | Return while the charge to get home still exists. |
| `impact` | Collision detection from an accelerometer spike or an attitude excursion. |
| `freefall` | Sustained near-zero g means the aircraft is falling, not flying. |
| `uncommanded_descent` | Losing height while cruising or hovering, when nothing asked it to. |
| `geofence` | Predictive cylindrical fence. |
| `altitude_ceiling` | Climbing above the configured ceiling, usually the local legal limit. |
| `terrain_clearance` | Height above the ground beneath us, not above the launch point. |
| `position_unknown` | No position fix at all. Landing here beats flying blind toward a guess. |
| `gps_quality` | Degraded or lost satellite fix, escalating with how long it persists. |
| `telemetry_stale` | Absence of telemetry is itself an alarm. |
| `link_loss` | Control link gone. The response is policy, not physics, so it is configurable: |
| `no_progress` | The aircraft is commanded to a target but is not getting closer. |
| `mission_timeout` | Airborne longer than the flight-time budget allows. |
| `hover_timeout` | Loitering at the destination past the hover budget. |
| `wind` | Wind at or above the airframe's limit - come home while it still can. |

## Configured thresholds

Defaults from `config/default.yaml`. Every one is a setting.

### `altitude`

| Setting | Default |
|---|---|
| `clearance_tolerance_m` | `2.0` |
| `max_altitude_m` | `120.0` |
| `min_altitude_m` | `10.0` |
| `rth_altitude_m` | `80.0` |
| `terrain_clearance_m` | `40.0` |

### `battery`

| Setting | Default |
|---|---|
| `adaptive_estimation` | `True` |
| `arm_minimum_pct` | `0.5` |
| `critical_pct` | `0.2` |
| `cruise_drain_pct_per_km` | `7.0` |
| `emergency_pct` | `0.1` |
| `hover_drain_pct_per_min` | `5.0` |
| `low_pct` | `0.4` |
| `reserve_pct` | `0.15` |

### `behaviour`

| Setting | Default |
|---|---|
| `allow_terminate` | `False` |
| `latch_failsafes` | `True` |
| `require_preflight_pass` | `True` |
| `upload_autopilot_fence` | `True` |
| `write_autopilot_params` | `True` |

### `flight`

| Setting | Default |
|---|---|
| `arrival_radius_m` | `3.0` |
| `cruise_speed_ms` | `12.0` |
| `max_speed_ms` | `15.0` |
| `max_uncommanded_descent_ms` | `2.0` |
| `max_wind_ms` | `10.0` |

### `geofence`

| Setting | Default |
|---|---|
| `enabled` | `True` |
| `lookahead_s` | `10.0` |
| `max_radius_m` | `5000.0` |
| `soft_margin_m` | `500.0` |

### `gps`

| Setting | Default |
|---|---|
| `degraded_timeout_s` | `5.0` |
| `loss_timeout_s` | `3.0` |
| `max_hdop` | `2.5` |
| `min_satellites` | `8` |

### `impact`

| Setting | Default |
|---|---|
| `accel_duration_s` | `0.5` |
| `accel_instant_g` | `6.0` |
| `accel_threshold_g` | `3.0` |
| `enabled` | `True` |
| `freefall_duration_s` | `0.6` |
| `freefall_threshold_g` | `0.3` |
| `max_attitude_deg` | `60.0` |

### `link`

| Setting | Default |
|---|---|
| `blind_timeout_s` | `60.0` |
| `loss_action` | `return` |
| `loss_timeout_s` | `5.0` |
| `telemetry_stale_s` | `2.0` |

### `terrain`

| Setting | Default |
|---|---|
| `cache_path` | `.cache/elevation.json` |
| `enabled` | `True` |
| `provider_url` | `https://api.opentopodata.org/v1/srtm30m` |
| `required` | `False` |
| `samples` | `25` |
| `timeout_s` | `10.0` |

### `timing`

| Setting | Default |
|---|---|
| `max_flight_time_s` | `900.0` |
| `max_hover_time_s` | `300.0` |
| `no_progress_min_closure_m` | `5.0` |
| `no_progress_timeout_s` | `30.0` |
| `tick_interval_s` | `0.2` |

## Two design rules worth stating explicitly

**Unknown telemetry is never nominal.** Every field the aircraft might fail
to report is optional, and the rules treat a missing value as the worst case.
An unreported battery level is a low battery.

**Absent telemetry is itself an alarm.** A monitor that only reacts to data it
receives goes quiet exactly when it is most needed, so the engine is driven by
an external clock rather than by frame arrival, and staleness is its own rule.


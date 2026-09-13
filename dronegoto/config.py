"""Configuration: every safety limit is a setting, and every setting is validated.

Two deliberate choices here:

1. **Unknown keys are errors, not warnings.** A typo like `max_altitiude_m` would
   otherwise silently leave the default in force - the user believes they set a
   ceiling and they have not. In a safety config that failure mode is unacceptable.
2. **Cross-field contradictions are errors.** A `critical` battery threshold above
   the `low` threshold is not a preference, it is a config that cannot behave
   sensibly, so it is rejected at load time rather than mid-flight.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised for malformed, unknown or self-contradictory configuration."""


@dataclass(frozen=True)
class BatteryConfig:
    """Layered battery protection, following ArduPilot/PX4 practice.

    `low_pct` is the user-facing "come home" threshold. `reserve_pct` drives the
    dynamic estimator, which triggers a return as soon as the remaining charge
    would no longer cover the flight home plus this reserve - at distance that
    fires well before any fixed percentage would.
    """

    low_pct: float = 0.40
    critical_pct: float = 0.20
    emergency_pct: float = 0.10
    arm_minimum_pct: float = 0.50
    reserve_pct: float = 0.15
    cruise_drain_pct_per_km: float = 7.0
    hover_drain_pct_per_min: float = 5.0
    adaptive_estimation: bool = True


@dataclass(frozen=True)
class GeofenceConfig:
    """Cylindrical fence centred on home.

    PX4's own documentation notes its fence acts only *after* a breach. Ours adds
    a soft ring and a velocity lookahead so the aircraft turns back before
    crossing the hard boundary.
    """

    max_radius_m: float = 5000.0
    soft_margin_m: float = 500.0
    lookahead_s: float = 10.0
    enabled: bool = True


@dataclass(frozen=True)
class AltitudeConfig:
    max_altitude_m: float = 120.0
    min_altitude_m: float = 10.0
    rth_altitude_m: float = 80.0
    terrain_clearance_m: float = 40.0
    # Real altitude hold wobbles by a metre or two, and the cruise phase begins
    # just below the commanded altitude. Without a tolerance band the clearance
    # rule chatters at exactly the threshold instead of reporting a real problem.
    clearance_tolerance_m: float = 2.0


@dataclass(frozen=True)
class ImpactConfig:
    """Collision and freefall detection from IMU data.

    Two thresholds, because a collision is a sub-tick event. `accel_threshold_g`
    needs confirmation across `accel_duration_s` to reject noise; `accel_instant_g`
    fires on a single frame, because telemetry reports the *peak* acceleration
    since the previous frame and a peak that large is not noise. Without the
    instant path, any impact shorter than two control ticks goes undetected.
    """

    accel_threshold_g: float = 3.0
    accel_instant_g: float = 6.0
    accel_duration_s: float = 0.5
    freefall_threshold_g: float = 0.3
    freefall_duration_s: float = 0.6
    max_attitude_deg: float = 60.0
    enabled: bool = True


@dataclass(frozen=True)
class GpsConfig:
    min_satellites: int = 8
    max_hdop: float = 2.5
    loss_timeout_s: float = 3.0
    degraded_timeout_s: float = 5.0


@dataclass(frozen=True)
class LinkConfig:
    """Behaviour when the control link or the telemetry stream goes away.

    `loss_action` = "continue" is the mode that matches an onboard-executed
    mission: the aircraft finishes the flight without us. It is only safe
    because the autopilot-side geofence and failsafes are uploaded before arming.
    """

    loss_timeout_s: float = 5.0
    loss_action: str = "return"
    telemetry_stale_s: float = 2.0
    blind_timeout_s: float = 60.0
    VALID_ACTIONS = ("continue", "hold", "return", "land")


@dataclass(frozen=True)
class TimingConfig:
    max_flight_time_s: float = 900.0
    max_hover_time_s: float = 300.0
    no_progress_timeout_s: float = 30.0
    no_progress_min_closure_m: float = 5.0
    tick_interval_s: float = 0.2


@dataclass(frozen=True)
class FlightConfig:
    cruise_speed_ms: float = 12.0
    max_speed_ms: float = 15.0
    max_wind_ms: float = 10.0
    max_uncommanded_descent_ms: float = 2.0
    arrival_radius_m: float = 3.0


@dataclass(frozen=True)
class TerrainConfig:
    enabled: bool = True
    provider_url: str = "https://api.opentopodata.org/v1/srtm30m"
    samples: int = 25
    timeout_s: float = 10.0
    cache_path: str = ".cache/elevation.json"
    required: bool = False


@dataclass(frozen=True)
class TricksConfig:
    """Gates for a software-triggered aerobatic maneuver (e.g. ArduPilot FLIP).

    A flip is a deliberate, large, brief attitude excursion - exactly what
    `impact.max_attitude_deg` exists to catch as a collision signal elsewhere in
    this system. These checks run *before* the maneuver is requested, so the
    aircraft never asks for one it cannot recover from cleanly: enough altitude
    to complete it and still clear the ground, enough charge for a max-current
    maneuver, and starting from level, controlled flight rather than out of an
    unrelated upset.
    """

    enabled: bool = True
    min_altitude_m: float = 15.0
    min_battery_pct: float = 0.50
    max_attitude_deviation_deg: float = 20.0
    require_gps_fix: bool = True


@dataclass(frozen=True)
class BehaviourConfig:
    """Policy knobs that are not thresholds."""

    latch_failsafes: bool = True
    allow_terminate: bool = False
    require_preflight_pass: bool = True
    upload_autopilot_fence: bool = True
    write_autopilot_params: bool = True


@dataclass(frozen=True)
class SafetyConfig:
    battery: BatteryConfig = BatteryConfig()
    geofence: GeofenceConfig = GeofenceConfig()
    altitude: AltitudeConfig = AltitudeConfig()
    impact: ImpactConfig = ImpactConfig()
    gps: GpsConfig = GpsConfig()
    link: LinkConfig = LinkConfig()
    timing: TimingConfig = TimingConfig()
    flight: FlightConfig = FlightConfig()
    terrain: TerrainConfig = TerrainConfig()
    tricks: TricksConfig = TricksConfig()
    behaviour: BehaviourConfig = BehaviourConfig()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SafetyConfig:
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError(f"configuration must be a mapping, got {type(data).__name__}")

        known = {f.name: f for f in fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            raise ConfigError(
                f"unknown configuration section(s): {', '.join(sorted(unknown))}. "
                f"Valid sections: {', '.join(sorted(known))}"
            )

        sections: dict[str, Any] = {}
        for name, f in known.items():
            section_data = data.get(name, {})
            if not isinstance(section_data, dict):
                raise ConfigError(f"section '{name}' must be a mapping")
            sections[name] = _build_section(f.type, name, section_data)

        config = cls(**sections)
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | Path) -> SafetyConfig:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            f.name: {sf.name: getattr(getattr(self, f.name), sf.name) for sf in fields(getattr(self, f.name))}
            for f in fields(self)
        }

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self) -> None:
        errors: list[str] = []
        b, g, a = self.battery, self.geofence, self.altitude
        t, fl, gps = self.timing, self.flight, self.gps

        for label, value in (
            ("battery.low_pct", b.low_pct),
            ("battery.critical_pct", b.critical_pct),
            ("battery.emergency_pct", b.emergency_pct),
            ("battery.arm_minimum_pct", b.arm_minimum_pct),
            ("battery.reserve_pct", b.reserve_pct),
        ):
            if not 0.0 < value <= 1.0:
                errors.append(f"{label} must be a fraction in (0, 1], got {value}")

        if not b.emergency_pct < b.critical_pct < b.low_pct:
            errors.append(
                f"battery thresholds must increase emergency < critical < low, got "
                f"{b.emergency_pct} / {b.critical_pct} / {b.low_pct}"
            )
        if b.arm_minimum_pct <= b.low_pct:
            errors.append(
                f"battery.arm_minimum_pct ({b.arm_minimum_pct}) must exceed low_pct "
                f"({b.low_pct}), otherwise the aircraft may trigger a return immediately after arming"
            )

        if g.max_radius_m <= 0:
            errors.append("geofence.max_radius_m must be positive")
        if g.soft_margin_m < 0:
            errors.append("geofence.soft_margin_m must be non-negative")
        if g.soft_margin_m >= g.max_radius_m:
            errors.append(
                f"geofence.soft_margin_m ({g.soft_margin_m}) must be smaller than "
                f"max_radius_m ({g.max_radius_m})"
            )
        if g.lookahead_s < 0:
            errors.append("geofence.lookahead_s must be non-negative")

        if a.max_altitude_m <= a.min_altitude_m:
            errors.append(
                f"altitude.max_altitude_m ({a.max_altitude_m}) must exceed "
                f"min_altitude_m ({a.min_altitude_m})"
            )
        if not a.min_altitude_m <= a.rth_altitude_m <= a.max_altitude_m:
            errors.append(
                f"altitude.rth_altitude_m ({a.rth_altitude_m}) must lie between "
                f"min_altitude_m ({a.min_altitude_m}) and max_altitude_m ({a.max_altitude_m})"
            )
        if a.terrain_clearance_m < 0:
            errors.append("altitude.terrain_clearance_m must be non-negative")
        if a.clearance_tolerance_m < 0:
            errors.append("altitude.clearance_tolerance_m must be non-negative")
        if a.clearance_tolerance_m >= a.terrain_clearance_m:
            errors.append(
                f"altitude.clearance_tolerance_m ({a.clearance_tolerance_m}) must be "
                f"smaller than terrain_clearance_m ({a.terrain_clearance_m}), or the "
                f"clearance rule could never fire"
            )
        if a.terrain_clearance_m >= a.max_altitude_m:
            errors.append(
                f"altitude.terrain_clearance_m ({a.terrain_clearance_m}) is not below "
                f"max_altitude_m ({a.max_altitude_m}); no cruise altitude could satisfy both"
            )

        if fl.cruise_speed_ms <= 0 or fl.max_speed_ms <= 0:
            errors.append("flight speeds must be positive")
        elif fl.cruise_speed_ms > fl.max_speed_ms:
            errors.append(
                f"flight.cruise_speed_ms ({fl.cruise_speed_ms}) exceeds "
                f"max_speed_ms ({fl.max_speed_ms})"
            )
        if fl.arrival_radius_m <= 0:
            errors.append("flight.arrival_radius_m must be positive")
        if fl.max_wind_ms <= 0:
            errors.append("flight.max_wind_ms must be positive")

        if self.link.loss_action not in LinkConfig.VALID_ACTIONS:
            errors.append(
                f"link.loss_action must be one of {LinkConfig.VALID_ACTIONS}, "
                f"got {self.link.loss_action!r}"
            )
        if self.link.telemetry_stale_s <= 0:
            errors.append("link.telemetry_stale_s must be positive")

        if gps.min_satellites < 4:
            errors.append(
                f"gps.min_satellites ({gps.min_satellites}) is below 4; a 3D fix is "
                f"not possible with fewer satellites"
            )
        if gps.max_hdop <= 0:
            errors.append("gps.max_hdop must be positive")

        for label, value in (
            ("timing.max_flight_time_s", t.max_flight_time_s),
            ("timing.max_hover_time_s", t.max_hover_time_s),
            ("timing.no_progress_timeout_s", t.no_progress_timeout_s),
            ("timing.tick_interval_s", t.tick_interval_s),
        ):
            if value <= 0:
                errors.append(f"{label} must be positive")
        if t.max_hover_time_s >= t.max_flight_time_s:
            errors.append(
                f"timing.max_hover_time_s ({t.max_hover_time_s}) must be less than "
                f"max_flight_time_s ({t.max_flight_time_s}), or the hover alone exhausts the flight"
            )

        # Every duration gate must span at least two control ticks, or the
        # condition can never be confirmed and the rule silently never fires.
        for label, window in (
            ("impact.accel_duration_s", self.impact.accel_duration_s),
            ("impact.freefall_duration_s", self.impact.freefall_duration_s),
            ("gps.loss_timeout_s", gps.loss_timeout_s),
            ("gps.degraded_timeout_s", gps.degraded_timeout_s),
            ("link.loss_timeout_s", self.link.loss_timeout_s),
        ):
            if window < 2 * t.tick_interval_s:
                errors.append(
                    f"{label} ({window}) is shorter than two control ticks "
                    f"({2 * t.tick_interval_s}); the condition could never be confirmed "
                    f"and the rule would never fire"
                )

        if self.link.blind_timeout_s <= self.link.telemetry_stale_s:
            errors.append(
                f"link.blind_timeout_s ({self.link.blind_timeout_s}) must exceed "
                f"telemetry_stale_s ({self.link.telemetry_stale_s})"
            )

        if self.impact.accel_instant_g <= self.impact.accel_threshold_g:
            errors.append(
                f"impact.accel_instant_g ({self.impact.accel_instant_g}) must exceed "
                f"accel_threshold_g ({self.impact.accel_threshold_g}); the instant "
                f"threshold is the stricter of the two"
            )
        if self.impact.accel_threshold_g <= 1.0:
            errors.append(
                f"impact.accel_threshold_g ({self.impact.accel_threshold_g}) must exceed 1.0g; "
                f"level flight already reads 1.0g and would trigger constantly"
            )
        if not 0.0 <= self.impact.freefall_threshold_g < 1.0:
            errors.append("impact.freefall_threshold_g must be in [0, 1)")
        if not 0.0 < self.impact.max_attitude_deg <= 180.0:
            errors.append("impact.max_attitude_deg must be in (0, 180]")

        if self.terrain.samples < 2:
            errors.append("terrain.samples must be at least 2 (route endpoints)")

        tr = self.tricks
        if tr.min_altitude_m < 0:
            errors.append("tricks.min_altitude_m must be non-negative")
        if not 0.0 < tr.min_battery_pct <= 1.0:
            errors.append("tricks.min_battery_pct must be a fraction in (0, 1]")
        if not 0.0 < tr.max_attitude_deviation_deg < self.impact.max_attitude_deg:
            errors.append(
                f"tricks.max_attitude_deviation_deg ({tr.max_attitude_deviation_deg}) must be "
                f"positive and below impact.max_attitude_deg ({self.impact.max_attitude_deg}); "
                f"otherwise a maneuver could be permitted to start from an attitude the impact "
                f"rule would itself already be treating as a collision"
            )

        if errors:
            raise ConfigError(
                "invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors)
            )


def _build_section(section_type: Any, name: str, data: dict[str, Any]) -> Any:
    """Instantiate a config section, rejecting unknown keys."""
    # Dataclass field types arrive as strings under `from __future__ import
    # annotations`, so resolve against this module's namespace.
    if isinstance(section_type, str):
        section_type = globals()[section_type]
    if not is_dataclass(section_type):
        raise ConfigError(f"section '{name}' has no schema")

    valid = {f.name for f in fields(section_type)}
    unknown = set(data) - valid
    if unknown:
        raise ConfigError(
            f"unknown key(s) in section '{name}': {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(valid))}"
        )
    return section_type(**data)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


def load_default() -> SafetyConfig:
    """Load the packaged defaults, falling back to code defaults if absent."""
    if DEFAULT_CONFIG_PATH.exists():
        return SafetyConfig.load(DEFAULT_CONFIG_PATH)
    return SafetyConfig.from_dict({})

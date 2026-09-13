"""Mission planning and the go/no-go decision.

This runs before anything arms. It answers the questions that decide whether the
flight is possible at all - can we clear the terrain under the ceiling, is the
target inside the fence, is there enough charge to get there *and back* - and
reports them as blockers rather than discovering them mid-air.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SafetyConfig
from .elevation import TerrainProfile
from .geo import GeoPoint, haversine_m


@dataclass(frozen=True)
class MissionPlan:
    home: GeoPoint
    target: GeoPoint
    distance_m: float
    cruise_altitude_rel_m: float
    cruise_altitude_amsl_m: float | None
    hover_s: float
    eta_outbound_s: float
    eta_total_s: float
    battery_required_pct: float
    battery_available_pct: float | None
    terrain: TerrainProfile | None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def go(self) -> bool:
        return not self.blockers

    @property
    def verdict(self) -> str:
        return "GO" if self.go else "NO-GO"

    @property
    def round_trip_m(self) -> float:
        return self.distance_m * 2.0

    @property
    def battery_margin_pct(self) -> float | None:
        if self.battery_available_pct is None:
            return None
        return self.battery_available_pct - self.battery_required_pct

    def render(self) -> str:
        lines = ["MISSION PREVIEW", ""]
        lines.append(f"  from ............. {self.home}")
        lines.append(f"  to ............... {self.target}")
        lines.append(f"  distance ......... {self.distance_m / 1000:.2f} km out / "
                     f"{self.round_trip_m / 1000:.2f} km round trip")
        lines.append(f"  cruise altitude .. {self.cruise_altitude_rel_m:.0f} m above home"
                     + (f" ({self.cruise_altitude_amsl_m:.0f} m AMSL)"
                        if self.cruise_altitude_amsl_m is not None else ""))
        if self.terrain is not None:
            lines.append(f"  ground peak ...... {self.terrain.max_elevation_m:.0f} m AMSL "
                         f"along route ({len(self.terrain.points)} samples)")
        else:
            lines.append("  ground peak ...... unknown (no elevation data)")
        lines.append(f"  ETA to target .... {_duration(self.eta_outbound_s)}")
        lines.append(f"  hover budget ..... {_duration(self.hover_s)}")
        lines.append(f"  total flight ..... {_duration(self.eta_total_s)}")
        lines.append(f"  battery needed ... {self.battery_required_pct:.0f}% "
                     "(including reserve)")
        if self.battery_available_pct is not None:
            margin = self.battery_margin_pct or 0.0
            lines.append(f"  battery available  {self.battery_available_pct:.0f}% "
                         f"(margin {margin:+.0f}%)")
        lines.append("")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        for blocker in self.blockers:
            lines.append(f"  BLOCKER: {blocker}")
        lines.append("")
        lines.append(f"  VERDICT .......... {self.verdict}")
        return "\n".join(lines)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"


def plan_mission(
    home: GeoPoint,
    target: GeoPoint,
    config: SafetyConfig,
    hover_s: float | None = None,
    requested_altitude_m: float | None = None,
    terrain: TerrainProfile | None = None,
    battery_available: float | None = None,
) -> MissionPlan:
    """Build a plan and decide whether it is flyable."""
    blockers: list[str] = []
    warnings: list[str] = []

    hover_s = config.timing.max_hover_time_s if hover_s is None else hover_s
    distance_m = haversine_m(home, target)

    # --- geofence -------------------------------------------------------
    if config.geofence.enabled:
        soft_radius = config.geofence.max_radius_m - config.geofence.soft_margin_m
        if distance_m >= config.geofence.max_radius_m:
            blockers.append(
                f"target is {distance_m / 1000:.2f} km from home, outside the "
                f"{config.geofence.max_radius_m / 1000:.2f} km geofence"
            )
        elif distance_m >= soft_radius:
            blockers.append(
                f"target is {distance_m / 1000:.2f} km from home, inside the geofence "
                f"margin ({soft_radius / 1000:.2f} km) - the aircraft would turn back "
                f"before reaching it"
            )

    # --- altitude and terrain -------------------------------------------
    altitude_cfg = config.altitude
    home_elevation = terrain.home_elevation_m if terrain is not None else None
    cruise_rel = requested_altitude_m

    if terrain is not None:
        required_amsl = terrain.required_cruise_amsl(altitude_cfg.terrain_clearance_m)
        required_rel = required_amsl - terrain.home_elevation_m
        if cruise_rel is None:
            cruise_rel = max(required_rel, altitude_cfg.min_altitude_m)
        elif cruise_rel < required_rel:
            blockers.append(
                f"requested {cruise_rel:.0f} m gives only "
                f"{cruise_rel - (terrain.max_elevation_m - terrain.home_elevation_m):.0f} m "
                f"over the highest ground on the route; "
                f"{altitude_cfg.terrain_clearance_m:.0f} m is required "
                f"(needs {required_rel:.0f} m)"
            )
        if required_rel > altitude_cfg.max_altitude_m:
            blockers.append(
                f"clearing the {terrain.max_elevation_m:.0f} m ground peak by "
                f"{altitude_cfg.terrain_clearance_m:.0f} m needs {required_rel:.0f} m, "
                f"above the {altitude_cfg.max_altitude_m:.0f} m ceiling"
            )
    else:
        if config.terrain.enabled and config.terrain.required:
            blockers.append(
                "elevation data is unavailable and terrain.required is set"
            )
        elif config.terrain.enabled:
            warnings.append(
                "no elevation data - cruise altitude assumes flat ground, which is "
                "unsafe over rising terrain"
            )
        if cruise_rel is None:
            cruise_rel = min(altitude_cfg.rth_altitude_m, altitude_cfg.max_altitude_m)

    if cruise_rel > altitude_cfg.max_altitude_m:
        blockers.append(
            f"cruise altitude {cruise_rel:.0f} m exceeds the "
            f"{altitude_cfg.max_altitude_m:.0f} m ceiling"
        )
    if cruise_rel < altitude_cfg.min_altitude_m:
        blockers.append(
            f"cruise altitude {cruise_rel:.0f} m is below the "
            f"{altitude_cfg.min_altitude_m:.0f} m floor"
        )

    cruise_amsl = None if home_elevation is None else home_elevation + cruise_rel

    # --- time and energy -------------------------------------------------
    speed = config.flight.cruise_speed_ms
    climb_s = cruise_rel / 3.0
    eta_outbound = climb_s + distance_m / speed
    eta_total = eta_outbound + hover_s + distance_m / speed + cruise_rel / 1.5

    battery_cfg = config.battery
    cruise_cost = (distance_m * 2 / 1000.0) * battery_cfg.cruise_drain_pct_per_km
    hover_cost = (hover_s / 60.0) * battery_cfg.hover_drain_pct_per_min
    vertical_cost = ((climb_s + cruise_rel / 1.5) / 60.0) * battery_cfg.hover_drain_pct_per_min
    required = cruise_cost + hover_cost + vertical_cost + battery_cfg.reserve_pct * 100.0

    available = None if battery_available is None else battery_available * 100.0
    if available is not None:
        if available < battery_cfg.arm_minimum_pct * 100.0:
            blockers.append(
                f"battery {available:.0f}% is below the "
                f"{battery_cfg.arm_minimum_pct * 100:.0f}% minimum for takeoff"
            )
        if available < required:
            blockers.append(
                f"battery {available:.0f}% cannot cover the round trip plus reserve "
                f"({required:.0f}% required)"
            )
        elif available < required + 10.0:
            warnings.append(
                f"battery margin is thin: {available - required:.0f}% over the "
                f"{required:.0f}% required"
            )

    if eta_total > config.timing.max_flight_time_s:
        blockers.append(
            f"planned flight of {_duration(eta_total)} exceeds the "
            f"{_duration(config.timing.max_flight_time_s)} limit"
        )

    return MissionPlan(
        home=home,
        target=target,
        distance_m=distance_m,
        cruise_altitude_rel_m=cruise_rel,
        cruise_altitude_amsl_m=cruise_amsl,
        hover_s=hover_s,
        eta_outbound_s=eta_outbound,
        eta_total_s=eta_total,
        battery_required_pct=required,
        battery_available_pct=available,
        terrain=terrain,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )

"""A kinematic simulator with fault injection.

Not a flight-dynamics model - it does not need to be. Its job is to produce
telemetry realistic enough to exercise every safety rule, and to be able to
break in each of the specific ways the rules exist to catch.

Time is simulated, so a fifteen-minute flight runs in milliseconds and the test
suite can fly hundreds of them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from ..config import SafetyConfig
from ..geo import GeoPoint, bearing_deg, haversine_m, project
from ..telemetry import Telemetry
from .base import BackendCapabilities, BackendError, DroneBackend

FAULT_KINDS = (
    "battery_drain",    # magnitude = drain multiplier
    "impact",           # magnitude = g
    "freefall",
    "gps_degraded",     # magnitude = satellite count
    "gps_loss",
    "position_loss",
    "link_loss",
    "telemetry_freeze",
    "stuck",            # motion inhibited, telemetry otherwise nominal
    "wind",             # magnitude = m/s
    "terrain_rise",     # magnitude = metres of ground rise per km from home
    "attitude_upset",   # magnitude = degrees
)


@dataclass(frozen=True)
class Fault:
    """A failure injected into the flight at a given simulated time."""

    kind: str
    at_s: float = 0.0
    duration_s: float = float("inf")
    magnitude: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {self.kind!r}; valid: {', '.join(FAULT_KINDS)}")

    def active_at(self, t: float) -> bool:
        return self.at_s <= t < self.at_s + self.duration_s


class SimMode(StrEnum):
    IDLE = "idle"
    TAKEOFF = "takeoff"
    GOTO = "goto"
    HOLD = "hold"
    RETURN = "return"
    LAND = "land"
    LANDED = "landed"
    TERMINATED = "terminated"


@dataclass
class SimConfig:
    climb_rate_ms: float = 3.0
    descent_rate_ms: float = 1.5
    home_elevation_m: float = 0.0
    start_battery: float = 1.0
    satellites: float = 16
    hdop: float = 0.8
    ambient_wind_ms: float = 3.0


class SimBackend(DroneBackend):
    """Simulated aircraft.

    The physics is deliberately optimistic - it flies exactly where it is told at
    exactly the commanded speed. That is the right bias for a safety test bed:
    any failsafe that fires here fired because of the injected fault, not because
    the model wandered.
    """

    def __init__(
        self,
        home: GeoPoint,
        config: SafetyConfig,
        sim: SimConfig | None = None,
        faults: list[Fault] | None = None,
        terrain: Callable[[GeoPoint], float] | None = None,
    ) -> None:
        self.config = config
        self.sim = sim or SimConfig()
        self.faults = list(faults or [])
        self._home = home
        self._terrain = terrain

        self._t = 0.0
        self.position = home
        self.altitude_rel_m = 0.0
        self.battery = self.sim.start_battery
        self.mode = SimMode.IDLE
        self.armed = False
        self._target: GeoPoint | None = None
        self._target_altitude_m = 0.0
        self._speed_ms = config.flight.cruise_speed_ms
        self._velocity_ned = (0.0, 0.0, 0.0)
        self._frozen_frame: Telemetry | None = None
        self.command_log: list[tuple[float, str]] = []

    # ------------------------------------------------------------------
    # DroneBackend
    # ------------------------------------------------------------------
    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name="sim",
            supports_geofence_upload=True,
            supports_failsafe_params=True,
            supports_terminate=True,
            supports_onboard_mission=True,
        )

    def now(self) -> float:
        return self._t

    async def connect(self) -> None:
        self._log("connect")

    async def close(self) -> None:
        self._log("close")

    async def home_position(self) -> GeoPoint | None:
        return self._home

    async def arm(self) -> None:
        if self.mode is SimMode.TERMINATED:
            raise BackendError("cannot arm a terminated aircraft")
        self.armed = True
        self._log("arm")

    async def takeoff(self, altitude_m: float) -> None:
        if not self.armed:
            raise BackendError("takeoff requested while disarmed")
        self.mode = SimMode.TAKEOFF
        self._target = self.position
        self._target_altitude_m = altitude_m
        self._log(f"takeoff {altitude_m:.0f}m")

    async def goto(self, target: GeoPoint, altitude_m: float, speed_ms: float) -> None:
        self.mode = SimMode.GOTO
        self._target = target
        self._target_altitude_m = altitude_m
        self._speed_ms = speed_ms
        self._log(f"goto {target} @{altitude_m:.0f}m")

    async def hold(self) -> None:
        self.mode = SimMode.HOLD
        self._log("hold")

    async def return_to_home(self, altitude_m: float) -> None:
        self.mode = SimMode.RETURN
        self._target = self._home
        self._target_altitude_m = altitude_m
        self._log(f"return {altitude_m:.0f}m")

    async def land(self) -> None:
        self.mode = SimMode.LAND
        self._log("land")

    async def terminate(self) -> None:
        self.mode = SimMode.TERMINATED
        self.armed = False
        self._log("terminate")

    async def upload_geofence(self, home: GeoPoint, radius_m: float, max_altitude_m: float) -> None:
        self._log(f"geofence r={radius_m:.0f}m alt={max_altitude_m:.0f}m")

    async def write_failsafe_params(self, params: dict[str, float]) -> None:
        self._log(f"params {sorted(params)}")

    # ------------------------------------------------------------------
    # Physics
    # ------------------------------------------------------------------
    async def step(self, dt: float) -> None:
        if dt <= 0:
            return
        self._t += dt
        if self.mode in (SimMode.IDLE, SimMode.LANDED):
            self._velocity_ned = (0.0, 0.0, 0.0)
            return
        if self.mode is SimMode.TERMINATED:
            # Motors off: it falls.
            self.altitude_rel_m = max(0.0, self.altitude_rel_m - 9.81 * dt)
            self._velocity_ned = (0.0, 0.0, 9.81)
            return

        climb = self._step_vertical(dt)
        north, east = self._step_horizontal(dt)
        self._velocity_ned = (north, east, -climb)
        self._drain_battery(dt)

    def _step_vertical(self, dt: float) -> float:
        if self.mode is SimMode.LAND:
            descent = min(self.sim.descent_rate_ms * dt, self.altitude_rel_m)
            self.altitude_rel_m -= descent
            if self.altitude_rel_m <= 0.01:
                self.altitude_rel_m = 0.0
                self.mode = SimMode.LANDED
                self.armed = False
                self._log("landed")
            return -descent / dt if dt else 0.0

        error = self._target_altitude_m - self.altitude_rel_m
        if abs(error) < 0.01:
            return 0.0
        rate = self.sim.climb_rate_ms if error > 0 else -self.sim.descent_rate_ms
        change = max(-abs(error), min(abs(error), rate * dt))
        self.altitude_rel_m += change
        return change / dt if dt else 0.0

    def _step_horizontal(self, dt: float) -> tuple[float, float]:
        if self.mode in (SimMode.HOLD, SimMode.TAKEOFF, SimMode.LAND) or self._target is None:
            return 0.0, 0.0
        if self._fault_active("stuck"):
            return 0.0, 0.0
        # Do not translate until near the commanded altitude - matches how a real
        # mission climbs out before departing, and keeps terrain clearance honest.
        if self.altitude_rel_m < self._target_altitude_m - 2.0:
            return 0.0, 0.0

        remaining = haversine_m(self.position, self._target)
        if remaining < 0.5:
            if self.mode is SimMode.RETURN:
                self.mode = SimMode.LAND
                self._log("arrived home, landing")
            return 0.0, 0.0

        heading = bearing_deg(self.position, self._target)
        distance = min(self._speed_ms * dt, remaining)
        self.position = project(self.position, heading, distance)
        speed = distance / dt if dt else 0.0
        return speed * math.cos(math.radians(heading)), speed * math.sin(math.radians(heading))

    def _drain_battery(self, dt: float) -> None:
        """Time-based drain with a small penalty for translating.

        A multirotor's power draw is dominated by the cost of staying airborne,
        which is why endurance is quoted in minutes and not kilometres. Charging
        both a per-minute *and* a per-kilometre rate would double-count the same
        energy; the per-kilometre figure in the config is the derived cruise
        equivalent used by the return estimator, not a second physical cost.
        """
        cfg = self.config.battery
        speed = math.hypot(self._velocity_ned[0], self._velocity_ned[1])
        translation_penalty = 1.0 + 0.15 * (speed / max(1e-6, self.config.flight.max_speed_ms))
        drain = cfg.hover_drain_pct_per_min * (dt / 60.0) * translation_penalty
        multiplier = self._fault_magnitude("battery_drain", default=1.0)
        self.battery = max(0.0, self.battery - (drain * multiplier) / 100.0)

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    def ground_elevation(self, point: GeoPoint) -> float:
        if self._terrain is not None:
            return self._terrain(point)
        rise = self._fault_magnitude("terrain_rise", default=0.0)
        if rise:
            return self.sim.home_elevation_m + rise * (haversine_m(self._home, point) / 1000.0)
        return self.sim.home_elevation_m

    async def read_telemetry(self) -> Telemetry:
        if self._fault_active("telemetry_freeze") and self._frozen_frame is not None:
            # Deliberately returns a stale frame: the aircraft has gone quiet and
            # the supervisor must notice on its own.
            return self._frozen_frame

        gps_lost = self._fault_active("gps_loss")
        position_lost = gps_lost or self._fault_active("position_loss")
        satellites = int(self._fault_magnitude("gps_degraded", default=self.sim.satellites))
        degraded = self._fault_active("gps_degraded")

        accel = 1.0
        if self._fault_active("impact"):
            accel = self._fault_magnitude("impact", default=4.0)
        elif self._fault_active("freefall"):
            accel = self._fault_magnitude("freefall", default=0.05)
        elif self.mode is SimMode.TERMINATED:
            accel = 0.0

        upset = self._fault_magnitude("attitude_upset", default=0.0)
        attitude = (upset, 0.0, 0.0) if upset else (1.5, 2.0, 0.0)

        frame = Telemetry(
            timestamp=self._t,
            position=None if position_lost else self.position,
            altitude_rel_m=self.altitude_rel_m,
            altitude_amsl_m=self.sim.home_elevation_m + self.altitude_rel_m,
            velocity_ned_ms=self._velocity_ned,
            battery_remaining=self.battery,
            battery_voltage_v=10.0 + 2.6 * self.battery,
            satellites=0 if gps_lost else satellites,
            hdop=9.9 if gps_lost else (4.0 if degraded else self.sim.hdop),
            has_fix=not gps_lost,
            accel_magnitude_g=accel,
            attitude_deg=attitude,
            wind_speed_ms=self._fault_magnitude("wind", default=self.sim.ambient_wind_ms),
            armed=self.armed,
            in_air=self.altitude_rel_m > 0.5,
            link_ok=not self._fault_active("link_loss"),
        )
        self._frozen_frame = frame
        return frame

    # ------------------------------------------------------------------
    # Faults
    # ------------------------------------------------------------------
    def _fault_active(self, kind: str) -> bool:
        return any(f.kind == kind and f.active_at(self._t) for f in self.faults)

    def _fault_magnitude(self, kind: str, default: float) -> float:
        for fault in self.faults:
            if fault.kind == kind and fault.active_at(self._t):
                return fault.magnitude
        return default

    def inject(self, fault: Fault) -> None:
        self.faults.append(fault)

    def _log(self, message: str) -> None:
        self.command_log.append((round(self._t, 2), message))

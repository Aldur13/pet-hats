"""MAVLink backend for PX4 and ArduPilot, via MAVSDK.

**Navigation deliberately does not use `action.goto_location()`.** Multiple
MAVSDK-Python reports describe it commanding a descent to ground and crashing,
or behaving in SITL in ways it does not on real hardware:

  https://github.com/mavlink/MAVSDK-Python/issues/179
  https://github.com/mavlink/MAVSDK-Python/issues/233
  https://github.com/mavlink/MAVSDK-Python/issues/304

The Mission API is used instead. It is the better-tested path, and it has a
property that matters for this system specifically: an uploaded mission executes
onboard, so the flight survives the loss of the link to this process.

`mavsdk` is imported lazily so the rest of the package - and the whole test
suite - works without it installed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from typing import Any

from ..config import SafetyConfig
from ..geo import GeoPoint
from ..telemetry import Telemetry
from .base import BackendCapabilities, BackendError, DroneBackend

log = logging.getLogger(__name__)

# PX4 parameter names. ArduPilot uses a different set (BATT_LOW_MAH,
# BATT_FS_LOW_ACT, RTL_ALT, FENCE_RADIUS, FENCE_ALT_MAX, FENCE_ENABLE); pass
# `param_map` to override rather than editing this.
PX4_PARAMS = {
    "battery_low": ("BAT_LOW_THR", "float", lambda cfg: cfg.battery.low_pct),
    "battery_critical": ("BAT_CRIT_THR", "float", lambda cfg: cfg.battery.critical_pct),
    "battery_emergency": ("BAT_EMERGEN_THR", "float", lambda cfg: cfg.battery.emergency_pct),
    "rtl_altitude": ("RTL_RETURN_ALT", "float", lambda cfg: cfg.altitude.rth_altitude_m),
    "max_flight_time": ("COM_FLT_TIME_MAX", "float", lambda cfg: cfg.timing.max_flight_time_s),
    "fence_radius": ("GF_MAX_HOR_DIST", "float", lambda cfg: cfg.geofence.max_radius_m),
    "fence_altitude": ("GF_MAX_VER_DIST", "float", lambda cfg: cfg.altitude.max_altitude_m),
    # 2 = Return. PX4's fence acts only once breached, which is why our own
    # predictive fence runs on top of it rather than relying on it.
    "fence_action": ("GF_ACTION", "int", lambda cfg: 2),
}

# Deliberately no BATT_LOW_VOLT / BATT_LOW_MAH entry: our thresholds are
# percentages and ArduPilot's are volts or mAh, which depend on the pack. Writing
# a guess - and in particular writing 0, which *disables* the voltage failsafe -
# would be worse than leaving the pilot's own calibration in place.
# ArduCopter custom flight mode numbers (APM:Copter mode list). Only the ones
# this backend actually uses are named; the rest of ArduPilot's mode set is
# out of scope here.
ARDUCOPTER_MODE_FLIP = 14

# MAV_MODE_FLAG_CUSTOM_MODE_ENABLED - required on base_mode for MAV_CMD_DO_SET_MODE
# to select an ArduPilot custom mode rather than a standard MAVLink base mode.
_MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1

ARDUPILOT_PARAMS = {
    "rtl_altitude": ("RTL_ALT", "float", lambda cfg: cfg.altitude.rth_altitude_m * 100),
    "fence_radius": ("FENCE_RADIUS", "float", lambda cfg: cfg.geofence.max_radius_m),
    "fence_altitude": ("FENCE_ALT_MAX", "float", lambda cfg: cfg.altitude.max_altitude_m),
    "fence_enable": ("FENCE_ENABLE", "int", lambda cfg: 1),
    "battery_fs_low_action": ("BATT_FS_LOW_ACT", "int", lambda cfg: 2),   # RTL
    "battery_fs_crit_action": ("BATT_FS_CRT_ACT", "int", lambda cfg: 1),  # Land
}


def build_mission_items(
    mission_item_cls: Any,
    waypoints: list[tuple[GeoPoint, float]],
    speed_ms: float,
    loiter_s: float = 0.0,
) -> list[Any]:
    """Construct MissionItems, tolerating MAVSDK's field churn across versions.

    The constructor has gained and reordered parameters between releases, so
    rather than pinning a positional signature (which fails loudly on some
    versions and *silently mis-assigns* on others) the supported parameter names
    are read from the class and only those are passed.
    """
    try:
        parameters = list(inspect.signature(mission_item_cls.__init__).parameters)[1:]
    except (TypeError, ValueError):  # pragma: no cover - exotic binding
        parameters = []

    items = []
    for index, (point, altitude_m) in enumerate(waypoints):
        is_last = index == len(waypoints) - 1
        candidate = {
            "latitude_deg": point.lat,
            "longitude_deg": point.lon,
            "relative_altitude_m": float(altitude_m),
            "speed_m_s": float(speed_ms),
            "is_fly_through": not is_last,
            "gimbal_pitch_deg": float("nan"),
            "gimbal_yaw_deg": float("nan"),
            "camera_action": getattr(mission_item_cls, "CameraAction", None)
            and mission_item_cls.CameraAction.NONE,
            "loiter_time_s": float(loiter_s if is_last else 0.0),
            "camera_photo_interval_s": float("nan"),
            "acceptance_radius_m": float("nan"),
            "yaw_deg": float("nan"),
            "camera_photo_distance_m": float("nan"),
            "vehicle_action": getattr(mission_item_cls, "VehicleAction", None)
            and mission_item_cls.VehicleAction.NONE,
        }
        if parameters:
            kwargs = {k: v for k, v in candidate.items() if k in parameters}
            missing = [p for p in parameters if p not in kwargs]
            if missing:
                raise BackendError(
                    f"this MAVSDK build's MissionItem needs parameters this backend does "
                    f"not supply: {', '.join(missing)}. Pin mavsdk>=1.4,<3 or update "
                    f"build_mission_items()."
                )
        else:  # pragma: no cover
            kwargs = candidate
        items.append(mission_item_cls(**kwargs))
    return items


class MavsdkBackend(DroneBackend):
    """A PX4 or ArduPilot aircraft reached over MAVLink."""

    def __init__(
        self,
        config: SafetyConfig,
        system_address: str = "udp://:14540",
        param_map: dict[str, tuple[str, str, Any]] | None = None,
        connect_timeout_s: float = 30.0,
    ) -> None:
        self.config = config
        self.system_address = system_address
        self.param_map = param_map if param_map is not None else PX4_PARAMS
        self.connect_timeout_s = connect_timeout_s
        self._drone: Any = None
        self._mavsdk: Any = None
        self._home: GeoPoint | None = None
        self._tasks: list[asyncio.Task] = []
        self._latest: dict[str, Any] = {}
        self._latest_at: dict[str, float] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name="mavsdk",
            supports_geofence_upload=True,
            supports_failsafe_params=True,
            supports_terminate=True,
            supports_onboard_mission=True,
            # FLIP is ArduCopter-specific; report it honestly rather than
            # advertising a capability that would raise BackendError on PX4.
            supports_flip=self.param_map is ARDUPILOT_PARAMS,
        )

    def now(self) -> float:
        return time.monotonic()

    async def step(self, dt: float) -> None:
        """Pace the control loop against wall time.

        Nothing else in the loop necessarily yields, so without this sleep the
        supervisor would spin flat out, starve the telemetry subscription tasks
        on the same event loop, and see a frozen frame forever.
        """
        await asyncio.sleep(dt)

    # ------------------------------------------------------------------
    async def connect(self) -> None:
        try:
            import mavsdk
        except ImportError as exc:  # pragma: no cover
            raise BackendError(
                "the mavsdk package is required for this backend: pip install 'dronegoto[mavsdk]'"
            ) from exc

        self._mavsdk = mavsdk
        self._drone = mavsdk.System()
        await self._drone.connect(system_address=self.system_address)

        deadline = self.now() + self.connect_timeout_s
        async for state in self._drone.core.connection_state():
            if state.is_connected:
                break
            if self.now() > deadline:
                raise BackendError(f"no vehicle on {self.system_address} after "
                                   f"{self.connect_timeout_s:.0f}s")

        async for health in self._drone.telemetry.health():
            if health.is_global_position_ok and health.is_home_position_ok:
                break
            if self.now() > deadline:
                raise BackendError(
                    "vehicle connected but position estimate never became valid "
                    "(is_global_position_ok / is_home_position_ok)"
                )

        self._start_subscriptions()
        # Let the first frames land before anyone reads telemetry, otherwise a
        # preflight runs against an empty cache and fails for the wrong reason.
        await asyncio.sleep(1.0)

    def _start_subscriptions(self) -> None:
        telemetry = self._drone.telemetry
        streams = {
            "position": telemetry.position(),
            "battery": telemetry.battery(),
            "gps_info": telemetry.gps_info(),
            "attitude": telemetry.attitude_euler(),
            "velocity": telemetry.velocity_ned(),
            "in_air": telemetry.in_air(),
            "armed": telemetry.armed(),
            "home": telemetry.home(),
        }
        for name, stream in streams.items():
            self._tasks.append(asyncio.create_task(self._pump(name, stream)))

    async def _pump(self, name: str, stream: Any) -> None:
        try:
            async for value in stream:
                self._latest[name] = value
                self._latest_at[name] = self.now()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary; any failure ends the stream
            # A dead subscription must not be silent: telemetry simply stops
            # updating, and the staleness rule is what notices.
            log.warning("telemetry stream %r ended: %s", name, exc)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    async def read_telemetry(self) -> Telemetry:
        position = self._latest.get("position")
        battery = self._latest.get("battery")
        gps = self._latest.get("gps_info")
        attitude = self._latest.get("attitude")
        velocity = self._latest.get("velocity")
        home = self._latest.get("home")

        if home is not None and self._home is None:
            self._home = GeoPoint(home.latitude_deg, home.longitude_deg)

        # Timestamp is the oldest of the *continuous* readings, not "now": a frame
        # is only as fresh as its stalest part, and claiming otherwise would hide a
        # dead subscription from the staleness rule. Streams that only publish on
        # change (home, armed, in_air) are excluded, or a perfectly healthy
        # aircraft would look stale a few seconds after takeoff.
        fresh = [self._latest_at[name] for name in FAST_STREAMS if name in self._latest_at]
        timestamp = min(fresh) if fresh else self.now()

        velocity_ned = None
        accel_g = None
        if velocity is not None:
            velocity_ned = (
                velocity.north_m_s, velocity.east_m_s, velocity.down_m_s,
            )
        if attitude is not None:
            attitude_deg = (attitude.roll_deg, attitude.pitch_deg, attitude.yaw_deg)
            # MAVSDK exposes no acceleration stream, so tilt is the only
            # available impact proxy here. The attitude-excursion branch of the
            # impact rule carries the load; a companion computer reading the IMU
            # directly should populate accel_magnitude_g properly.
        else:
            attitude_deg = None

        return Telemetry(
            timestamp=timestamp,
            position=GeoPoint(position.latitude_deg, position.longitude_deg)
            if position is not None else None,
            altitude_rel_m=position.relative_altitude_m if position is not None else None,
            altitude_amsl_m=position.absolute_altitude_m if position is not None else None,
            velocity_ned_ms=velocity_ned,
            battery_remaining=_clamp01(battery.remaining_percent) if battery is not None else None,
            battery_voltage_v=battery.voltage_v if battery is not None else None,
            satellites=gps.num_satellites if gps is not None else None,
            hdop=None,
            has_fix=_has_3d_fix(gps.fix_type) if gps is not None else None,
            accel_magnitude_g=accel_g,
            attitude_deg=attitude_deg,
            wind_speed_ms=None,
            armed=bool(self._latest.get("armed", False)),
            in_air=bool(self._latest.get("in_air", False)),
            link_ok=bool(self._latest_at) and (self.now() - timestamp) < 5.0,
        )

    async def home_position(self) -> GeoPoint | None:
        if self._home is None:
            home = self._latest.get("home")
            if home is not None:
                self._home = GeoPoint(home.latitude_deg, home.longitude_deg)
        return self._home

    # ------------------------------------------------------------------
    async def arm(self) -> None:
        await self._call(self._drone.action.arm(), "arm")

    async def takeoff(self, altitude_m: float) -> None:
        await self._call(
            self._drone.action.set_takeoff_altitude(float(altitude_m)), "set takeoff altitude"
        )
        await self._call(self._drone.action.takeoff(), "takeoff")

    async def goto(self, target: GeoPoint, altitude_m: float, speed_ms: float) -> None:
        """Upload and start a one-waypoint mission.

        Not goto_location - see the module docstring. The mission also continues
        onboard if this process loses the link.
        """
        mission = self._mavsdk.mission
        items = build_mission_items(mission.MissionItem, [(target, altitude_m)], speed_ms)
        plan = mission.MissionPlan(items)
        await self._call(self._drone.mission.set_return_to_launch_after_mission(False),
                         "configure mission end behaviour")
        await self._call(self._drone.mission.upload_mission(plan), "upload mission")
        await self._call(self._drone.mission.start_mission(), "start mission")

    async def hold(self) -> None:
        await self._call(self._drone.action.hold(), "hold")

    async def return_to_home(self, altitude_m: float) -> None:
        await self._call(
            self._drone.action.set_return_to_launch_altitude(float(altitude_m)),
            "set RTL altitude",
        )
        await self._call(self._drone.action.return_to_launch(), "return to launch")

    async def land(self) -> None:
        await self._call(self._drone.action.land(), "land")

    async def terminate(self) -> None:
        await self._call(self._drone.action.terminate(), "terminate")

    async def flip(self) -> None:
        """Trigger ArduPilot's FLIP mode: one automatic flip, then the previous
        flight mode is restored by the autopilot itself.

        Not implemented for PX4 - this is an ArduCopter-specific flight mode
        (`ArduCopter/mode_flip.cpp`), not a MAVLink standard. Callers should not
        call this directly; `dronegoto.tricks.flip()` gates it on altitude and
        battery first, the same way a mission is gated before arming.
        """
        if self.param_map is not ARDUPILOT_PARAMS:
            raise BackendError(
                "flip is an ArduCopter-only flight mode; this backend is configured "
                "for a different autopilot (pass param_map=ARDUPILOT_PARAMS)"
            )
        await self.set_flight_mode(ARDUCOPTER_MODE_FLIP)

    async def set_flight_mode(self, custom_mode: int) -> None:
        """Send MAV_CMD_DO_SET_MODE for an ArduPilot custom flight mode.

        MAVSDK's `action` plugin only exposes a handful of named, cross-vehicle
        modes (arm, land, RTL...) - it has no call for an ArduPilot-specific mode
        like FLIP. Reaching it means dropping to the raw MAVLink command via the
        `mavlink_passthrough` plugin, which not every MAVSDK build exposes; that
        absence is reported as a BackendError rather than silently doing nothing.
        """
        passthrough = getattr(self._drone, "mavlink_passthrough", None)
        if passthrough is None:
            raise BackendError(
                "this MAVSDK build has no mavlink_passthrough plugin, so an "
                "ArduPilot custom flight mode cannot be requested"
            )
        try:
            await passthrough.send_command_long(
                passthrough.get_target_sysid(),
                passthrough.get_target_compid(),
                176,  # MAV_CMD_DO_SET_MODE
                0,
                _MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                float(custom_mode),
                0, 0, 0, 0, 0,
            )
        except Exception as exc:
            raise BackendError(f"set flight mode {custom_mode} failed: {exc}") from exc

    # ------------------------------------------------------------------
    async def upload_geofence(
        self, home: GeoPoint, radius_m: float, max_altitude_m: float
    ) -> None:
        geofence = self._mavsdk.geofence
        circle = geofence.Circle(
            geofence.Point(home.lat, home.lon), float(radius_m), geofence.FenceType.INCLUSION
        )
        await self._call(
            self._drone.geofence.upload_geofence(geofence.GeofenceData([], [circle])),
            "upload geofence",
        )

    async def write_failsafe_params(self, params: dict[str, float]) -> None:
        """Write our limits into the aircraft so they outlive this process.

        `params` from the controller is ignored in favour of the typed map, which
        knows each parameter's units and whether it is an int or a float.
        """
        failures = []
        for name, kind, value_of in self.param_map.values():
            value = value_of(self.config)
            try:
                if kind == "int":
                    await self._drone.param.set_param_int(name, int(value))
                else:
                    await self._drone.param.set_param_float(name, float(value))
            except Exception as exc:  # noqa: BLE001 - collect every failure, then raise once
                failures.append(f"{name}: {exc}")
        if failures:
            raise BackendError(
                "could not write onboard failsafe parameters - the aircraft will not "
                "protect itself if this process dies: " + "; ".join(failures)
            )

    async def _call(self, coro: Any, what: str) -> None:
        try:
            await coro
        except Exception as exc:
            raise BackendError(f"{what} failed: {exc}") from exc


# Streams whose age says something about link health. See read_telemetry().
FAST_STREAMS = ("position", "battery", "gps_info", "attitude", "velocity")

# Fix types that give a usable 3D position, by name so this survives MAVSDK
# reordering the enum. Its FixType is a plain Enum: `fix_type >= 3` raises.
_3D_FIX_NAMES = frozenset({"FIX_3D", "FIX_DGPS", "RTK_FLOAT", "RTK_FIXED"})


def _has_3d_fix(fix_type: Any) -> bool:
    name = getattr(fix_type, "name", None)
    if name is not None:
        return name in _3D_FIX_NAMES
    try:
        return int(fix_type) >= 3
    except (TypeError, ValueError):
        return False


def _clamp01(percent: float | None) -> float | None:
    """MAVSDK reports remaining as 0..1, but firmware occasionally reports >1."""
    if percent is None or math.isnan(percent):
        return None
    return max(0.0, min(1.0, float(percent)))

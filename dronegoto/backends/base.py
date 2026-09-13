"""The interface every aircraft must present.

Deliberately small. Anything a backend cannot honestly implement should raise
rather than silently no-op - a geofence upload that quietly does nothing is
worse than one that fails loudly, because the operator believes they have a
protection they do not have.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from ..geo import GeoPoint
from ..telemetry import Telemetry


class BackendError(RuntimeError):
    """The aircraft refused, or could not honour, a command."""


class FlightCommand(StrEnum):
    TAKEOFF = "takeoff"
    GOTO = "goto"
    HOLD = "hold"
    RETURN = "return"
    LAND = "land"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class BackendCapabilities:
    """What a given aircraft can actually do.

    The mission controller consults this so that a missing capability becomes an
    explicit preflight failure instead of a surprise in the air.
    """

    name: str
    supports_geofence_upload: bool = False
    supports_failsafe_params: bool = False
    supports_terminate: bool = False
    supports_onboard_mission: bool = False
    supports_flip: bool = False


class DroneBackend(ABC):
    """An aircraft, real or simulated."""

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities: ...

    @abstractmethod
    def now(self) -> float:
        """Current time in seconds.

        Supplied by the backend so a simulator can run a 15-minute flight in
        milliseconds while the safety rules see ordinary elapsed time.
        """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def read_telemetry(self) -> Telemetry: ...

    @abstractmethod
    async def home_position(self) -> GeoPoint | None: ...

    @abstractmethod
    async def arm(self) -> None: ...

    @abstractmethod
    async def takeoff(self, altitude_m: float) -> None: ...

    @abstractmethod
    async def goto(self, target: GeoPoint, altitude_m: float, speed_ms: float) -> None: ...

    @abstractmethod
    async def hold(self) -> None: ...

    @abstractmethod
    async def return_to_home(self, altitude_m: float) -> None: ...

    @abstractmethod
    async def land(self) -> None: ...

    @abstractmethod
    async def terminate(self) -> None: ...

    async def upload_geofence(self, home: GeoPoint, radius_m: float, max_altitude_m: float) -> None:
        """Push the fence to the aircraft as a second, independent layer."""
        raise BackendError(f"{self.capabilities.name} cannot store a geofence onboard")

    async def write_failsafe_params(self, params: dict[str, float]) -> None:
        """Push failsafe parameters so the aircraft protects itself if we die."""
        raise BackendError(f"{self.capabilities.name} cannot store failsafe parameters")

    async def flip(self) -> None:
        """Perform one automatic flip and return to the prior flight mode.

        Deliberately not a full flight-control feature: this hands off to the
        autopilot's own flip implementation (ArduPilot's FLIP mode) rather than
        commanding attitude ourselves. Callers must not call this directly -
        `dronegoto.tricks.flip()` is the safety-gated entry point that checks
        altitude and battery first, exactly as `mission.py` gates a launch.
        """
        raise BackendError(f"{self.capabilities.name} cannot perform a flip")

    async def step(self, dt: float) -> None:
        """Advance simulated time. Real aircraft ignore this."""
        return

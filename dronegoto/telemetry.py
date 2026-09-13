"""Telemetry snapshots and history.

Design rule that governs this whole file: **an unknown value is never nominal.**
Every field that the aircraft might fail to report is Optional, and the safety
rules treat `None` as the worst case rather than as "fine". A monitor that only
reacts to data it receives fails silently exactly when things are going wrong.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from .geo import GeoPoint, haversine_m

# Fields whose absence means we cannot reason about safety at all.
CRITICAL_FIELDS = ("position", "altitude_rel_m", "battery_remaining")


@dataclass(frozen=True)
class Telemetry:
    """One instant of aircraft state.

    `timestamp` is a monotonic clock reading in seconds. It is supplied by the
    caller rather than read here so that the safety engine stays pure and
    replayable from a black-box log.
    """

    timestamp: float
    position: GeoPoint | None = None
    altitude_rel_m: float | None = None
    altitude_amsl_m: float | None = None
    velocity_ned_ms: tuple[float, float, float] | None = None
    battery_remaining: float | None = None
    battery_voltage_v: float | None = None
    satellites: int | None = None
    hdop: float | None = None
    has_fix: bool | None = None
    # Peak |acceleration| since the previous frame, not an instantaneous sample.
    # A collision lasts milliseconds; at any realistic telemetry rate an
    # instantaneous reading would simply miss it between frames.
    accel_magnitude_g: float | None = None
    attitude_deg: tuple[float, float, float] | None = None
    wind_speed_ms: float | None = None
    armed: bool = False
    in_air: bool = False
    link_ok: bool | None = None

    def __post_init__(self) -> None:
        if self.battery_remaining is not None and not 0.0 <= self.battery_remaining <= 1.0:
            raise ValueError(
                f"battery_remaining is a fraction in [0, 1], got {self.battery_remaining!r}"
            )

    @property
    def groundspeed_ms(self) -> float | None:
        if self.velocity_ned_ms is None:
            return None
        north, east, _ = self.velocity_ned_ms
        return math.hypot(north, east)

    @property
    def climb_rate_ms(self) -> float | None:
        """Positive is climbing. NED down is positive downward, hence the sign flip."""
        if self.velocity_ned_ms is None:
            return None
        return -self.velocity_ned_ms[2]

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.timestamp)

    def missing_critical(self) -> tuple[str, ...]:
        return tuple(name for name in CRITICAL_FIELDS if getattr(self, name) is None)

    def distance_to(self, point: GeoPoint | None) -> float | None:
        if self.position is None or point is None:
            return None
        return haversine_m(self.position, point)

    def with_(self, **changes: object) -> Telemetry:
        """Return a copy with fields replaced - convenient in tests and the simulator."""
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass
class TelemetryBuffer:
    """Bounded history, for rules that need a time window.

    Impact detection, stall detection and uncommanded-descent detection all ask
    questions about the recent past, so they need more than the latest frame.
    """

    window_s: float = 60.0
    _frames: deque[Telemetry] = field(default_factory=deque)

    def append(self, frame: Telemetry) -> None:
        self._frames.append(frame)
        self._evict(frame.timestamp)

    def _evict(self, now: float) -> None:
        while self._frames and now - self._frames[0].timestamp > self.window_s:
            self._frames.popleft()

    def __len__(self) -> int:
        return len(self._frames)

    def __iter__(self) -> Iterator[Telemetry]:
        return iter(self._frames)

    @property
    def latest(self) -> Telemetry | None:
        return self._frames[-1] if self._frames else None

    def since(self, now: float, seconds: float) -> list[Telemetry]:
        """Frames within the last `seconds`, oldest first."""
        cutoff = now - seconds
        return [f for f in self._frames if f.timestamp >= cutoff]

    def sustained(self, now: float, seconds: float, predicate) -> bool:
        """True when `predicate` holds across a genuinely covered window.

        Two frames spanning 10 ms must not count as evidence of a 300 ms
        condition, or a single noisy sample could trigger a terminal action. But
        the coverage test has to tolerate the sampling rate it actually gets: at
        a 0.2 s tick, a 0.6 s window holds four frames whose span is 0.6 s only
        if float accumulation lands exactly on the boundary - and it does not.
        Allowing one sample gap of slack makes this robust at any telemetry rate
        instead of silently never confirming at some of them.
        """
        frames = self.since(now, seconds)
        if len(frames) < 2:
            return False
        if not all(predicate(f) for f in frames):
            return False
        covered = frames[-1].timestamp - frames[0].timestamp
        sample_gap = covered / (len(frames) - 1)
        return covered >= seconds - sample_gap - 1e-6

    def clear(self) -> None:
        self._frames.clear()

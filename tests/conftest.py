"""Shared fixtures.

`ctx()` builds a nominal, everything-healthy context. Each test then breaks
exactly one thing, which keeps the "does not fire when it shouldn't" half of the
suite honest - if a rule fires on the nominal context, every test catches it.
"""

from __future__ import annotations

import pytest

from dronegoto.config import SafetyConfig
from dronegoto.geo import GeoPoint
from dronegoto.safety import MissionPhase, MissionState, SafetyContext
from dronegoto.telemetry import Telemetry, TelemetryBuffer

HOME = GeoPoint(51.5074, -0.1278)
TARGET = GeoPoint(51.5312, -0.1123)


@pytest.fixture
def config() -> SafetyConfig:
    return SafetyConfig.from_dict({})


def make_telemetry(timestamp: float = 100.0, **overrides) -> Telemetry:
    base = {
        "timestamp": timestamp,
        "position": HOME,
        "altitude_rel_m": 100.0,
        "altitude_amsl_m": 120.0,
        "velocity_ned_ms": (10.0, 0.0, 0.0),
        "battery_remaining": 0.9,
        "battery_voltage_v": 12.4,
        "satellites": 16,
        "hdop": 0.8,
        "has_fix": True,
        "accel_magnitude_g": 1.0,
        "attitude_deg": (2.0, 3.0, 90.0),
        "wind_speed_ms": 3.0,
        "armed": True,
        "in_air": True,
        "link_ok": True,
    }
    base.update(overrides)
    return Telemetry(**base)


def make_state(**overrides) -> MissionState:
    state = MissionState(
        home=HOME,
        target=TARGET,
        phase=MissionPhase.CRUISE,
        started_at=0.0,
        cruise_altitude_m=100.0,
        ground_elevation_here_m=20.0,
        max_ground_elevation_m=40.0,
        battery_at_start=1.0,
        distance_travelled_m=100.0,
        best_distance_m=2000.0,
        best_distance_at=95.0,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def make_context(
    config: SafetyConfig,
    now: float = 100.0,
    telemetry: Telemetry | None = None,
    history: TelemetryBuffer | None = None,
    state: MissionState | None = None,
    **telemetry_overrides,
) -> SafetyContext:
    tel = telemetry if telemetry is not None else make_telemetry(now, **telemetry_overrides)
    if history is None:
        history = TelemetryBuffer()
        history.append(tel)
    return SafetyContext(
        now=now,
        telemetry=tel,
        history=history,
        config=config,
        state=state if state is not None else make_state(),
    )


def filled_history(frames: list[Telemetry]) -> TelemetryBuffer:
    buffer = TelemetryBuffer()
    for frame in frames:
        buffer.append(frame)
    return buffer


def sustained_history(
    end: float, duration: float, step: float = 0.02, **overrides
) -> TelemetryBuffer:
    """History where every frame in the window carries `overrides`."""
    # The last frame must land exactly on `end`, otherwise TelemetryBuffer.sustained
    # sees a window shorter than requested and (correctly) refuses to confirm it.
    count = max(2, round(duration / step) + 1)
    frames = [
        make_telemetry(round(end - (count - 1 - i) * step, 6), **overrides)
        for i in range(count)
    ]
    return filled_history(frames)


@pytest.fixture
def ctx(config):
    def _build(**kwargs) -> SafetyContext:
        return make_context(config, **kwargs)

    return _build

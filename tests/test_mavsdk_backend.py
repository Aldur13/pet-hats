"""MAVSDK backend against a fake `mavsdk`.

The real package is not a test dependency, and the aircraft path had five
defects that only showed up on reading. These tests pin the shapes that broke:
a plain-Enum FixType, streams that only publish on change, a home position
needed before the first telemetry read, and a control loop that must yield.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from types import SimpleNamespace

import pytest

from dronegoto.backends.mavsdk_backend import (
    ARDUPILOT_PARAMS,
    FAST_STREAMS,
    MavsdkBackend,
    _has_3d_fix,
)
from dronegoto.config import SafetyConfig


class FixType(Enum):
    """Mirrors mavsdk.telemetry.FixType: a plain Enum, *not* IntEnum."""

    NO_GPS = 0
    NO_FIX = 1
    FIX_2D = 2
    FIX_3D = 3
    FIX_DGPS = 4
    RTK_FLOAT = 5
    RTK_FIXED = 6


def backend_with_cache(now: float = 1000.0, **ages) -> MavsdkBackend:
    """A backend whose subscription cache has been filled by hand."""
    b = MavsdkBackend(SafetyConfig.from_dict({}))
    b.now = lambda: now  # type: ignore[method-assign]
    b._latest = {
        "position": SimpleNamespace(latitude_deg=51.5, longitude_deg=-0.12,
                                    relative_altitude_m=50.0, absolute_altitude_m=70.0),
        "battery": SimpleNamespace(remaining_percent=0.8, voltage_v=12.1),
        "gps_info": SimpleNamespace(num_satellites=14, fix_type=FixType.FIX_3D),
        "attitude": SimpleNamespace(roll_deg=1.0, pitch_deg=2.0, yaw_deg=90.0),
        "velocity": SimpleNamespace(north_m_s=5.0, east_m_s=0.0, down_m_s=0.0),
        "home": SimpleNamespace(latitude_deg=51.49, longitude_deg=-0.13),
        "armed": True,
        "in_air": True,
    }
    b._latest_at = {name: now - ages.get(name, 0.1) for name in b._latest}
    return b


@pytest.mark.parametrize(
    "fix,expected",
    [(FixType.NO_GPS, False), (FixType.NO_FIX, False), (FixType.FIX_2D, False),
     (FixType.FIX_3D, True), (FixType.FIX_DGPS, True), (FixType.RTK_FLOAT, True),
     (FixType.RTK_FIXED, True), (3, True), (2, False), ("garbage", False)],
)
def test_fix_type_handles_plain_enum_and_ints(fix, expected):
    """`fix_type >= 3` raised TypeError on the real Enum. This must not."""
    assert _has_3d_fix(fix) is expected


async def test_read_telemetry_does_not_raise_on_enum_fix_type():
    frame = await backend_with_cache().read_telemetry()
    assert frame.has_fix is True
    assert frame.satellites == 14
    assert frame.position is not None
    assert frame.hdop is None, "MAVSDK does not report HDOP; must stay None, not fake it"


async def test_home_position_is_available_before_any_telemetry_read():
    """The CLI asks for home right after connect(); it used to be None until
    read_telemetry() had run, so a real aircraft was always refused."""
    b = backend_with_cache()
    home = await b.home_position()
    assert home is not None
    assert home.lat == pytest.approx(51.49)


async def test_staleness_ignores_streams_that_only_publish_on_change():
    """home/armed/in_air arrive on change. A healthy aircraft must not look
    stale because its home position was last sent a minute ago."""
    b = backend_with_cache(home=60.0, armed=45.0, in_air=45.0)
    frame = await b.read_telemetry()
    assert frame.age_s(b.now()) < 1.0
    assert frame.link_ok is True


async def test_staleness_still_notices_a_dead_fast_stream():
    b = backend_with_cache(battery=30.0)
    frame = await b.read_telemetry()
    assert frame.age_s(b.now()) == pytest.approx(30.0)
    assert "battery" in FAST_STREAMS


async def test_step_yields_to_the_event_loop(monkeypatch):
    """Without a real await in the loop the subscription tasks starve."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await MavsdkBackend(SafetyConfig.from_dict({})).step(0.2)
    assert slept == [0.2]


def test_ardupilot_map_never_disables_a_failsafe():
    """Writing BATT_LOW_VOLT=0 turns ArduPilot's voltage failsafe *off*."""
    names = {name for name, _, _ in ARDUPILOT_PARAMS.values()}
    assert "BATT_LOW_VOLT" not in names
    assert "BATT_LOW_MAH" not in names
    config = SafetyConfig.from_dict({})
    for _, kind, value_of in ARDUPILOT_PARAMS.values():
        value = value_of(config)
        assert value != 0 or kind == "int", "no zero-valued float params (a 0 usually means disabled)"

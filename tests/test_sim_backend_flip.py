"""SimBackend.flip() - the mechanics `tricks.py`'s gate calls after it passes.

Physically approximate on purpose (see the docstring on the method itself);
what matters here is that it refuses under the same conditions a real
autopilot would, and that the attitude effect it reports actually starts and
ends.
"""

from __future__ import annotations

import pytest

from dronegoto.backends.base import BackendError
from dronegoto.backends.sim import SimBackend
from dronegoto.config import SafetyConfig
from dronegoto.geo import GeoPoint

HOME = GeoPoint(51.5074, -0.1278)


@pytest.fixture
def config() -> SafetyConfig:
    return SafetyConfig.from_dict({})


async def climb(backend: SimBackend, altitude_m: float = 50.0) -> None:
    await backend.connect()
    await backend.arm()
    await backend.takeoff(altitude_m)
    for _ in range(2000):
        await backend.step(0.2)
        if backend.altitude_rel_m >= altitude_m - 0.1:
            return
    raise AssertionError("did not reach altitude")


async def test_capability_is_advertised(config):
    backend = SimBackend(HOME, config)
    assert backend.capabilities.supports_flip is True


async def test_refuses_on_the_ground(config):
    backend = SimBackend(HOME, config)
    await backend.connect()
    with pytest.raises(BackendError, match="disarmed or on the ground"):
        await backend.flip()


async def test_refuses_while_armed_but_not_airborne(config):
    backend = SimBackend(HOME, config)
    await backend.connect()
    await backend.arm()
    with pytest.raises(BackendError):
        await backend.flip()


async def test_flip_is_logged(config):
    backend = SimBackend(HOME, config)
    await climb(backend)
    await backend.flip()
    assert any(cmd == "flip" for _, cmd in backend.command_log)


async def test_attitude_spikes_during_the_flip_and_recovers(config):
    backend = SimBackend(HOME, config)
    await climb(backend)

    level = await backend.read_telemetry()
    assert abs(level.attitude_deg[0]) < 10.0, "should be level before flipping"

    await backend.flip()
    mid_flip = await backend.read_telemetry()
    assert abs(mid_flip.attitude_deg[0]) > 90.0, "should show a large excursion mid-flip"

    await backend.step(backend._flip_duration_s + 0.5)
    recovered = await backend.read_telemetry()
    assert abs(recovered.attitude_deg[0]) < 10.0, "should self-recover after the flip window"


async def test_flip_does_not_affect_position_or_battery_bookkeeping(config):
    """The simulated flip is an attitude effect only - it must not perturb the
    physics the rest of the suite depends on (position, battery drain)."""
    backend = SimBackend(HOME, config)
    await climb(backend)
    battery_before = backend.battery
    position_before = backend.position

    await backend.flip()
    await backend.step(backend._flip_duration_s + 0.5)

    assert backend.position == position_before
    assert backend.battery == pytest.approx(battery_before, abs=0.01)

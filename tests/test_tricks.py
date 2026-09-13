"""The flip gate: fires when it should, and - just as important - refuses for
every one of its own stated reasons.

Uses a minimal fake backend rather than `SimBackend` so each check can be
exercised in isolation against a crafted `Telemetry` frame, the same pattern
`tests/conftest.py` uses for the safety rules themselves.
"""

from __future__ import annotations

import pytest

from dronegoto import tricks
from dronegoto.backends.base import BackendCapabilities, BackendError, DroneBackend
from dronegoto.config import SafetyConfig
from dronegoto.telemetry import Telemetry
from tests.conftest import HOME, make_telemetry


class FakeBackend(DroneBackend):
    """Returns a fixed telemetry frame and records whether flip() was called."""

    def __init__(self, telemetry: Telemetry, supports_flip: bool = True,
                flip_raises: BackendError | None = None) -> None:
        self.telemetry = telemetry
        self._supports_flip = supports_flip
        self._flip_raises = flip_raises
        self.flip_called = False

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(name="fake", supports_flip=self._supports_flip)

    def now(self) -> float:
        return self.telemetry.timestamp

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def read_telemetry(self) -> Telemetry:
        return self.telemetry

    async def home_position(self):
        return HOME

    async def arm(self) -> None: ...
    async def takeoff(self, altitude_m: float) -> None: ...
    async def goto(self, target, altitude_m: float, speed_ms: float) -> None: ...
    async def hold(self) -> None: ...
    async def return_to_home(self, altitude_m: float) -> None: ...
    async def land(self) -> None: ...
    async def terminate(self) -> None: ...

    async def flip(self) -> None:
        if self._flip_raises is not None:
            raise self._flip_raises
        self.flip_called = True


def airborne_telemetry(**overrides) -> Telemetry:
    """A frame that should pass every check by default; tests break one thing."""
    base = {
        "armed": True, "in_air": True, "altitude_rel_m": 50.0, "battery_remaining": 0.9,
        "has_fix": True, "attitude_deg": (2.0, -3.0, 90.0), "wind_speed_ms": 3.0,
    }
    base.update(overrides)
    return make_telemetry(**base)


@pytest.fixture
def config() -> SafetyConfig:
    return SafetyConfig.from_dict({})


# ---------------------------------------------------------------------------
# The nominal case
# ---------------------------------------------------------------------------

async def test_nominal_telemetry_passes_every_check_and_flips(config):
    backend = FakeBackend(airborne_telemetry())
    report = await tricks.flip(backend, config)
    assert report.passed
    assert report.performed
    assert backend.flip_called
    assert report.failures == ()


# ---------------------------------------------------------------------------
# Each gate, refusing for its own stated reason
# ---------------------------------------------------------------------------

async def test_refuses_on_the_ground(config):
    backend = FakeBackend(airborne_telemetry(armed=False, in_air=False))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert not backend.flip_called
    assert "armed and airborne" in [c.name for c in report.failures]


async def test_refuses_below_minimum_altitude(config):
    backend = FakeBackend(airborne_telemetry(altitude_rel_m=5.0))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "altitude" in [c.name for c in report.failures]
    check = next(c for c in report.checks if c.name == "altitude")
    assert "5m" in check.detail and "15m" in check.detail


async def test_unknown_altitude_fails_closed(config):
    """Consistent with the rest of the system: an unknown value is never
    treated as acceptable."""
    backend = FakeBackend(airborne_telemetry(altitude_rel_m=None))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "altitude" in [c.name for c in report.failures]


async def test_refuses_on_low_battery(config):
    backend = FakeBackend(airborne_telemetry(battery_remaining=0.3))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "battery" in [c.name for c in report.failures]


async def test_unknown_battery_fails_closed(config):
    backend = FakeBackend(airborne_telemetry(battery_remaining=None))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "battery" in [c.name for c in report.failures]


async def test_refuses_without_gps_fix_by_default(config):
    backend = FakeBackend(airborne_telemetry(has_fix=False))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "gps fix" in [c.name for c in report.failures]


async def test_gps_check_can_be_disabled(config):
    lenient = SafetyConfig.from_dict({"tricks": {"require_gps_fix": False}})
    backend = FakeBackend(airborne_telemetry(has_fix=False))
    report = await tricks.flip(backend, lenient)
    assert report.performed
    assert "gps fix" not in [c.name for c in report.checks]


async def test_refuses_when_not_level(config):
    backend = FakeBackend(airborne_telemetry(attitude_deg=(35.0, 0.0, 90.0)))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "level attitude" in [c.name for c in report.failures]


async def test_unknown_attitude_fails_closed(config):
    backend = FakeBackend(airborne_telemetry(attitude_deg=None))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "level attitude" in [c.name for c in report.failures]


async def test_refuses_in_high_wind(config):
    backend = FakeBackend(airborne_telemetry(wind_speed_ms=15.0))
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert "wind" in [c.name for c in report.failures]


async def test_unreported_wind_does_not_block(config):
    """Wind is reported by very few backends; absence is not treated the same
    as a known-bad reading, unlike altitude/battery/attitude."""
    backend = FakeBackend(airborne_telemetry(wind_speed_ms=None))
    report = await tricks.flip(backend, config)
    assert report.performed


async def test_disabled_in_config_refuses_immediately_without_reading_telemetry(config):
    disabled = SafetyConfig.from_dict({"tricks": {"enabled": False}})
    backend = FakeBackend(airborne_telemetry())
    report = await tricks.flip(backend, disabled)
    assert not report.performed
    assert not backend.flip_called
    assert len(report.checks) == 1
    assert report.checks[0].name == "tricks enabled"


async def test_backend_without_flip_support_is_refused(config):
    backend = FakeBackend(airborne_telemetry(), supports_flip=False)
    report = await tricks.flip(backend, config)
    assert not report.performed
    assert not backend.flip_called
    assert "backend support" in [c.name for c in report.failures]


async def test_backend_refusal_after_the_gate_passes_is_reported_not_raised(config):
    """Every gate check can pass and the aircraft can still refuse the command
    (e.g. mode rejected in flight) - that must come back as a report, not an
    unhandled exception."""
    backend = FakeBackend(airborne_telemetry(), flip_raises=BackendError("mode change rejected"))
    report = await tricks.flip(backend, config)
    assert report.passed
    assert not report.performed
    assert report.error == "mode change rejected"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_render_shows_ok_and_fail_and_a_verdict(config):
    checks = tricks._check_telemetry(airborne_telemetry(battery_remaining=0.1), config)
    report = tricks.FlipReport(checks=tuple(checks), performed=False)
    text = report.render()
    assert "FAIL" in text and "battery" in text
    assert "refused" in text


async def test_blackbox_records_every_attempt(tmp_path, config):
    from dronegoto.blackbox import BlackBox, replay

    path = tmp_path / "flip.jsonl"
    with BlackBox(path) as blackbox:
        await tricks.flip(FakeBackend(airborne_telemetry()), config, blackbox=blackbox)
        await tricks.flip(FakeBackend(airborne_telemetry(battery_remaining=0.1)), config,
                          blackbox=blackbox)

    records = [r for r in replay(path) if r["kind"] == "flip"]
    assert len(records) == 2
    assert records[0]["performed"] is True
    assert records[1]["performed"] is False
    assert any(c["name"] == "battery" for c in records[1]["checks"])

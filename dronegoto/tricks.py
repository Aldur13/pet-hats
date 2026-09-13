"""Software-triggered aerobatic maneuvers - the "click flip" button.

This is deliberately thin. It does not implement a flip: ArduPilot's own FLIP
flight mode (`ArduCopter/mode_flip.cpp`) does the actual attitude control, and
this module only decides whether it is safe to *ask* for one, then asks.

The gating follows the same shape as `mission.py`'s preflight report - a list
of named checks, all of which must pass, rendered the same way - because the
question being answered is the same kind of question: not "can the command be
sent" but "should it be", given what the aircraft is doing right now.

**Scope note.** This module is a standalone, self-contained gate: it reads
current telemetry itself and does not assume a `MissionController` is running.
That is deliberate - "click flip" is meant to work during ordinary hand-flying,
when no autonomous mission (and no live `SafetyEngine`) is active. If a mission
*is* running in the same process when a flip is requested, be aware that the
mission's own `rule_impact` will see the flip's large, brief attitude
excursion and - correctly, from its own point of view - treat it as a possible
collision, triggering a RETURN. Reconciling that (a "maneuver in progress,
ignore attitude-based impact detection" window on `MissionState`) is future
work if a unified manual+autonomous supervisor is built; today, flipping while
a mission is actively supervising the same aircraft is not a supported
combination and will likely abort the mission.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backends.base import BackendError, DroneBackend
from .blackbox import BlackBox
from .config import SafetyConfig
from .mission import Check
from .telemetry import Telemetry


@dataclass(frozen=True)
class FlipReport:
    """The gate's verdict: which checks passed, and whether the maneuver ran."""

    checks: tuple[Check, ...]
    performed: bool = False
    error: str | None = None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def render(self) -> str:
        lines = ["FLIP", ""]
        lines.extend(check.render() for check in self.checks)
        lines.append("")
        if self.error:
            lines.append(f"  aircraft refused the command: {self.error}")
        elif self.performed:
            lines.append("  flip commanded.")
        else:
            lines.append(
                f"  refused - {len(self.failures)} check(s) failed: "
                + ", ".join(c.name for c in self.failures)
            )
        return "\n".join(lines)


def _check_telemetry(telemetry: Telemetry, config: SafetyConfig) -> list[Check]:
    cfg = config.tricks
    checks: list[Check] = []

    checks.append(Check("armed and airborne", telemetry.armed and telemetry.in_air,
                        f"armed={telemetry.armed}, in_air={telemetry.in_air}"))

    altitude = telemetry.altitude_rel_m
    altitude_ok = altitude is not None and altitude >= cfg.min_altitude_m
    altitude_text = f"{altitude:.0f}m" if altitude is not None else "unknown"
    checks.append(
        Check("altitude", altitude_ok,
              f"{altitude_text} (need >= {cfg.min_altitude_m:.0f}m)")
    )

    battery = telemetry.battery_remaining
    battery_ok = battery is not None and battery >= cfg.min_battery_pct
    checks.append(
        Check("battery", battery_ok,
              (f"{battery * 100:.0f}%" if battery is not None else "unknown")
              + f" (need >= {cfg.min_battery_pct * 100:.0f}%)")
    )

    if cfg.require_gps_fix:
        gps_ok = telemetry.has_fix is True
        checks.append(Check("gps fix", gps_ok, f"has_fix={telemetry.has_fix}"))

    attitude = telemetry.attitude_deg
    level_ok = (
        attitude is not None
        and abs(attitude[0]) <= cfg.max_attitude_deviation_deg
        and abs(attitude[1]) <= cfg.max_attitude_deviation_deg
    )
    attitude_text = (
        f"roll={attitude[0]:.0f}, pitch={attitude[1]:.0f}"
        if attitude is not None else "unknown"
    )
    checks.append(
        Check("level attitude", level_ok,
              f"{attitude_text} (need <= {cfg.max_attitude_deviation_deg:.0f} deg)")
    )

    wind = telemetry.wind_speed_ms
    wind_ok = wind is None or wind <= config.flight.max_wind_ms
    checks.append(
        Check("wind", wind_ok,
              f"{wind:.1f} m/s" if wind is not None else "unreported")
    )

    return checks


async def flip(
    backend: DroneBackend, config: SafetyConfig, blackbox: BlackBox | None = None
) -> FlipReport:
    """Gate, then request, one automatic flip.

    Reads live telemetry itself; the caller only needs a connected backend.
    """
    if not config.tricks.enabled:
        report = FlipReport(checks=(Check("tricks enabled", False, "disabled in config"),))
    elif not backend.capabilities.supports_flip:
        report = FlipReport(
            checks=(Check("backend support", False,
                          f"{backend.capabilities.name} does not support flip"),)
        )
    else:
        telemetry = await backend.read_telemetry()
        checks = _check_telemetry(telemetry, config)
        if all(c.passed for c in checks):
            try:
                await backend.flip()
                report = FlipReport(checks=tuple(checks), performed=True)
            except BackendError as exc:
                report = FlipReport(checks=tuple(checks), performed=False, error=str(exc))
        else:
            report = FlipReport(checks=tuple(checks), performed=False)

    if blackbox is not None:
        blackbox.write(
            "flip",
            performed=report.performed,
            passed=report.passed,
            error=report.error,
            checks=[{"name": c.name, "passed": c.passed, "detail": c.detail} for c in report.checks],
        )
    return report

"""Live terminal display.

No dependencies and no curses: it redraws a fixed block in place with ANSI
cursor movement, and degrades to plain periodic lines when stdout is not a
terminal (a log file, a CI run, a pipe) so it never emits escape-code noise
into somewhere it will be read later.
"""

from __future__ import annotations

import sys
from typing import TextIO

from .safety import MissionState, SafetyVerdict, Severity
from .telemetry import Telemetry

_WIDTH = 42
_SEVERITY_MARK = {
    Severity.NONE: "nominal",
    Severity.WARN: "WARNING",
    Severity.HOLD: "HOLDING",
    Severity.RETURN: "RETURNING",
    Severity.LAND: "LANDING",
    Severity.TERMINATE: "TERMINATED",
}


def _bar(fraction: float | None, width: int = 4) -> str:
    if fraction is None:
        return "?" * width
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "." * (width - filled)


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


class Dashboard:
    """Renders telemetry and safety state while a mission runs."""

    def __init__(self, stream: TextIO | None = None, enabled: bool = True) -> None:
        self.stream = stream or sys.stdout
        self.enabled = enabled
        self.interactive = enabled and self.stream.isatty()
        self._lines_drawn = 0
        self._last_plain_t = -1e9
        self._last_event: tuple[int, frozenset[str]] | None = None

    def update(self, telemetry: Telemetry, state: MissionState, verdict: SafetyVerdict) -> None:
        if not self.enabled:
            return
        if self.interactive:
            self._draw_block(telemetry, state, verdict)
        else:
            self._draw_plain(telemetry, state, verdict)

    # ------------------------------------------------------------------
    def _rows(
        self, telemetry: Telemetry, state: MissionState, verdict: SafetyVerdict
    ) -> list[str]:
        inner = _WIDTH - 2
        altitude = telemetry.altitude_rel_m
        battery = telemetry.battery_remaining
        speed = telemetry.groundspeed_ms
        to_target = telemetry.distance_to(state.target)
        to_home = telemetry.distance_to(state.home)
        elapsed = state.elapsed(telemetry.timestamp)

        def row(content: str) -> str:
            return "|" + content[:inner].ljust(inner) + "|"

        rows = ["+" + f" DRONEGOTO {state.phase.value.upper()} ".center(inner, "-") + "+"]
        rows.append(row(f" alt  {_fmt(altitude, 'm', 8)}   batt {_fmt_pct(battery)} {_bar(battery)}"))
        rows.append(row(f" dist {_fmt_km(to_target)}   sats {_fmt(telemetry.satellites, '', 4)}"))
        rows.append(row(f" home {_fmt_km(to_home)}   spd  {_fmt(speed, 'm/s', 8)}"))

        state_text = _SEVERITY_MARK[verdict.severity]
        if verdict.primary is not None:
            state_text = f"{state_text}: {verdict.primary.rule}"
        clock = f"T+{_clock(elapsed)}"
        room = inner - len(clock) - 2
        rows.append(row(f" {state_text[:room].ljust(room)} {clock}"))
        rows.append("+" + "-" * inner + "+")
        return rows

    def _draw_block(
        self, telemetry: Telemetry, state: MissionState, verdict: SafetyVerdict
    ) -> None:
        rows = self._rows(telemetry, state, verdict)
        if self._lines_drawn:
            self.stream.write(f"\033[{self._lines_drawn}A")
        for row in rows:
            self.stream.write("\033[2K" + row + "\n")
        self.stream.flush()
        self._lines_drawn = len(rows)

    def _draw_plain(
        self, telemetry: Telemetry, state: MissionState, verdict: SafetyVerdict
    ) -> None:
        # One line every two seconds of flight time, plus each safety event as it
        # *changes*. A latched RETURN is still true on every subsequent tick, so
        # reprinting it would bury the rest of the flight under one repeated line.
        event = (int(verdict.severity), frozenset(t.rule for t in verdict.triggers))
        interesting = verdict.severity > Severity.NONE and event != self._last_event
        if verdict.severity > Severity.NONE:
            self._last_event = event
        if not interesting and telemetry.timestamp - self._last_plain_t < 2.0:
            return
        self._last_plain_t = telemetry.timestamp
        parts = [
            f"T+{_clock(state.elapsed(telemetry.timestamp))}",
            f"{state.phase.value:<8}",
            f"alt={_fmt(telemetry.altitude_rel_m, 'm', 0).strip()}",
            f"batt={_fmt_pct(telemetry.battery_remaining).strip()}",
        ]
        to_home = telemetry.distance_to(state.home)
        if to_home is not None:
            parts.append(f"home={to_home:.0f}m")
        if interesting and verdict.primary is not None:
            parts.append(f"<< {verdict.primary}")
        self.stream.write("  ".join(parts) + "\n")
        self.stream.flush()

    def close(self) -> None:
        if self.enabled and self.interactive and self._lines_drawn:
            self.stream.write("\n")
            self.stream.flush()
        self._lines_drawn = 0


def _fmt(value: float | int | None, unit: str, width: int) -> str:
    if value is None:
        return f"{'--' + unit:>{width}}"
    text = f"{value:.1f}{unit}" if isinstance(value, float) else f"{value}{unit}"
    return f"{text:>{width}}"


def _fmt_pct(fraction: float | None) -> str:
    return "  --%" if fraction is None else f"{fraction * 100:4.0f}%"


def _fmt_km(metres: float | None) -> str:
    if metres is None:
        return "    --  "
    if metres < 1000:
        return f"{metres:6.0f}m"
    return f"{metres / 1000:5.2f}km"

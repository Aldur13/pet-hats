"""Flight recorder.

Writes every telemetry frame and every safety decision - with the numbers that
caused it - as JSON lines. When something goes wrong the question is always
"what did it see, and why did it do that", and a log of actions without the
inputs that produced them cannot answer it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from .safety import SafetyVerdict, Severity
from .telemetry import Telemetry


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Severity):
        return value.label
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if hasattr(value, "lat") and hasattr(value, "lon"):
        return {"lat": value.lat, "lon": value.lon}
    if is_dataclass(value):
        return _encode(asdict(value))
    return str(value)


class BlackBox:
    """Append-only JSONL recorder."""

    def __init__(self, path: str | Path, flush_every: int = 20) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self._flush_every = flush_every
        self._since_flush = 0
        self.write("session", started_utc=datetime.now(UTC).isoformat())

    def write(self, kind: str, **payload: Any) -> None:
        record = {"kind": kind, "wall": round(time.time(), 3), **_encode(payload)}
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._since_flush += 1
        if self._since_flush >= self._flush_every:
            self.flush()

    def telemetry(self, frame: Telemetry, phase: str) -> None:
        self.write(
            "telemetry",
            t=round(frame.timestamp, 3),
            phase=phase,
            lat=frame.position.lat if frame.position else None,
            lon=frame.position.lon if frame.position else None,
            alt_rel=frame.altitude_rel_m,
            alt_amsl=frame.altitude_amsl_m,
            batt=frame.battery_remaining,
            sats=frame.satellites,
            hdop=frame.hdop,
            accel_g=frame.accel_magnitude_g,
            groundspeed=frame.groundspeed_ms,
            climb=frame.climb_rate_ms,
            wind=frame.wind_speed_ms,
            link=frame.link_ok,
            in_air=frame.in_air,
        )

    def verdict(self, t: float, verdict: SafetyVerdict) -> None:
        """Only written when something fired - a clean tick is the telemetry line."""
        if verdict.severity is Severity.NONE:
            return
        self.write(
            "verdict",
            t=round(t, 3),
            severity=verdict.severity.label,
            latched=verdict.latched,
            triggers=[
                {
                    "rule": trigger.rule,
                    "severity": trigger.severity.label,
                    "reason": trigger.reason,
                    "detail": trigger.detail,
                }
                for trigger in verdict.triggers
            ],
        )

    def command(self, t: float, command: str, detail: str = "") -> None:
        self.write("command", t=round(t, 3), command=command, detail=detail)

    def flush(self) -> None:
        self._file.flush()
        self._since_flush = 0

    def close(self) -> None:
        if not self._file.closed:
            self.flush()
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def replay(path: str | Path) -> Iterator[dict[str, Any]]:
    """Read a black-box file back, skipping any truncated final line.

    A power loss mid-write leaves a partial record; the rest of the flight is
    still worth reading.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def summarise(path: str | Path) -> str:
    """Human-readable digest of a recorded flight."""
    frames = 0
    verdicts: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    first_t: float | None = None
    last_t: float | None = None
    min_batt: float | None = None

    for record in replay(path):
        kind = record.get("kind")
        if kind == "telemetry":
            frames += 1
            t = record.get("t")
            if t is not None:
                first_t = t if first_t is None else first_t
                last_t = t
            batt = record.get("batt")
            if batt is not None:
                min_batt = batt if min_batt is None else min(min_batt, batt)
        elif kind == "verdict":
            verdicts.append(record)
        elif kind == "command":
            commands.append(record)

    lines = [f"FLIGHT REPLAY  {path}", ""]
    duration = (last_t - first_t) if (first_t is not None and last_t is not None) else 0.0
    lines.append(f"  frames ........... {frames}")
    lines.append(f"  duration ......... {duration:.1f}s")
    if min_batt is not None:
        lines.append(f"  lowest battery ... {min_batt * 100:.0f}%")
    lines.append("")
    lines.append("  commands:")
    for command in commands:
        lines.append(f"    t+{command.get('t', 0):7.1f}s  {command.get('command')}"
                     + (f"  {command['detail']}" if command.get("detail") else ""))
    if verdicts:
        lines.append("")
        lines.append("  safety decisions:")
        seen: set[tuple[str, str]] = set()
        for verdict in verdicts:
            for trigger in verdict.get("triggers", []):
                key = (trigger.get("rule", ""), verdict.get("severity", ""))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"    t+{verdict.get('t', 0):7.1f}s  [{verdict.get('severity')}] "
                    f"{trigger.get('rule')}: {trigger.get('reason')}"
                )
    return "\n".join(lines)

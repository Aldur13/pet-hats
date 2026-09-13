#!/usr/bin/env python3
"""Generate docs/SAFETY.md from the code.

The rule table and the documented thresholds are read out of the source rather
than maintained alongside it, so the documentation cannot drift away from what
the aircraft will actually do.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dronegoto.config import SafetyConfig
from dronegoto.safety import RULES, Severity


def generate() -> str:
    config = SafetyConfig.from_dict({})
    lines = [
        "# Safety rules",
        "",
        "Generated from the code by `python3 tools/gen_safety_docs.py`.",
        "Do not edit by hand.",
        "",
        f"{len(RULES)} rule functions are evaluated on **every** tick. They emit named",
        "triggers, and the arbiter takes the most severe action across all of them —",
        "never first-match, so rule ordering can never hide an emergency.",
        "",
        "## Severity ladder",
        "",
        "```",
        " < ".join(s.label for s in Severity),
        "```",
        "",
        "`RETURN` and above **latch**: once the mission is abandoned, a recovering",
        "sensor reading cannot talk the aircraft into continuing. `WARN` and `HOLD`",
        "do not latch, because transient conditions should not ground an aircraft.",
        "",
        "`TERMINATE` is downgraded to `LAND` unless `behaviour.allow_terminate` is set.",
        "Cutting the motors is the one action guaranteed to destroy the aircraft.",
        "",
        "## Rules",
        "",
        "| Rule function | What it catches |",
        "|---|---|",
    ]
    for rule in RULES:
        doc = (rule.__doc__ or "(undocumented)").strip().splitlines()[0]
        lines.append(f"| `{rule.__name__.removeprefix('rule_')}` | {doc} |")

    lines += [
        "",
        "## Configured thresholds",
        "",
        "Defaults from `config/default.yaml`. Every one is a setting.",
        "",
    ]
    data = config.to_dict()
    for section in sorted(data):
        lines += [f"### `{section}`", "", "| Setting | Default |", "|---|---|"]
        for key in sorted(data[section]):
            lines.append(f"| `{key}` | `{data[section][key]}` |")
        lines.append("")

    lines += [
        "## Two design rules worth stating explicitly",
        "",
        "**Unknown telemetry is never nominal.** Every field the aircraft might fail",
        "to report is optional, and the rules treat a missing value as the worst case.",
        "An unreported battery level is a low battery.",
        "",
        "**Absent telemetry is itself an alarm.** A monitor that only reacts to data it",
        "receives goes quiet exactly when it is most needed, so the engine is driven by",
        "an external clock rather than by frame arrival, and staleness is its own rule.",
        "",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    target = pathlib.Path(__file__).resolve().parent.parent / "docs" / "SAFETY.md"
    target.write_text(generate())
    print(f"wrote {target}")

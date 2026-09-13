"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import tricks
from .backends.base import BackendError, DroneBackend
from .backends.sim import Fault, SimBackend, SimConfig
from .blackbox import BlackBox, summarise
from .config import ConfigError, SafetyConfig, load_default
from .dashboard import Dashboard
from .elevation import FlatTerrain, TerrainProfile, profile_route
from .geo import CoordinateError, GeoPoint, parse_coordinates, project
from .mission import MissionController, MissionOutcome
from .preview import plan_mission
from .safety import RULES, Severity

# Named fault sets for `dronegoto sim-scenario`.
SCENARIOS: dict[str, list[Fault]] = {
    "nominal": [],
    "battery-drain": [Fault("battery_drain", at_s=40.0, magnitude=4.0)],
    "impact": [Fault("impact", at_s=60.0, duration_s=1.0, magnitude=7.0)],
    "freefall": [Fault("freefall", at_s=60.0, duration_s=2.0, magnitude=0.05)],
    "gps-loss": [Fault("gps_loss", at_s=60.0)],
    "gps-degraded": [Fault("gps_degraded", at_s=50.0, magnitude=4.0)],
    "link-loss": [Fault("link_loss", at_s=50.0, duration_s=40.0)],
    "telemetry-freeze": [Fault("telemetry_freeze", at_s=60.0, duration_s=90.0)],
    "stuck": [Fault("stuck", at_s=50.0)],
    "wind": [Fault("wind", at_s=45.0, magnitude=14.0)],
    "terrain-rise": [Fault("terrain_rise", at_s=0.0, magnitude=110.0)],
    "attitude-upset": [Fault("attitude_upset", at_s=60.0, duration_s=1.0, magnitude=80.0)],
}

EXIT_OK = 0
EXIT_UNSAFE = 1
EXIT_REFUSED = 2
EXIT_USAGE = 3


def _load_config(path: str | None) -> SafetyConfig:
    return SafetyConfig.load(path) if path else load_default()


def _resolve_terrain(
    home: GeoPoint, target: GeoPoint, config: SafetyConfig, offline: bool
) -> TerrainProfile | None:
    if not config.terrain.enabled:
        return None
    source = FlatTerrain() if offline else None
    return profile_route(home, target, config.terrain, source=source)


class _SimGround:
    """Elevation source backed by the simulator's true terrain."""

    def __init__(self, backend: SimBackend) -> None:
        self.backend = backend

    def lookup(self, points):
        return [self.backend.ground_elevation(p) for p in points]


def _default_sim_home(target: GeoPoint) -> GeoPoint:
    """Put the simulated launch point 1.5 km from the target, so a `fly --backend
    sim` with no --home flies a real route rather than sitting on the spot."""
    return project(target, 225.0, 1500.0)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config)
    except ConfigError as exc:
        print(f"configuration is invalid:\n{exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"configuration OK ({args.config or 'built-in defaults'})")
    print()
    data = config.to_dict()
    for section in sorted(data):
        print(f"  [{section}]")
        for key in sorted(data[section]):
            print(f"    {key:<28} {data[section][key]}")
    return EXIT_OK


def cmd_rules(args: argparse.Namespace) -> int:
    print("SAFETY RULES\n")
    print(f"  {len(RULES)} rules are evaluated every tick. The most severe action wins.\n")
    print("  severity ladder: " + " < ".join(s.label for s in Severity) + "\n")
    for rule in RULES:
        summary = (rule.__doc__ or "").strip().splitlines()
        headline = summary[0] if summary else "(undocumented)"
        print(f"  {rule.__name__.removeprefix('rule_'):<22} {headline}")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config)
        target = parse_coordinates(args.target)
        home = parse_coordinates(args.home) if args.home else _default_sim_home(target)
    except (ConfigError, CoordinateError) as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_USAGE

    terrain = _resolve_terrain(home, target, config, args.offline)
    plan = plan_mission(
        home, target, config,
        hover_s=args.hover,
        requested_altitude_m=args.alt,
        terrain=terrain,
        battery_available=args.battery,
    )
    print(plan.render())
    return EXIT_OK if plan.go else EXIT_REFUSED


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"no such flight log: {path}", file=sys.stderr)
        return EXIT_USAGE
    print(summarise(path))
    return EXIT_OK


def cmd_fly(args: argparse.Namespace) -> int:
    return asyncio.run(_fly(args))


async def _fly(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config)
        target = parse_coordinates(args.target)
    except (ConfigError, CoordinateError) as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_USAGE

    faults: list[Fault] = []
    if args.scenario:
        if args.scenario not in SCENARIOS:
            print(f"unknown scenario {args.scenario!r}. Available: "
                  f"{', '.join(sorted(SCENARIOS))}", file=sys.stderr)
            return EXIT_USAGE
        faults = list(SCENARIOS[args.scenario])

    backend: DroneBackend
    if args.backend == "sim":
        home = parse_coordinates(args.home) if args.home else _default_sim_home(target)
        backend = SimBackend(home, config, sim=SimConfig(start_battery=args.battery or 1.0),
                             faults=faults)
    else:
        from .backends.mavsdk_backend import MavsdkBackend

        if not _confirm_real_flight(args, target):
            print("aborted - nothing was commanded.", file=sys.stderr)
            return EXIT_REFUSED
        backend = MavsdkBackend(config, system_address=args.address)

    try:
        await backend.connect()
    except BackendError as exc:
        print(f"could not connect: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    home = await backend.home_position()
    if home is None:
        print("aircraft has no home position - refusing to fly", file=sys.stderr)
        return EXIT_REFUSED

    telemetry = await backend.read_telemetry()
    terrain = _resolve_terrain(home, target, config, args.offline)
    plan = plan_mission(
        home, target, config,
        hover_s=args.hover,
        requested_altitude_m=args.alt,
        terrain=terrain,
        battery_available=telemetry.battery_remaining,
    )
    print(plan.render())
    print()
    if not plan.go and config.behaviour.require_preflight_pass:
        print("refusing to fly a NO-GO plan.", file=sys.stderr)
        return EXIT_REFUSED

    # The planner flew on the survey (flat, when --offline). The world is what it
    # is: in the simulator that is the backend's own ground model, so the
    # in-flight terrain rule sees the ground the aircraft is actually over.
    # Without this a terrain-rise scenario is a silent no-op.
    flight_terrain = terrain
    if isinstance(backend, SimBackend) and config.terrain.enabled:
        flight_terrain = profile_route(
            home, target, config.terrain, source=_SimGround(backend)
        )

    log_path = Path(args.log) if args.log else Path("flights") / (
        f"{datetime.now(UTC).astimezone().strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    dashboard = Dashboard(enabled=not args.quiet)

    with BlackBox(log_path) as blackbox:
        controller = MissionController(
            backend, config, blackbox=blackbox,
            on_tick=lambda t, s, v: dashboard.update(t, s, v),
        )
        report = await controller.preflight(plan)
        print(report.render())
        print()
        if not report.passed and config.behaviour.require_preflight_pass:
            print("preflight failed - not arming.", file=sys.stderr)
            return EXIT_REFUSED

        result = await controller.run(plan, terrain=flight_terrain, report=report)
        dashboard.close()
        await backend.close()

    print()
    print(result.render())
    print()
    print(f"flight log: {log_path}")
    if result.outcome is MissionOutcome.COMPLETED:
        return EXIT_OK
    return EXIT_OK if result.safe else EXIT_UNSAFE


def cmd_flip(args: argparse.Namespace) -> int:
    return asyncio.run(_flip(args))


async def _flip(args: argparse.Namespace) -> int:
    """The "click flip" button: gate, then request, one automatic flip.

    No mission runs here - this is meant to work while you are hand-flying.
    See dronegoto/tricks.py for the scope note on combining this with an
    active autonomous mission in the same process.
    """
    try:
        config = _load_config(args.config)
    except ConfigError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_USAGE

    backend: DroneBackend
    if args.backend == "sim":
        home = parse_coordinates(args.home) if args.home else GeoPoint(51.5074, -0.1278)
        backend = SimBackend(
            home, config, sim=SimConfig(start_battery=args.battery if args.battery is not None else 1.0)
        )
    else:
        from .backends.mavsdk_backend import MavsdkBackend

        if not _confirm_real_flip(args):
            print("aborted - nothing was commanded.", file=sys.stderr)
            return EXIT_REFUSED
        backend = MavsdkBackend(config, system_address=args.address)

    try:
        await backend.connect()
    except BackendError as exc:
        print(f"could not connect: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.backend == "sim" and args.airborne:
        # Convenience so the gate has something to evaluate without wiring up a
        # full mission first: arm, climb to a test altitude, and level off.
        target_alt = args.alt or 50.0
        await backend.arm()
        await backend.takeoff(target_alt)
        max_ticks = int(target_alt / 1.0 / 0.2) + 100
        for _ in range(max_ticks):
            await backend.step(0.2)
            if backend.altitude_rel_m >= target_alt - 0.5:
                break

    blackbox = BlackBox(args.log) if args.log else None
    try:
        report = await tricks.flip(backend, config, blackbox=blackbox)
    finally:
        if blackbox is not None:
            blackbox.close()
        await backend.close()

    print(report.render())
    return EXIT_OK if report.performed else EXIT_REFUSED


def _confirm_real_flip(args: argparse.Namespace) -> bool:
    """Require an explicit acknowledgement before commanding a flip on a real
    aircraft. Unlike `fly`, the pilot is presumably already flying by hand -
    the risk here is a bad maneuver on command, not an unattended aircraft."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("refusing to flip a real aircraft non-interactively without --yes",
              file=sys.stderr)
        return False
    print("About to command a FLIP on a REAL aircraft.")
    print("This only proceeds if the pre-flip checks pass (altitude, battery, "
          "GPS fix, level attitude, wind).")
    return input("Type 'flip' to confirm: ").strip().lower() == "flip"


def _confirm_real_flight(args: argparse.Namespace, target: GeoPoint) -> bool:
    """Require an explicit acknowledgement before commanding a real aircraft.

    An autonomous flight has no one holding the sticks. The one irreversible
    moment is arming, so it gets a deliberate yes.
    """
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("refusing to fly a real aircraft non-interactively without --yes",
              file=sys.stderr)
        return False
    print(f"About to fly a REAL aircraft to {target}.")
    print("You are responsible for airspace, line of sight and local regulations.")
    return input("Type 'fly' to confirm: ").strip().lower() == "fly"


def cmd_sim_scenario(args: argparse.Namespace) -> int:
    if args.name == "list":
        print("scenarios:")
        for name in sorted(SCENARIOS):
            faults = SCENARIOS[name]
            detail = ", ".join(f"{f.kind}@{f.at_s:.0f}s" for f in faults) or "no faults"
            print(f"  {name:<18} {detail}")
        return EXIT_OK
    args.backend = "sim"
    args.scenario = args.name
    return cmd_fly(args)


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dronegoto",
        description="Fly to a coordinate, hover, and come home - under continuous "
                    "safety supervision.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="path to a YAML config (default: config/default.yaml)")
        p.add_argument("--alt", type=float, help="cruise altitude in metres above home")
        p.add_argument("--hover", type=float, default=120.0, help="seconds to hover on arrival")
        p.add_argument("--home", help="launch coordinate (default for sim: 1.5 km from target)")
        p.add_argument("--offline", action="store_true",
                       help="skip the elevation service and assume flat ground")

    check = sub.add_parser("check", help="validate configuration and print it")
    check.add_argument("--config")
    check.set_defaults(func=cmd_check)

    rules = sub.add_parser("rules", help="list the safety rules")
    rules.set_defaults(func=cmd_rules)

    preview = sub.add_parser("preview", help="plan a mission and print the go/no-go verdict")
    preview.add_argument("target", help="'lat, lon', a DMS pair, or a Google Maps URL")
    preview.add_argument("--battery", type=float, help="battery fraction 0..1 to plan against")
    add_common(preview)
    preview.set_defaults(func=cmd_preview)

    fly = sub.add_parser("fly", help="fly the mission")
    fly.add_argument("target", help="'lat, lon', a DMS pair, or a Google Maps URL")
    fly.add_argument("--backend", choices=("sim", "mavsdk"), default="sim")
    fly.add_argument("--address", default="udp://:14540", help="MAVLink address")
    fly.add_argument("--battery", type=float, help="simulated starting battery, 0..1")
    fly.add_argument("--scenario", help="inject a named fault set (sim only)")
    fly.add_argument("--log", help="black-box path (default: flights/<timestamp>.jsonl)")
    fly.add_argument("--quiet", action="store_true", help="no live dashboard")
    fly.add_argument("--yes", action="store_true", help="skip the real-flight confirmation")
    add_common(fly)
    fly.set_defaults(func=cmd_fly)

    flip = sub.add_parser("flip", help="the 'click flip' button - gate, then trigger, one flip")
    flip.add_argument("--config", help="path to a YAML config (default: config/default.yaml)")
    flip.add_argument("--backend", choices=("sim", "mavsdk"), default="sim")
    flip.add_argument("--address", default="udp://:14540", help="MAVLink address")
    flip.add_argument("--home", help="sim launch coordinate (default: a fixed test point)")
    flip.add_argument("--alt", type=float, help="sim test altitude in metres (default 50)")
    flip.add_argument("--battery", type=float, help="simulated starting battery, 0..1")
    flip.add_argument("--airborne", action="store_true",
                      help="sim only: arm and climb to --alt first, for trying the "
                           "command without a running mission")
    flip.add_argument("--log", help="black-box path to record the attempt (optional)")
    flip.add_argument("--yes", action="store_true", help="skip the real-flip confirmation")
    flip.set_defaults(func=cmd_flip)

    scenario = sub.add_parser("sim-scenario", help="fly a named failure scenario in the simulator")
    scenario.add_argument("name", help="scenario name, or 'list'")
    scenario.add_argument("target", nargs="?", default="51.5074, -0.1278")
    scenario.add_argument("--log")
    scenario.add_argument("--quiet", action="store_true")
    scenario.add_argument("--battery", type=float)
    add_common(scenario)
    scenario.set_defaults(func=cmd_sim_scenario, address="udp://:14540", yes=True)

    replay = sub.add_parser("replay", help="summarise a recorded flight")
    replay.add_argument("path")
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted - the aircraft is still flying its own failsafes.",
              file=sys.stderr)
        return EXIT_UNSAFE


if __name__ == "__main__":
    sys.exit(main())

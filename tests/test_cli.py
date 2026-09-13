"""CLI behaviour, including the refusals.

What the tool declines to do matters as much as what it does: a NO-GO plan, an
unparseable coordinate, and a real aircraft with no human present are all
supposed to stop the flight before anything arms.
"""

from __future__ import annotations

import json

import pytest

from dronegoto.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr()


def test_check_prints_every_setting(capsys):
    code, out = run(["check"], capsys)
    assert code == EXIT_OK
    assert "configuration OK" in out.out
    assert "max_altitude_m" in out.out
    assert "low_pct" in out.out


def test_check_rejects_a_bad_config(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("battery:\n  low_pct: 0.1\n  critical_pct: 0.2\n")
    code, out = run(["check", "--config", str(path)], capsys)
    assert code == EXIT_REFUSED
    assert "invalid" in out.err


def test_rules_lists_every_rule(capsys):
    from dronegoto.safety import RULES

    code, out = run(["rules"], capsys)
    assert code == EXIT_OK
    for rule in RULES:
        assert rule.__name__.removeprefix("rule_") in out.out


def test_preview_go(capsys):
    code, out = run(
        ["preview", "51.5074, -0.1278", "--offline", "--battery", "0.95"], capsys
    )
    assert code == EXIT_OK
    assert "VERDICT" in out.out and "GO" in out.out


def test_preview_no_go_exits_refused(capsys):
    code, out = run(
        ["preview", "51.5074, -0.1278", "--offline", "--battery", "0.20"], capsys
    )
    assert code == EXIT_REFUSED
    assert "NO-GO" in out.out
    assert "BLOCKER" in out.out


def test_preview_accepts_a_google_maps_url(capsys):
    code, out = run(
        ["preview", "https://www.google.com/maps/@51.5074,-0.1278,15z",
         "--offline", "--battery", "0.95"], capsys
    )
    assert code == EXIT_OK
    assert "51.507400, -0.127800" in out.out


def test_unparseable_coordinate_is_a_usage_error(capsys):
    code, out = run(["preview", "somewhere near the park", "--offline"], capsys)
    assert code == EXIT_USAGE
    assert "could not parse" in out.err


def test_fly_nominal_in_the_simulator(tmp_path, capsys):
    log = tmp_path / "f.jsonl"
    code, out = run(
        ["fly", "51.5074,-0.1278", "--backend", "sim", "--offline", "--quiet",
         "--hover", "30", "--log", str(log)],
        capsys,
    )
    assert code == EXIT_OK
    assert "ARMING PERMITTED" in out.out
    assert "COMPLETED" in out.out
    assert log.exists()


def test_fly_writes_a_readable_black_box(tmp_path, capsys):
    log = tmp_path / "f.jsonl"
    run(["fly", "51.5074,-0.1278", "--backend", "sim", "--offline", "--quiet",
         "--hover", "20", "--log", str(log)], capsys)
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    kinds = {r["kind"] for r in records}
    assert {"session", "preflight", "telemetry", "command"} <= kinds
    assert any(r["kind"] == "command" and r["command"] == "arm" for r in records)


def test_fly_refuses_a_no_go_plan_without_arming(tmp_path, capsys):
    code, out = run(
        ["fly", "51.5074,-0.1278", "--backend", "sim", "--offline", "--quiet",
         "--battery", "0.2"],
        capsys,
    )
    assert code == EXIT_REFUSED
    assert "NO-GO" in out.out
    # It must stop at the plan, before preflight ever runs - nothing was armed.
    assert "PREFLIGHT" not in out.out
    assert "MISSION RESULT" not in out.out


@pytest.mark.parametrize("scenario", ["impact", "battery-drain", "gps-loss", "stuck", "wind"])
def test_sim_scenarios_all_end_safely(scenario, tmp_path, capsys):
    code, out = run(
        ["sim-scenario", scenario, "51.5074,-0.1278", "--offline", "--quiet",
         "--hover", "30", "--log", str(tmp_path / f"{scenario}.jsonl")],
        capsys,
    )
    assert code == EXIT_OK, f"scenario {scenario} reported an unsafe outcome"
    assert "MISSION RESULT" in out.out


def test_sim_scenario_list(capsys):
    code, out = run(["sim-scenario", "list"], capsys)
    assert code == EXIT_OK
    assert "battery-drain" in out.out and "telemetry-freeze" in out.out


def test_unknown_scenario_is_a_usage_error(capsys):
    code, out = run(
        ["fly", "51.5074,-0.1278", "--backend", "sim", "--scenario", "nope", "--offline"],
        capsys,
    )
    assert code == EXIT_USAGE
    assert "unknown scenario" in out.err


def test_replay_summarises_a_flight(tmp_path, capsys):
    log = tmp_path / "f.jsonl"
    run(["fly", "51.5074,-0.1278", "--backend", "sim", "--offline", "--quiet",
         "--hover", "20", "--log", str(log)], capsys)
    code, out = run(["replay", str(log)], capsys)
    assert code == EXIT_OK
    assert "FLIGHT REPLAY" in out.out
    assert "arm" in out.out and "takeoff" in out.out


def test_replay_of_a_missing_file(capsys):
    code, _ = run(["replay", "/nonexistent/flight.jsonl"], capsys)
    assert code == EXIT_USAGE


def test_real_flight_is_refused_without_a_human_or_explicit_yes(monkeypatch, capsys):
    """Arming is the irreversible moment, so it never happens by accident in a
    script or a cron job."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, out = run(
        ["fly", "51.5074,-0.1278", "--backend", "mavsdk", "--offline", "--quiet"], capsys
    )
    assert code == EXIT_REFUSED
    assert "without --yes" in out.err
    assert "nothing was commanded" in out.err


def test_terrain_rise_scenario_actually_climbs(tmp_path, capsys):
    """The scenario used to be a silent no-op from the CLI: with --offline the
    controller only saw the flat survey, never the simulator's rising ground."""
    log = tmp_path / "terrain.jsonl"
    code, out = run(
        ["sim-scenario", "terrain-rise", "51.5074,-0.1278", "--offline", "--quiet",
         "--hover", "20", "--log", str(log)],
        capsys,
    )
    assert code == EXIT_OK
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    climbs = [r for r in records if r["kind"] == "command" and r["command"] == "climb"]
    assert climbs, "terrain rise produced no climb command"
    assert "terrain_clearance" in out.out or any(
        r["kind"] == "verdict" and any(t["rule"] == "terrain_clearance" for t in r["triggers"])
        for r in records
    )

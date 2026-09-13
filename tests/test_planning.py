"""Mission planning, terrain handling and the black box."""

from __future__ import annotations

import pytest

from dronegoto.blackbox import BlackBox, replay, summarise
from dronegoto.config import SafetyConfig, TerrainConfig
from dronegoto.elevation import FlatTerrain, TerrainProfile, profile_route
from dronegoto.geo import GeoPoint, project
from dronegoto.preview import plan_mission
from dronegoto.safety import SafetyVerdict, Severity, Trigger
from dronegoto.telemetry import Telemetry

HOME = GeoPoint(51.5074, -0.1278)


class Hill:
    """Ground rising to a peak at the midpoint of the route."""

    def __init__(self, peak_m: float = 80.0, base_m: float = 10.0) -> None:
        self.peak_m, self.base_m = peak_m, base_m

    def lookup(self, points):
        n = len(points)
        return [
            self.base_m + (self.peak_m - self.base_m) * (1 - abs(2 * i / (n - 1) - 1))
            for i in range(n)
        ]


def _profile(target, source, samples=9):
    return profile_route(HOME, target, TerrainConfig(samples=samples), source=source)


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

def test_flat_profile():
    profile = _profile(project(HOME, 45.0, 2000.0), FlatTerrain(25.0))
    assert profile.max_elevation_m == 25.0
    assert profile.required_cruise_amsl(40.0) == 65.0


def test_elevation_at_uses_nearest_sample_never_interpolating_a_peak_away():
    target = project(HOME, 45.0, 2000.0)
    profile = _profile(target, Hill())
    midpoint = profile.points[len(profile.points) // 2]
    assert profile.elevation_at(midpoint) == profile.max_elevation_m


def test_profile_is_none_when_the_source_fails():
    class Unavailable:
        def lookup(self, points):
            return None

    assert _profile(project(HOME, 45.0, 1000.0), Unavailable()) is None


def test_terrain_disabled_returns_no_profile():
    config = TerrainConfig(enabled=False)
    assert profile_route(HOME, project(HOME, 45.0, 1000.0), config) is None


def test_empty_profile_is_rejected():
    with pytest.raises(ValueError):
        TerrainProfile(points=(), elevations=())


def test_mismatched_profile_lengths_are_rejected():
    with pytest.raises(ValueError):
        TerrainProfile(points=(HOME,), elevations=(1.0, 2.0))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_cruise_altitude_is_derived_to_clear_the_highest_ground():
    config = SafetyConfig.from_dict({})
    target = project(HOME, 45.0, 2000.0)
    profile = _profile(target, Hill(peak_m=80.0, base_m=10.0))
    plan = plan_mission(HOME, target, config, hover_s=60.0, terrain=profile,
                        battery_available=0.95)
    # peak 80 AMSL, home 10 AMSL, 40 m clearance -> 110 m above home.
    assert plan.cruise_altitude_rel_m == pytest.approx(110.0)
    assert plan.cruise_altitude_amsl_m == pytest.approx(120.0)
    assert plan.go


def test_a_requested_altitude_that_does_not_clear_the_terrain_is_blocked():
    config = SafetyConfig.from_dict({})
    target = project(HOME, 45.0, 2000.0)
    profile = _profile(target, Hill(peak_m=80.0, base_m=10.0))
    plan = plan_mission(HOME, target, config, hover_s=60.0, requested_altitude_m=50.0,
                        terrain=profile, battery_available=0.95)
    assert not plan.go
    assert any("required" in b or "clearing" in b for b in plan.blockers)


def test_terrain_too_high_to_clear_under_the_ceiling_is_blocked():
    config = SafetyConfig.from_dict({})
    target = project(HOME, 45.0, 2000.0)
    profile = _profile(target, Hill(peak_m=200.0, base_m=10.0))
    plan = plan_mission(HOME, target, config, hover_s=60.0, terrain=profile,
                        battery_available=0.95)
    assert not plan.go
    assert any("ceiling" in b for b in plan.blockers)


def test_missing_elevation_data_warns_by_default_and_blocks_when_required():
    target = project(HOME, 45.0, 1500.0)
    lenient = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, target, lenient, hover_s=60.0, terrain=None,
                        battery_available=0.95)
    assert plan.go and any("no elevation data" in w for w in plan.warnings)

    strict = SafetyConfig.from_dict({"terrain": {"required": True}})
    plan = plan_mission(HOME, target, strict, hover_s=60.0, terrain=None,
                        battery_available=0.95)
    assert not plan.go


def test_target_beyond_the_fence_is_blocked():
    config = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, project(HOME, 45.0, 6000.0), config, hover_s=60.0,
                        battery_available=0.95)
    assert not plan.go
    assert any("geofence" in b for b in plan.blockers)


def test_target_inside_the_fence_margin_is_blocked_as_unreachable():
    """It would turn back before arriving, so launching would be pointless."""
    config = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, project(HOME, 45.0, 4700.0), config, hover_s=60.0,
                        battery_available=0.95)
    assert not plan.go
    assert any("margin" in b for b in plan.blockers)


def test_insufficient_battery_is_blocked():
    config = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, project(HOME, 45.0, 3000.0), config, hover_s=300.0,
                        battery_available=0.55)
    assert not plan.go
    assert any("round trip" in b for b in plan.blockers)


def test_thin_battery_margin_warns_without_blocking():
    config = SafetyConfig.from_dict({})
    # Needs ~44%, and the arm minimum is 50%, so the thin band is 50-54%.
    plan = plan_mission(HOME, project(HOME, 45.0, 1200.0), config, hover_s=60.0,
                        battery_available=0.52)
    assert plan.go
    assert any("margin is thin" in w for w in plan.warnings)


def test_comfortable_battery_margin_warns_about_nothing():
    config = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, project(HOME, 45.0, 1200.0), config, hover_s=60.0,
                        battery_available=0.95)
    assert plan.go
    assert not any("margin is thin" in w for w in plan.warnings)


def test_plan_renders_a_verdict():
    config = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, project(HOME, 45.0, 1500.0), config, hover_s=60.0,
                        battery_available=0.95)
    rendered = plan.render()
    assert "VERDICT" in rendered and "GO" in rendered
    assert f"{plan.distance_m / 1000:.2f} km" in rendered


# ---------------------------------------------------------------------------
# Black box
# ---------------------------------------------------------------------------

def test_blackbox_records_and_replays(tmp_path):
    path = tmp_path / "flight.jsonl"
    frame = Telemetry(timestamp=12.5, position=HOME, altitude_rel_m=100.0,
                      battery_remaining=0.75, satellites=14, in_air=True, armed=True)
    verdict = SafetyVerdict(
        Severity.RETURN,
        (Trigger("impact_detected", Severity.RETURN, "6.2g peak", {"peak_g": 6.2}),),
    )
    with BlackBox(path) as box:
        box.telemetry(frame, "cruise")
        box.verdict(12.5, verdict)
        box.command(12.5, "return", "impact_detected")

    records = list(replay(path))
    kinds = [r["kind"] for r in records]
    assert kinds == ["session", "telemetry", "verdict", "command"]
    assert records[1]["batt"] == 0.75
    assert records[2]["triggers"][0]["detail"]["peak_g"] == 6.2


def test_blackbox_omits_clean_ticks_but_keeps_every_decision(tmp_path):
    path = tmp_path / "flight.jsonl"
    with BlackBox(path) as box:
        box.verdict(1.0, SafetyVerdict(Severity.NONE))
        box.verdict(2.0, SafetyVerdict(Severity.WARN, (Trigger("w", Severity.WARN, "x"),)))
    assert [r["kind"] for r in replay(path)] == ["session", "verdict"]


def test_replay_survives_a_truncated_final_line(tmp_path):
    """A power loss mid-write must not make the rest of the flight unreadable."""
    path = tmp_path / "flight.jsonl"
    with BlackBox(path) as box:
        box.command(1.0, "arm")
    with path.open("a") as handle:
        handle.write('{"kind":"command","t":2.0,"comm')
    records = list(replay(path))
    assert [r["kind"] for r in records] == ["session", "command"]


def test_summarise_reports_commands_and_decisions(tmp_path):
    path = tmp_path / "flight.jsonl"
    frame = Telemetry(timestamp=0.0, position=HOME, battery_remaining=0.9, in_air=True)
    with BlackBox(path) as box:
        box.telemetry(frame, "cruise")
        box.telemetry(frame.with_(timestamp=60.0, battery_remaining=0.4), "return")
        box.command(30.0, "return", "battery_low")
        box.verdict(30.0, SafetyVerdict(
            Severity.RETURN, (Trigger("battery_low", Severity.RETURN, "battery 40%"),)))
    text = summarise(path)
    assert "lowest battery ... 40%" in text
    assert "battery_low" in text
    assert "duration ......... 60.0s" in text


def test_hover_longer_than_the_budget_is_blocked_at_planning():
    """Previously such a mission flew and was then reported as RETURNED by the
    hover timeout, which is the wrong place to discover a planning error."""
    config = SafetyConfig.from_dict({})
    plan = plan_mission(HOME, project(HOME, 45.0, 800.0), config, hover_s=900.0,
                        battery_available=0.95)
    assert not plan.go
    assert any("hover" in b and "exceeds" in b for b in plan.blockers)

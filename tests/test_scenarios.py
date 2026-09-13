"""Fault-injection scenarios: a full flight, broken in one specific way each time.

Unit tests prove a rule fires on a crafted context. These prove the rule fires
during an actual flight, that the controller acts on it, and - the part that
matters most - that the aircraft still ends up somewhere safe.
"""

from __future__ import annotations

import random

import pytest

from dronegoto.backends.sim import Fault, SimBackend, SimConfig
from dronegoto.config import SafetyConfig
from dronegoto.geo import GeoPoint, haversine_m, project
from dronegoto.mission import MissionController, MissionOutcome
from dronegoto.preview import plan_mission
from dronegoto.safety import Severity

HOME = GeoPoint(51.5074, -0.1278)


async def fly(
    faults: list[Fault] | None = None,
    distance_m: float = 1500.0,
    hover_s: float = 60.0,
    config: SafetyConfig | None = None,
    sim_config: SimConfig | None = None,
    terrain=None,
    requested_altitude_m: float | None = None,
):
    config = config or SafetyConfig.from_dict({})
    target = project(HOME, 45.0, distance_m)
    backend = SimBackend(HOME, config, sim=sim_config, faults=list(faults or []))
    await backend.connect()
    telemetry = await backend.read_telemetry()
    plan = plan_mission(
        HOME, target, config,
        hover_s=hover_s,
        requested_altitude_m=requested_altitude_m,
        terrain=terrain,
        battery_available=telemetry.battery_remaining,
    )
    controller = MissionController(backend, config)
    result = await controller.run(plan, terrain=terrain)
    return result, backend, controller


def triggered(result, rule: str) -> bool:
    return any(t.rule == rule for t in result.triggers)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

async def test_nominal_flight_completes():
    result, backend, _ = await fly()
    assert result.outcome is MissionOutcome.COMPLETED
    assert result.reached_target
    assert result.triggers == ()
    assert result.distance_from_home_m == pytest.approx(0.0, abs=2.0)


# ---------------------------------------------------------------------------
# The two failures the user explicitly asked for
# ---------------------------------------------------------------------------

async def test_battery_drain_returns_home():
    """Accelerated drain: the aircraft must abandon the mission and come back."""
    result, _, _ = await fly(faults=[Fault("battery_drain", at_s=30.0, magnitude=7.0)])
    assert result.outcome in (MissionOutcome.RETURNED, MissionOutcome.LANDED_OUT)
    assert triggered(result, "battery_low") or triggered(result, "return_energy_margin")
    assert result.safe


async def test_battery_drain_aborts_the_mission_and_lands_at_home():
    """A burst of heavy drain steep enough to abort the mission, but leaving
    enough charge to fly home - the case the return-energy estimator exists for."""
    result, _, _ = await fly(
        distance_m=2000.0,
        faults=[Fault("battery_drain", at_s=40.0, duration_s=40.0, magnitude=6.0)],
    )
    assert result.outcome is MissionOutcome.RETURNED
    assert triggered(result, "return_energy_margin")
    assert result.distance_from_home_m == pytest.approx(0.0, abs=5.0)
    assert result.final_battery > 0.25, "should land home with charge to spare"


async def test_transient_drain_that_crosses_no_threshold_does_not_abort():
    """The other half: a brief spike must not ground an otherwise healthy flight."""
    result, _, _ = await fly(
        distance_m=1500.0,
        faults=[Fault("battery_drain", at_s=40.0, duration_s=30.0, magnitude=8.0)],
    )
    assert result.outcome is MissionOutcome.COMPLETED
    assert result.reached_target


async def test_collision_returns_home():
    """The user's requirement: if it hits something, come home."""
    result, _, _ = await fly(
        faults=[Fault("impact", at_s=80.0, duration_s=0.5, magnitude=6.0)]
    )
    assert triggered(result, "impact_detected")
    assert result.outcome is MissionOutcome.RETURNED
    assert result.distance_from_home_m == pytest.approx(0.0, abs=5.0)
    assert not result.reached_target, "should abandon the outbound leg on impact"


async def test_collision_during_hover_still_returns():
    """Exercises the sustained path: 5g is below the instant threshold, so it
    only fires once confirmed across the window."""
    result, _, _ = await fly(
        distance_m=800.0,
        faults=[Fault("impact", at_s=140.0, duration_s=1.5, magnitude=5.0)],
    )
    assert triggered(result, "impact_detected")
    assert result.safe


async def test_attitude_upset_returns_home():
    result, _, _ = await fly(
        faults=[Fault("attitude_upset", at_s=80.0, duration_s=1.0, magnitude=75.0)]
    )
    assert triggered(result, "attitude_excursion")
    assert result.safe


# ---------------------------------------------------------------------------
# Navigation and link failures
# ---------------------------------------------------------------------------

async def test_gps_loss_lands():
    result, _, _ = await fly(faults=[Fault("gps_loss", at_s=70.0)])
    assert result.outcome is MissionOutcome.LANDED_OUT
    assert triggered(result, "position_unknown") or triggered(result, "gps_lost")
    assert result.safe


async def test_degraded_gps_returns_home():
    result, _, _ = await fly(
        faults=[Fault("gps_degraded", at_s=60.0, magnitude=4.0)]
    )
    assert triggered(result, "gps_degraded")
    assert result.safe


async def test_telemetry_freeze_returns_home():
    """The aircraft stops talking. Silence must itself trigger the failsafe."""
    result, _, _ = await fly(faults=[Fault("telemetry_freeze", at_s=70.0, duration_s=20.0)])
    assert triggered(result, "telemetry_stale")
    assert result.safe


@pytest.mark.parametrize(
    "action,expected_rule",
    [("return", "link_loss"), ("land", "link_loss"), ("hold", None), ("continue", None)],
)
async def test_link_loss_honours_policy(action, expected_rule):
    config = SafetyConfig.from_dict({"link": {"loss_action": action}})
    result, _, _ = await fly(
        config=config, faults=[Fault("link_loss", at_s=60.0, duration_s=30.0)]
    )
    if expected_rule:
        assert triggered(result, expected_rule)
    assert result.safe, f"link_loss policy '{action}' left the aircraft unsafe"


async def test_link_loss_continue_still_completes_the_mission():
    """'continue' is the onboard-mission behaviour: finish the flight without us."""
    config = SafetyConfig.from_dict({"link": {"loss_action": "continue"}})
    result, _, _ = await fly(
        config=config, faults=[Fault("link_loss", at_s=60.0, duration_s=30.0)]
    )
    assert result.outcome is MissionOutcome.COMPLETED
    assert result.reached_target


# ---------------------------------------------------------------------------
# Environment and geometry
# ---------------------------------------------------------------------------

async def test_stuck_aircraft_gives_up_and_returns():
    """Not getting closer is a failure no single sensor reports."""
    result, _, _ = await fly(faults=[Fault("stuck", at_s=60.0)])
    assert triggered(result, "no_progress")
    assert result.safe


async def test_high_wind_returns_home():
    result, _, _ = await fly(faults=[Fault("wind", at_s=60.0, magnitude=14.0)])
    assert triggered(result, "wind_speed")
    assert result.safe


async def test_freefall_lands_rather_than_terminating_by_default():
    result, _, _ = await fly(
        faults=[Fault("freefall", at_s=70.0, duration_s=2.0, magnitude=0.05)]
    )
    assert triggered(result, "freefall_detected")
    assert result.outcome is MissionOutcome.LANDED_OUT
    assert result.outcome is not MissionOutcome.TERMINATED


async def test_geofence_turns_the_aircraft_around_even_if_preflight_is_bypassed():
    """Proves the layers are independent: the in-flight fence does not rely on
    the planner having caught the same problem."""
    config = SafetyConfig.from_dict({
        "behaviour": {"require_preflight_pass": False},
        "geofence": {"max_radius_m": 1200.0, "soft_margin_m": 200.0},
        "timing": {"max_flight_time_s": 800.0, "max_hover_time_s": 120.0},
    })
    result, _, _ = await fly(config=config, distance_m=2500.0, hover_s=30.0)
    assert triggered(result, "geofence_soft") or triggered(result, "geofence_predicted")
    assert not result.reached_target
    assert result.safe


async def test_rising_terrain_is_caught_in_flight():
    """Ground rises under a constant relative altitude - invisible to any check
    measured from the launch point."""
    from dronegoto.config import TerrainConfig
    from dronegoto.elevation import profile_route

    target = project(HOME, 45.0, 1500.0)

    class RisingGround:
        def lookup(self, points):
            return [0.0] * len(points)  # planner believes it is flat

    terrain = profile_route(HOME, target, TerrainConfig(samples=9), source=RisingGround())
    config = SafetyConfig.from_dict({})
    backend = SimBackend(
        HOME, config,
        faults=[Fault("terrain_rise", at_s=0.0, magnitude=120.0)],
    )
    await backend.connect()
    telemetry = await backend.read_telemetry()
    plan = plan_mission(
        HOME, target, config, hover_s=30.0, terrain=terrain,
        battery_available=telemetry.battery_remaining,
    )
    controller = MissionController(backend, config)

    # Feed the controller the *true* rising ground, not the flat survey the
    # planner used, so the discrepancy shows up in flight exactly as it would.
    class TrueTerrain:
        points = terrain.points
        elevations = terrain.elevations
        max_elevation_m = 180.0

        def elevation_at(self, point):
            return 120.0 * (haversine_m(HOME, point) / 1000.0)

    result = await controller.run(plan, terrain=TrueTerrain())
    assert triggered(result, "terrain_clearance")
    assert result.safe


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

async def test_hover_timeout_sends_it_home():
    config = SafetyConfig.from_dict({"timing": {"max_hover_time_s": 20.0}})
    result, _, _ = await fly(config=config, distance_m=600.0, hover_s=600.0)
    assert triggered(result, "hover_timeout")
    assert result.safe


async def test_mission_timeout_sends_it_home():
    """Preflight would normally reject a plan this long - bypassing it proves the
    in-flight budget is enforced independently of the planner."""
    config = SafetyConfig.from_dict({
        "timing": {"max_flight_time_s": 120.0, "max_hover_time_s": 60.0},
        "behaviour": {"require_preflight_pass": False},
    })
    result, _, _ = await fly(config=config, distance_m=900.0, hover_s=50.0)
    assert triggered(result, "mission_timeout")
    assert result.safe


# ---------------------------------------------------------------------------
# Preflight refusal
# ---------------------------------------------------------------------------

async def test_flat_battery_never_arms():
    config = SafetyConfig.from_dict({})
    result, backend, _ = await fly(config=config, sim_config=SimConfig(start_battery=0.30))
    assert result.outcome is MissionOutcome.PREFLIGHT_FAILED
    assert "arm" not in [c for _, c in backend.command_log]
    assert backend.altitude_rel_m == 0.0


async def test_target_outside_the_fence_never_arms():
    result, backend, _ = await fly(distance_m=6000.0)
    assert result.outcome is MissionOutcome.PREFLIGHT_FAILED
    assert backend.altitude_rel_m == 0.0


async def test_poor_gps_never_arms():
    config = SafetyConfig.from_dict({})
    result, backend, _ = await fly(config=config, sim_config=SimConfig(satellites=4, hdop=6.0))
    assert result.outcome is MissionOutcome.PREFLIGHT_FAILED
    assert backend.altitude_rel_m == 0.0


# ---------------------------------------------------------------------------
# Chaos
# ---------------------------------------------------------------------------

CHAOS_FAULTS = [
    ("battery_drain", 4.0), ("impact", 6.0), ("gps_degraded", 4.0), ("gps_loss", 0.0),
    ("link_loss", 0.0), ("stuck", 0.0), ("wind", 14.0), ("telemetry_freeze", 0.0),
    ("terrain_rise", 90.0), ("attitude_upset", 80.0), ("freefall", 0.05),
]


@pytest.mark.parametrize("seed", range(40))
async def test_chaos_always_ends_in_a_safe_state(seed):
    """Randomised simultaneous faults. The aircraft must always reach a defined,
    safe terminal state - never hang, never end mid-air, never crash the
    supervisor."""
    rng = random.Random(seed)
    faults = [
        Fault(
            kind,
            at_s=rng.uniform(20.0, 200.0),
            duration_s=rng.choice([1.0, 5.0, 30.0, float("inf")]),
            magnitude=magnitude,
        )
        for kind, magnitude in rng.sample(CHAOS_FAULTS, rng.randint(1, 3))
    ]
    result, backend, _ = await fly(faults=faults, distance_m=rng.uniform(400.0, 2500.0))

    assert result.outcome in set(MissionOutcome), "undefined outcome"
    assert result.safe or result.outcome is MissionOutcome.TERMINATED, (
        f"unsafe outcome {result.outcome} from {[(f.kind, round(f.at_s)) for f in faults]}"
    )
    if result.outcome is MissionOutcome.LOST_CONTACT:
        # We went blind. We cannot observe the landing - but we must have
        # commanded the aircraft home before handing over to its own failsafes.
        commands = [c for _, c in backend.command_log]
        assert any(c.startswith(("return", "land")) for c in commands), (
            f"went blind without commanding a return: {commands}"
        )
    else:
        assert backend.altitude_rel_m <= 0.6, "ended the scenario still in the air"

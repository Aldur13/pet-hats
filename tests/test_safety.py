"""Per-rule tests.

Every rule gets two tests at minimum: it fires when it should, and it stays
silent on a nominal context. The second half is what stops a trigger-happy rule
from grounding the aircraft on a single noisy sample.
"""

from __future__ import annotations

import pytest

from dronegoto.config import SafetyConfig
from dronegoto.geo import project
from dronegoto.safety import (
    RULES,
    MissionPhase,
    SafetyEngine,
    Severity,
    estimate_return_cost_pct,
    rule_altitude_ceiling,
    rule_battery_levels,
    rule_freefall,
    rule_geofence,
    rule_gps_quality,
    rule_hover_timeout,
    rule_impact,
    rule_link_loss,
    rule_mission_timeout,
    rule_no_progress,
    rule_position_unknown,
    rule_return_energy,
    rule_telemetry_stale,
    rule_terrain_clearance,
    rule_uncommanded_descent,
    rule_wind,
    update_progress,
)
from tests.conftest import (
    HOME,
    TARGET,
    make_context,
    make_state,
    make_telemetry,
    sustained_history,
)


# ---------------------------------------------------------------------------
# The nominal case: nothing may fire
# ---------------------------------------------------------------------------

def test_nominal_context_triggers_no_rules(ctx):
    """If this fails, every 'does not fire' assertion below is worthless."""
    fired = [r.__name__ for r in RULES if r(ctx()) is not None]
    assert fired == [], f"rules fired on healthy telemetry: {fired}"


def test_nominal_verdict_is_clear(config, ctx):
    verdict = SafetyEngine(config).evaluate(ctx())
    assert verdict.severity is Severity.NONE
    assert verdict.ok
    assert verdict.triggers == ()


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "level,expected_rule,expected_severity",
    [
        (0.85, None, None),
        (0.41, None, None),
        (0.40, "battery_low", Severity.RETURN),
        (0.35, "battery_low", Severity.RETURN),
        (0.20, "battery_critical", Severity.LAND),
        (0.15, "battery_critical", Severity.LAND),
        (0.10, "battery_emergency", Severity.LAND),
        (0.05, "battery_emergency", Severity.LAND),
    ],
)
def test_battery_thresholds(ctx, level, expected_rule, expected_severity):
    trigger = rule_battery_levels(ctx(battery_remaining=level))
    if expected_rule is None:
        assert trigger is None
    else:
        assert trigger is not None
        assert trigger.rule == expected_rule
        assert trigger.severity is expected_severity


def test_battery_at_40_percent_returns_home(ctx):
    """The threshold the user actually asked for, pinned explicitly."""
    trigger = rule_battery_levels(ctx(battery_remaining=0.40))
    assert trigger.rule == "battery_low"
    assert trigger.severity is Severity.RETURN
    assert "40%" in trigger.reason


def test_unknown_battery_is_treated_as_low(ctx):
    trigger = rule_battery_levels(ctx(battery_remaining=None))
    assert trigger is not None
    assert trigger.rule == "battery_unknown"
    assert trigger.severity is Severity.RETURN


def test_unknown_battery_on_the_ground_is_not_an_alarm(ctx):
    context = ctx(battery_remaining=None, in_air=False, state=make_state(phase=MissionPhase.PREFLIGHT))
    assert rule_battery_levels(context) is None


# ---------------------------------------------------------------------------
# Return-energy estimator
# ---------------------------------------------------------------------------

def test_return_energy_ignores_short_hop_with_healthy_battery(ctx):
    assert rule_return_energy(ctx()) is None


def test_return_energy_fires_before_the_fixed_threshold_at_distance(config):
    """The whole point: at 4.5 km out, 44% is already too little to get home.

    The fixed 40% threshold is still perfectly happy here. That gap is the
    entire reason the dynamic estimator exists.
    """
    far = project(HOME, 45.0, 4500.0)
    context = make_context(
        config,
        position=far,
        battery_remaining=0.44,
        state=make_state(best_distance_m=100.0, best_distance_at=100.0),
    )
    assert rule_battery_levels(context) is None, "fixed threshold should still be happy"
    trigger = rule_return_energy(context)
    assert trigger is not None
    assert trigger.rule == "return_energy_margin"
    assert trigger.severity is Severity.RETURN


def test_return_energy_catches_a_headwind_the_config_did_not_predict(config):
    """Drain measured in flight is double the configured figure - a headwind, a
    cold pack, a heavier payload. A fixed percentage cannot see this; the
    adaptive estimate can, and pulls the aircraft home early."""
    far = project(HOME, 45.0, 2500.0)
    context = make_context(
        config,
        position=far,
        battery_remaining=0.55,
        state=make_state(
            battery_at_start=1.0,
            distance_travelled_m=2500.0,  # 45% used over 2.5 km = 18%/km observed
            best_distance_m=100.0,
            best_distance_at=100.0,
        ),
    )
    assert rule_battery_levels(context) is None, "55% is far above every fixed threshold"
    trigger = rule_return_energy(context)
    assert trigger is not None
    assert trigger.rule == "return_energy_margin"


def test_return_energy_is_silent_once_already_returning(config):
    far = project(HOME, 45.0, 4500.0)
    context = make_context(
        config, position=far, battery_remaining=0.45, state=make_state(phase=MissionPhase.RETURN)
    )
    assert rule_return_energy(context) is None


def test_adaptive_estimation_only_tightens_the_margin(config):
    """Observed drain may raise the estimate, never lower it below configured."""
    far = project(HOME, 45.0, 2000.0)
    thirsty = make_context(
        config,
        position=far,
        battery_remaining=0.6,
        state=make_state(battery_at_start=1.0, distance_travelled_m=2000.0),
    )
    economical = make_context(
        config,
        position=far,
        battery_remaining=0.99,
        state=make_state(battery_at_start=1.0, distance_travelled_m=2000.0),
    )
    configured_only = config.battery.cruise_drain_pct_per_km * 2.0
    assert estimate_return_cost_pct(thirsty) > configured_only
    # A frugal flight must not shrink the reserve below the configured figure.
    assert estimate_return_cost_pct(economical) >= configured_only


# ---------------------------------------------------------------------------
# Impact / collision - the user's other explicit requirement
# ---------------------------------------------------------------------------

def test_impact_spike_returns_home(config):
    """Sustained path: 4.8g is under the instant threshold, so it needs
    confirmation across the full window."""
    history = sustained_history(end=100.0, duration=0.6, accel_magnitude_g=4.8)
    context = make_context(config, history=history, telemetry=history.latest)
    trigger = rule_impact(context)
    assert trigger is not None
    assert trigger.rule == "impact_detected"
    assert trigger.severity is Severity.RETURN
    assert trigger.detail["path"] == "sustained"
    assert trigger.detail["peak_g"] == pytest.approx(4.8)


def test_single_hard_peak_returns_home_immediately(config):
    """Instant path. Telemetry reports peak-hold acceleration, so one frame at
    7g is a collision - and a real impact is over long before a second frame
    arrives, so waiting for confirmation would miss it entirely."""
    trigger = rule_impact(make_context(config, accel_magnitude_g=7.0))
    assert trigger is not None
    assert trigger.rule == "impact_detected"
    assert trigger.severity is Severity.RETURN
    assert trigger.detail["path"] == "instant"


def test_single_moderate_spike_does_not_trigger(config):
    """A single frame between the two thresholds is not enough: 4g could be a
    hard gust or a sharp manoeuvre, so it needs confirmation over the window."""
    frames = [make_telemetry(99.0 + i * 0.02, accel_magnitude_g=1.0) for i in range(30)]
    frames[-1] = make_telemetry(frames[-1].timestamp, accel_magnitude_g=4.0)
    from tests.conftest import filled_history

    history = filled_history(frames)
    context = make_context(config, now=frames[-1].timestamp, history=history, telemetry=frames[-1])
    assert rule_impact(context) is None


def test_normal_flight_g_does_not_trigger_impact(ctx):
    assert rule_impact(ctx(accel_magnitude_g=1.2)) is None


def test_attitude_excursion_returns_home(ctx):
    trigger = rule_impact(ctx(attitude_deg=(75.0, 5.0, 90.0)))
    assert trigger is not None
    assert trigger.rule == "attitude_excursion"
    assert trigger.severity is Severity.RETURN


def test_impact_detection_can_be_disabled(config):
    disabled = SafetyConfig.from_dict({"impact": {"enabled": False}})
    history = sustained_history(end=100.0, duration=0.15, accel_magnitude_g=9.0)
    context = make_context(disabled, history=history, telemetry=history.latest)
    assert rule_impact(context) is None


def test_freefall_is_terminate_severity(config):
    history = sustained_history(end=100.0, duration=0.8, accel_magnitude_g=0.05)
    context = make_context(config, history=history, telemetry=history.latest)
    trigger = rule_freefall(context)
    assert trigger is not None
    assert trigger.severity is Severity.TERMINATE


def test_uncommanded_descent_holds(ctx):
    trigger = rule_uncommanded_descent(ctx(velocity_ned_ms=(0.0, 0.0, 3.0)))
    assert trigger is not None
    assert trigger.severity is Severity.HOLD


def test_commanded_climb_is_not_a_descent(ctx):
    assert rule_uncommanded_descent(ctx(velocity_ned_ms=(0.0, 0.0, -3.0))) is None


# ---------------------------------------------------------------------------
# Geofence
# ---------------------------------------------------------------------------

def test_inside_the_fence_is_quiet(config):
    near = project(HOME, 90.0, 1000.0)
    assert rule_geofence(make_context(config, position=near, velocity_ned_ms=(0.0, 5.0, 0.0))) is None


def test_hard_geofence_breach(config):
    outside = project(HOME, 90.0, 5200.0)
    trigger = rule_geofence(make_context(config, position=outside, velocity_ned_ms=(0.0, 0.0, 0.0)))
    assert trigger.rule == "geofence_breach"
    assert trigger.severity is Severity.RETURN


def test_soft_geofence_ring(config):
    inside_margin = project(HOME, 90.0, 4700.0)
    trigger = rule_geofence(
        make_context(config, position=inside_margin, velocity_ned_ms=(0.0, 0.0, 0.0))
    )
    assert trigger.rule == "geofence_soft"


def test_predictive_geofence_turns_back_before_the_boundary(config):
    """Flying at the fence fast enough to cross it within the lookahead window."""
    approaching = project(HOME, 90.0, 4300.0)
    trigger = rule_geofence(
        make_context(config, position=approaching, velocity_ned_ms=(0.0, 25.0, 0.0))
    )
    assert trigger is not None
    assert trigger.rule == "geofence_predicted"
    assert trigger.detail["projected_m"] > trigger.detail["distance_m"]


def test_predictive_geofence_ignores_traffic_heading_inward(config):
    approaching = project(HOME, 90.0, 4300.0)
    assert (
        rule_geofence(make_context(config, position=approaching, velocity_ned_ms=(0.0, -25.0, 0.0)))
        is None
    )


def test_geofence_can_be_disabled(config):
    disabled = SafetyConfig.from_dict({"geofence": {"enabled": False}})
    outside = project(HOME, 90.0, 9000.0)
    assert rule_geofence(make_context(disabled, position=outside)) is None


# ---------------------------------------------------------------------------
# Altitude and terrain
# ---------------------------------------------------------------------------

def test_altitude_below_ceiling_is_quiet(ctx):
    assert rule_altitude_ceiling(ctx(altitude_rel_m=119.0)) is None


def test_altitude_just_over_ceiling_holds(ctx):
    trigger = rule_altitude_ceiling(ctx(altitude_rel_m=125.0))
    assert trigger.rule == "altitude_ceiling"
    assert trigger.severity is Severity.HOLD


def test_altitude_far_over_ceiling_returns(ctx):
    trigger = rule_altitude_ceiling(ctx(altitude_rel_m=145.0))
    assert trigger.rule == "altitude_ceiling_exceeded"
    assert trigger.severity is Severity.RETURN


def test_terrain_clearance_violation(config):
    context = make_context(
        config,
        altitude_amsl_m=50.0,
        state=make_state(ground_elevation_here_m=30.0, max_ground_elevation_m=30.0),
    )
    trigger = rule_terrain_clearance(context)
    assert trigger is not None
    assert trigger.rule == "terrain_clearance"
    assert trigger.detail["clearance_m"] == pytest.approx(20.0)


def test_terrain_clearance_satisfied(config):
    context = make_context(
        config, altitude_amsl_m=120.0, state=make_state(ground_elevation_here_m=30.0)
    )
    assert rule_terrain_clearance(context) is None


def test_terrain_rule_silent_without_elevation_data(config):
    context = make_context(config, state=make_state(ground_elevation_here_m=None))
    assert rule_terrain_clearance(context) is None


# ---------------------------------------------------------------------------
# Navigation integrity
# ---------------------------------------------------------------------------

def test_position_loss_lands(ctx):
    trigger = rule_position_unknown(ctx(position=None))
    assert trigger.severity is Severity.LAND


def test_gps_fix_loss_holds_then_lands(config):
    brief = sustained_history(end=100.0, duration=0.5, has_fix=False)
    assert rule_gps_quality(
        make_context(config, history=brief, telemetry=brief.latest)
    ).severity is Severity.HOLD

    prolonged = sustained_history(end=100.0, duration=3.5, step=0.1, has_fix=False)
    assert rule_gps_quality(
        make_context(config, history=prolonged, telemetry=prolonged.latest)
    ).rule == "gps_lost"


def test_degraded_gps_warns_then_returns(config):
    brief = sustained_history(end=100.0, duration=1.0, step=0.1, satellites=5, hdop=4.0)
    assert rule_gps_quality(
        make_context(config, history=brief, telemetry=brief.latest)
    ).severity is Severity.WARN

    prolonged = sustained_history(end=100.0, duration=6.0, step=0.1, satellites=5, hdop=4.0)
    trigger = rule_gps_quality(make_context(config, history=prolonged, telemetry=prolonged.latest))
    assert trigger.rule == "gps_degraded"
    assert trigger.severity is Severity.RETURN


def test_good_gps_is_quiet(ctx):
    assert rule_gps_quality(ctx(satellites=18, hdop=0.6)) is None


def test_stale_telemetry_returns_home(ctx):
    """Silence from the aircraft is itself an alarm."""
    context = ctx(now=105.0, telemetry=make_telemetry(timestamp=100.0))
    trigger = rule_telemetry_stale(context)
    assert trigger is not None
    assert trigger.severity is Severity.RETURN
    assert trigger.detail["age_s"] == pytest.approx(5.0)


def test_fresh_telemetry_is_quiet(ctx):
    assert rule_telemetry_stale(ctx(now=100.1, telemetry=make_telemetry(timestamp=100.0))) is None


@pytest.mark.parametrize(
    "action,expected",
    [
        ("continue", Severity.WARN),
        ("hold", Severity.HOLD),
        ("return", Severity.RETURN),
        ("land", Severity.LAND),
    ],
)
def test_link_loss_honours_configured_action(action, expected):
    config = SafetyConfig.from_dict({"link": {"loss_action": action}})
    history = sustained_history(end=100.0, duration=6.0, step=0.1, link_ok=False)
    trigger = rule_link_loss(make_context(config, history=history, telemetry=history.latest))
    assert trigger is not None
    assert trigger.severity is expected


def test_healthy_link_is_quiet(ctx):
    assert rule_link_loss(ctx()) is None


def test_no_progress_returns_home(config):
    state = make_state(best_distance_m=2000.0, best_distance_at=60.0)
    assert rule_no_progress(make_context(config, now=100.0, state=state)).rule == "no_progress"


def test_progress_resets_the_stall_timer(config):
    state = make_state(best_distance_m=2000.0, best_distance_at=60.0)
    update_progress(state, make_telemetry(position=project(TARGET, 0.0, 50.0)), now=100.0)
    assert state.best_distance_at == 100.0
    assert rule_no_progress(make_context(config, now=100.0, state=state)) is None


# ---------------------------------------------------------------------------
# Budgets and environment
# ---------------------------------------------------------------------------

def test_mission_timeout_returns_then_lands(config):
    state = make_state(started_at=0.0)
    assert rule_mission_timeout(make_context(config, now=950.0, state=state)).severity is Severity.RETURN
    assert rule_mission_timeout(make_context(config, now=1400.0, state=state)).severity is Severity.LAND


def test_mission_within_budget_is_quiet(config):
    assert rule_mission_timeout(make_context(config, now=300.0, state=make_state(started_at=0.0))) is None


def test_hover_timeout(config):
    state = make_state(phase=MissionPhase.HOVER, hover_started_at=0.0)
    assert rule_hover_timeout(make_context(config, now=310.0, state=state)).rule == "hover_timeout"
    assert rule_hover_timeout(make_context(config, now=100.0, state=state)) is None


def test_wind_limit(ctx):
    assert rule_wind(ctx(wind_speed_ms=12.0)).severity is Severity.RETURN
    assert rule_wind(ctx(wind_speed_ms=4.0)) is None


def test_terrain_clearance_does_not_chatter_at_the_threshold(config):
    """Sitting exactly on the minimum, or a metre under it, is altitude-hold
    wobble - not a terrain problem. Without the tolerance band this fired every
    time the aircraft entered cruise just below its commanded altitude."""
    for clearance in (40.0, 39.5, 38.5):
        context = make_context(
            config,
            altitude_amsl_m=30.0 + clearance,
            state=make_state(ground_elevation_here_m=30.0, max_ground_elevation_m=30.0),
        )
        assert rule_terrain_clearance(context) is None, f"chattered at {clearance} m"


def test_terrain_clearance_still_fires_once_genuinely_low(config):
    context = make_context(
        config,
        altitude_amsl_m=67.0,  # 37 m over 30 m ground: past the tolerance band
        state=make_state(ground_elevation_here_m=30.0, max_ground_elevation_m=30.0),
    )
    assert rule_terrain_clearance(context) is not None

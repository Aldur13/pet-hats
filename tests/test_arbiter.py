"""Arbiter behaviour: priority, latching, and the terminate policy.

These are the properties that make the rule set trustworthy as a whole. An
individually correct rule is worthless if ordering can hide it behind a milder one.
"""

from __future__ import annotations

import itertools
import random

import pytest

from dronegoto.config import SafetyConfig
from dronegoto.safety import (
    SafetyEngine,
    Severity,
    Trigger,
)
from tests.conftest import make_context, sustained_history


def const_rule(name: str, severity: Severity):
    def _rule(ctx):
        return Trigger(name, severity, f"synthetic {name}")

    _rule.__name__ = f"rule_{name}"
    return _rule


def silent_rule(ctx):
    return None


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "severities",
    list(itertools.permutations([Severity.WARN, Severity.HOLD, Severity.RETURN, Severity.LAND])),
)
def test_most_severe_action_always_wins_regardless_of_rule_order(config, severities):
    """PX4's rule: 'If multiple failsafes trigger, more severe action executes.'

    Exhaustive over every ordering, because the failure this guards against is
    precisely an ordering-dependent one.
    """
    rules = [const_rule(f"r{i}", s) for i, s in enumerate(severities)]
    verdict = SafetyEngine(config, rules=rules).evaluate(make_context(config))
    assert verdict.severity is Severity.LAND
    assert verdict.primary.severity is Severity.LAND


def test_arbiter_never_returns_less_than_any_individual_trigger(config):
    """Property test over randomised rule sets."""
    rng = random.Random(20240913)
    choices = [Severity.NONE, Severity.WARN, Severity.HOLD, Severity.RETURN, Severity.LAND]
    for _ in range(300):
        picks = [rng.choice(choices) for _ in range(rng.randint(1, 6))]
        rules = [
            silent_rule if s is Severity.NONE else const_rule(f"r{i}", s)
            for i, s in enumerate(picks)
        ]
        engine = SafetyEngine(config, rules=rules)
        verdict = engine.evaluate(make_context(config))
        assert verdict.severity >= max(picks), f"{verdict.severity} < {max(picks)} for {picks}"
        for trigger in verdict.triggers:
            assert verdict.severity >= trigger.severity


def test_all_triggers_are_reported_not_just_the_winner(config):
    rules = [const_rule("a", Severity.RETURN), const_rule("b", Severity.HOLD)]
    verdict = SafetyEngine(config, rules=rules).evaluate(make_context(config))
    assert {t.rule for t in verdict.triggers} == {"a", "b"}


# ---------------------------------------------------------------------------
# Latching
# ---------------------------------------------------------------------------

def test_return_latches_when_the_battery_bounces_back(config):
    """A sagging pack recovers voltage the moment load drops. Without a latch the
    aircraft would turn around and resume the mission, repeatedly."""
    engine = SafetyEngine(config)
    low = engine.evaluate(make_context(config, battery_remaining=0.35))
    assert low.severity is Severity.RETURN

    recovered = engine.evaluate(make_context(config, battery_remaining=0.65))
    assert recovered.severity is Severity.RETURN
    assert recovered.latched is True


def test_warnings_do_not_latch(config):
    """Latching a WARN would ground the aircraft over one marginal GPS sample."""
    engine = SafetyEngine(config, rules=[const_rule("noisy", Severity.WARN)])
    assert engine.evaluate(make_context(config)).severity is Severity.WARN
    engine.rules = [silent_rule]
    assert engine.evaluate(make_context(config)).severity is Severity.NONE


def test_hold_does_not_latch(config):
    engine = SafetyEngine(config, rules=[const_rule("gust", Severity.HOLD)])
    assert engine.evaluate(make_context(config)).severity is Severity.HOLD
    engine.rules = [silent_rule]
    assert engine.evaluate(make_context(config)).severity is Severity.NONE


def test_latch_escalates_but_never_relaxes(config):
    engine = SafetyEngine(config, rules=[const_rule("x", Severity.RETURN)])
    assert engine.evaluate(make_context(config)).severity is Severity.RETURN
    engine.rules = [const_rule("y", Severity.LAND)]
    assert engine.evaluate(make_context(config)).severity is Severity.LAND
    engine.rules = [const_rule("z", Severity.RETURN)]
    assert engine.evaluate(make_context(config)).severity is Severity.LAND, "must not relax"


def test_latching_can_be_disabled(config):
    unlatched = SafetyConfig.from_dict({"behaviour": {"latch_failsafes": False}})
    engine = SafetyEngine(unlatched)
    assert engine.evaluate(make_context(unlatched, battery_remaining=0.35)).severity is Severity.RETURN
    assert engine.evaluate(make_context(unlatched, battery_remaining=0.65)).severity is Severity.NONE


def test_reset_clears_the_latch(config):
    engine = SafetyEngine(config)
    engine.evaluate(make_context(config, battery_remaining=0.35))
    engine.reset()
    assert engine.evaluate(make_context(config, battery_remaining=0.9)).severity is Severity.NONE


# ---------------------------------------------------------------------------
# Terminate policy
# ---------------------------------------------------------------------------

def test_terminate_is_downgraded_to_land_by_default(config):
    """Cutting the motors is the one action guaranteed to destroy the aircraft,
    so it is opt-in."""
    history = sustained_history(end=100.0, duration=0.8, accel_magnitude_g=0.05)
    verdict = SafetyEngine(config).evaluate(
        make_context(config, history=history, telemetry=history.latest)
    )
    assert verdict.severity is Severity.LAND
    assert any("terminate disabled" in t.reason for t in verdict.triggers)


def test_terminate_is_honoured_when_explicitly_enabled():
    enabled = SafetyConfig.from_dict({"behaviour": {"allow_terminate": True}})
    history = sustained_history(end=100.0, duration=0.8, accel_magnitude_g=0.05)
    verdict = SafetyEngine(enabled).evaluate(
        make_context(enabled, history=history, telemetry=history.latest)
    )
    assert verdict.severity is Severity.TERMINATE


# ---------------------------------------------------------------------------
# Combined realistic scenarios
# ---------------------------------------------------------------------------

def test_collision_while_low_battery_lands_rather_than_returning(config):
    """Impact says RETURN, critical battery says LAND. The more severe wins."""
    history = sustained_history(end=100.0, duration=0.6, accel_magnitude_g=7.0,
                                battery_remaining=0.15)
    verdict = SafetyEngine(config).evaluate(
        make_context(config, history=history, telemetry=history.latest)
    )
    assert verdict.severity is Severity.LAND
    rules = {t.rule for t in verdict.triggers}
    assert "impact_detected" in rules and "battery_critical" in rules


def test_outside_fence_with_no_position_lands(config):
    """Losing the fix outbound: we cannot navigate home, so landing beats returning."""
    verdict = SafetyEngine(config).evaluate(make_context(config, position=None))
    assert verdict.severity is Severity.LAND
    assert "position_unknown" in {t.rule for t in verdict.triggers}


def test_verdict_reasons_are_ordered_most_severe_first(config):
    rules = [const_rule("mild", Severity.WARN), const_rule("severe", Severity.LAND)]
    verdict = SafetyEngine(config, rules=rules).evaluate(make_context(config))
    assert "severe" in verdict.reasons()[0]


def test_a_still_firing_latched_trigger_reports_current_numbers(config):
    """A latched RETURN that keeps quoting the battery level it first fired on
    would mislead anyone watching the live display."""
    engine = SafetyEngine(config)
    first = engine.evaluate(make_context(config, battery_remaining=0.39))
    assert "39%" in first.primary.reason

    later = engine.evaluate(make_context(config, battery_remaining=0.31))
    battery = next(t for t in later.triggers if t.rule == "battery_low")
    assert "31%" in battery.reason, "latched trigger should refresh while still firing"


def test_a_trigger_that_stops_firing_keeps_its_original_account(config):
    """The reason the mission was abandoned does not change retroactively."""
    engine = SafetyEngine(config, rules=[const_rule("gust", Severity.RETURN)])
    engine.evaluate(make_context(config))
    engine.rules = [silent_rule]
    verdict = engine.evaluate(make_context(config))
    assert verdict.severity is Severity.RETURN
    assert any(t.rule == "gust" for t in verdict.triggers)

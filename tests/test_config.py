"""Configuration loading and validation.

The governing idea: in a safety config, a silently-ignored mistake is worse than
a loud failure. A typo must not leave a default quietly in force.
"""

from __future__ import annotations

import pytest

from dronegoto.config import ConfigError, SafetyConfig, load_default


def test_defaults_load_and_validate():
    config = SafetyConfig.from_dict({})
    assert config.battery.low_pct == 0.40
    assert config.altitude.max_altitude_m == 120.0


def test_packaged_default_yaml_matches_the_code_defaults():
    """If these drift apart, the annotated file people read stops describing
    what the code actually does."""
    assert load_default().to_dict() == SafetyConfig.from_dict({}).to_dict()


def test_partial_override_keeps_other_defaults():
    config = SafetyConfig.from_dict({"battery": {"low_pct": 0.45}})
    assert config.battery.low_pct == 0.45
    assert config.battery.critical_pct == 0.20


def test_raising_the_return_threshold_to_the_arm_minimum_is_refused():
    """Arming at exactly the return threshold would trigger a return on takeoff."""
    with pytest.raises(ConfigError, match="must exceed low_pct"):
        SafetyConfig.from_dict({"battery": {"low_pct": 0.5}})


@pytest.mark.parametrize(
    "data,fragment",
    [
        ({"geofenc": {}}, "unknown configuration section"),
        ({"altitude": {"max_altitiude_m": 200}}, "unknown key"),
        ({"battery": {"lowpct": 0.4}}, "unknown key"),
    ],
)
def test_typos_are_rejected_not_ignored(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        SafetyConfig.from_dict(data)


@pytest.mark.parametrize(
    "data,fragment",
    [
        ({"battery": {"low_pct": 0.1, "critical_pct": 0.2}}, "must increase"),
        ({"battery": {"arm_minimum_pct": 0.3}}, "must exceed low_pct"),
        ({"battery": {"low_pct": 1.5}}, "fraction in"),
        ({"geofence": {"soft_margin_m": 6000.0}}, "smaller than"),
        ({"altitude": {"max_altitude_m": 5.0}}, "must exceed"),
        ({"altitude": {"rth_altitude_m": 500.0}}, "must lie between"),
        ({"altitude": {"terrain_clearance_m": 200.0}}, "not below"),
        ({"altitude": {"clearance_tolerance_m": 50.0}}, "could never fire"),
        ({"link": {"loss_action": "panic"}}, "must be one of"),
        ({"link": {"blind_timeout_s": 1.0}}, "must exceed"),
        ({"gps": {"min_satellites": 2}}, "below 4"),
        ({"impact": {"accel_threshold_g": 0.5}}, "must exceed 1.0g"),
        ({"impact": {"accel_instant_g": 1.5}}, "must exceed"),
        ({"impact": {"accel_duration_s": 0.1}}, "two control ticks"),
        ({"impact": {"freefall_duration_s": 0.2}}, "two control ticks"),
        ({"timing": {"max_hover_time_s": 2000.0}}, "must be less than"),
        ({"timing": {"tick_interval_s": 0.0}}, "must be positive"),
        ({"flight": {"cruise_speed_ms": 30.0}}, "exceeds"),
        ({"terrain": {"samples": 1}}, "at least 2"),
    ],
)
def test_contradictions_are_rejected(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        SafetyConfig.from_dict(data)


def test_a_duration_gate_shorter_than_two_ticks_is_refused():
    """The bug this rule exists to prevent: a 0.1 s impact window at a 0.2 s tick
    can never hold two frames, so the rule would silently never fire."""
    with pytest.raises(ConfigError, match="never be confirmed"):
        SafetyConfig.from_dict({
            "impact": {"accel_duration_s": 0.3},
            "timing": {"tick_interval_s": 0.25},
        })


def test_all_errors_are_reported_together():
    with pytest.raises(ConfigError) as exc:
        SafetyConfig.from_dict({
            "battery": {"low_pct": 0.1, "critical_pct": 0.2},
            "gps": {"min_satellites": 1},
        })
    assert str(exc.value).count("  - ") >= 2


def test_loading_a_missing_file_is_an_error():
    with pytest.raises(ConfigError, match="not found"):
        SafetyConfig.load("/nonexistent/config.yaml")


def test_invalid_yaml_is_an_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("battery: {low_pct: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        SafetyConfig.load(path)


def test_round_trips_through_yaml(tmp_path):
    import yaml

    original = SafetyConfig.from_dict({"battery": {"low_pct": 0.45}})
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(original.to_dict()))
    assert SafetyConfig.load(path).to_dict() == original.to_dict()

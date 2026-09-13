"""The safety engine.

This module is **pure**: no network, no clock, no aircraft. Every rule is a
function of (telemetry, history, config, mission state) and returns either a
Trigger or None. That purity is the whole reason this system can be built
sim-first - all nineteen failsafes are exercised in unit tests, on the ground,
before any of them runs on something with propellers.

Two behaviours are borrowed from production autopilots:

* **Most severe wins.** PX4: "If multiple failsafes trigger, more severe action
  executes." Every rule is evaluated every tick and the maximum severity is
  taken - never first-match, which would let rule ordering hide an emergency.
* **Layered battery protection.** ArduPilot's low/critical pair plus PX4's
  "cannot safely return" check, so a fixed percentage is never the only defence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Callable, Iterable

from .config import SafetyConfig
from .geo import GeoPoint, haversine_m, project
from .telemetry import Telemetry, TelemetryBuffer


class Severity(IntEnum):
    """Escalating responses. Ordering is meaningful - the arbiter takes the max."""

    NONE = 0
    WARN = 1        # log it, keep flying
    HOLD = 2        # stop making progress, hover in place
    RETURN = 3      # climb to RTH altitude and fly home
    LAND = 4        # descend and land where we are
    TERMINATE = 5   # cut motors; only ever with behaviour.allow_terminate

    @property
    def label(self) -> str:
        return self.name


class MissionPhase(StrEnum):
    PREFLIGHT = "preflight"
    ARMING = "arming"
    TAKEOFF = "takeoff"
    CRUISE = "cruise"
    HOVER = "hover"
    RETURN = "return"
    LANDING = "landing"
    DONE = "done"
    ABORTED = "aborted"

    @property
    def in_flight(self) -> bool:
        return self in (
            MissionPhase.TAKEOFF,
            MissionPhase.CRUISE,
            MissionPhase.HOVER,
            MissionPhase.RETURN,
            MissionPhase.LANDING,
        )


@dataclass(frozen=True)
class Trigger:
    """One rule firing, with the numbers that caused it.

    `detail` is written verbatim to the black box so a flight can be explained
    afterwards without guessing at thresholds.
    """

    rule: str
    severity: Severity
    reason: str
    detail: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity.label}] {self.rule}: {self.reason}"


@dataclass(frozen=True)
class SafetyVerdict:
    severity: Severity
    triggers: tuple[Trigger, ...] = ()
    latched: bool = False

    @property
    def ok(self) -> bool:
        return self.severity <= Severity.WARN

    @property
    def primary(self) -> Trigger | None:
        """The most severe trigger; ties broken by rule order for determinism."""
        return max(self.triggers, key=lambda t: t.severity) if self.triggers else None

    def reasons(self) -> list[str]:
        return [str(t) for t in sorted(self.triggers, key=lambda t: -t.severity)]


@dataclass
class MissionState:
    """Everything the rules need to know that is not in a telemetry frame."""

    home: GeoPoint | None = None
    target: GeoPoint | None = None
    phase: MissionPhase = MissionPhase.PREFLIGHT
    started_at: float | None = None
    hover_started_at: float | None = None
    cruise_altitude_m: float | None = None
    max_ground_elevation_m: float | None = None
    ground_elevation_here_m: float | None = None
    battery_at_start: float | None = None
    distance_travelled_m: float = 0.0
    best_distance_m: float | None = None
    best_distance_at: float | None = None

    def elapsed(self, now: float) -> float:
        return 0.0 if self.started_at is None else max(0.0, now - self.started_at)

    def hover_elapsed(self, now: float) -> float:
        return 0.0 if self.hover_started_at is None else max(0.0, now - self.hover_started_at)


@dataclass
class SafetyContext:
    now: float
    telemetry: Telemetry
    history: TelemetryBuffer
    config: SafetyConfig
    state: MissionState

    @property
    def airborne(self) -> bool:
        return self.telemetry.in_air or self.state.phase.in_flight


Rule = Callable[[SafetyContext], Trigger | None]
RULES: list[Rule] = []


def rule(func: Rule) -> Rule:
    RULES.append(func)
    return func


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

@rule
def rule_battery_levels(ctx: SafetyContext) -> Trigger | None:
    """Layered thresholds. An unknown battery is treated as a low battery."""
    cfg = ctx.config.battery
    level = ctx.telemetry.battery_remaining

    if level is None:
        if not ctx.airborne:
            return None
        return Trigger(
            "battery_unknown",
            Severity.RETURN,
            "battery level is not being reported - assuming worst case",
            {"battery_remaining": None},
        )

    pct = level * 100.0
    if level <= cfg.emergency_pct:
        return Trigger(
            "battery_emergency",
            Severity.LAND,
            f"battery {pct:.0f}% at or below emergency threshold "
            f"{cfg.emergency_pct * 100:.0f}% - landing immediately",
            {"battery_pct": pct, "threshold_pct": cfg.emergency_pct * 100},
        )
    if level <= cfg.critical_pct:
        return Trigger(
            "battery_critical",
            Severity.LAND,
            f"battery {pct:.0f}% at or below critical threshold "
            f"{cfg.critical_pct * 100:.0f}% - landing",
            {"battery_pct": pct, "threshold_pct": cfg.critical_pct * 100},
        )
    if level <= cfg.low_pct:
        return Trigger(
            "battery_low",
            Severity.RETURN,
            f"battery {pct:.0f}% at or below low threshold "
            f"{cfg.low_pct * 100:.0f}% - returning home",
            {"battery_pct": pct, "threshold_pct": cfg.low_pct * 100},
        )
    return None


def estimate_return_cost_pct(ctx: SafetyContext) -> float | None:
    """Charge (in percent) needed to fly home from here, plus the configured reserve.

    Uses observed drain when there is enough flight history to measure it, which
    matters because the configured figure is a guess about an airframe, a payload
    and a wind that the aircraft is actually experiencing right now.
    """
    cfg = ctx.config.battery
    state = ctx.state
    distance_home_m = ctx.telemetry.distance_to(state.home)
    if distance_home_m is None:
        return None

    drain_per_km = cfg.cruise_drain_pct_per_km
    if (
        cfg.adaptive_estimation
        and state.battery_at_start is not None
        and ctx.telemetry.battery_remaining is not None
        and state.distance_travelled_m > 250.0
    ):
        used_pct = (state.battery_at_start - ctx.telemetry.battery_remaining) * 100.0
        if used_pct > 0.5:
            observed = used_pct / (state.distance_travelled_m / 1000.0)
            # Never let the measurement talk us into a more optimistic figure than
            # configured; adaptivity may only tighten the margin, not relax it.
            drain_per_km = max(drain_per_km, observed)

    cruise_cost = (distance_home_m / 1000.0) * drain_per_km
    # Descent and landing are not free.
    landing_cost = cfg.hover_drain_pct_per_min * 0.5
    return cruise_cost + landing_cost + cfg.reserve_pct * 100.0


@rule
def rule_return_energy(ctx: SafetyContext) -> Trigger | None:
    """Return while the charge to get home still exists.

    PX4 calls this COM_FLTT_LOW_ACT. At distance it fires long before any fixed
    percentage would, which is precisely when a fixed percentage is useless.
    """
    if not ctx.airborne or ctx.state.phase in (MissionPhase.RETURN, MissionPhase.LANDING):
        return None
    level = ctx.telemetry.battery_remaining
    if level is None:
        return None
    needed = estimate_return_cost_pct(ctx)
    if needed is None:
        return None
    available = level * 100.0
    if available <= needed:
        distance_home = ctx.telemetry.distance_to(ctx.state.home) or 0.0
        return Trigger(
            "return_energy_margin",
            Severity.RETURN,
            f"battery {available:.0f}% no longer covers the {distance_home / 1000:.2f} km "
            f"flight home plus reserve (needs {needed:.0f}%)",
            {
                "battery_pct": available,
                "needed_pct": needed,
                "distance_home_m": distance_home,
            },
        )
    return None


# ---------------------------------------------------------------------------
# Impact and loss of control
# ---------------------------------------------------------------------------

@rule
def rule_impact(ctx: SafetyContext) -> Trigger | None:
    """Collision detection from an accelerometer spike or an attitude excursion."""
    cfg = ctx.config.impact
    if not cfg.enabled or not ctx.airborne:
        return None

    # Single-frame path: telemetry carries peak acceleration since the previous
    # frame, so one reading this high is a collision, not a noisy sample. A real
    # impact lasts milliseconds - far less than one control tick - so requiring
    # confirmation across frames would miss it entirely.
    peak_now = ctx.telemetry.accel_magnitude_g
    if peak_now is not None and peak_now >= cfg.accel_instant_g:
        return Trigger(
            "impact_detected",
            Severity.RETURN,
            f"impact detected: {peak_now:.1f}g peak (instant threshold "
            f"{cfg.accel_instant_g:.1f}g) - returning home",
            {"peak_g": peak_now, "threshold_g": cfg.accel_instant_g, "path": "instant"},
        )

    threshold = cfg.accel_threshold_g
    if ctx.history.sustained(
        ctx.now,
        cfg.accel_duration_s,
        lambda f: f.accel_magnitude_g is not None and f.accel_magnitude_g >= threshold,
    ):
        peak = max(
            (f.accel_magnitude_g or 0.0) for f in ctx.history.since(ctx.now, cfg.accel_duration_s)
        )
        return Trigger(
            "impact_detected",
            Severity.RETURN,
            f"impact detected: {peak:.1f}g sustained for {cfg.accel_duration_s:.2f}s "
            f"(threshold {threshold:.1f}g) - returning home",
            {"peak_g": peak, "threshold_g": threshold, "path": "sustained"},
        )

    attitude = ctx.telemetry.attitude_deg
    if attitude is not None:
        roll, pitch, _ = attitude
        worst = max(abs(roll), abs(pitch))
        if worst >= cfg.max_attitude_deg:
            return Trigger(
                "attitude_excursion",
                Severity.RETURN,
                f"attitude excursion {worst:.0f}deg exceeds {cfg.max_attitude_deg:.0f}deg - "
                f"possible collision or loss of control",
                {"roll_deg": roll, "pitch_deg": pitch, "limit_deg": cfg.max_attitude_deg},
            )
    return None


@rule
def rule_freefall(ctx: SafetyContext) -> Trigger | None:
    """Sustained near-zero g means the aircraft is falling, not flying."""
    cfg = ctx.config.impact
    if not cfg.enabled or not ctx.airborne:
        return None
    threshold = cfg.freefall_threshold_g
    if ctx.history.sustained(
        ctx.now,
        cfg.freefall_duration_s,
        lambda f: f.accel_magnitude_g is not None and f.accel_magnitude_g <= threshold,
    ):
        return Trigger(
            "freefall_detected",
            Severity.TERMINATE,
            f"freefall: acceleration below {threshold:.1f}g for "
            f"{cfg.freefall_duration_s:.1f}s - aircraft is not flying",
            {"threshold_g": threshold, "duration_s": cfg.freefall_duration_s},
        )
    return None


@rule
def rule_uncommanded_descent(ctx: SafetyContext) -> Trigger | None:
    """Losing height while cruising or hovering, when nothing asked it to.

    Catches a sagging aircraft before it becomes a landing: an overloaded
    airframe, a failing motor, or a downdraught the controller is not winning.
    """
    if ctx.state.phase not in (MissionPhase.CRUISE, MissionPhase.HOVER):
        return None
    climb = ctx.telemetry.climb_rate_ms
    if climb is None:
        return None
    limit = ctx.config.flight.max_uncommanded_descent_ms
    if climb <= -limit:
        return Trigger(
            "uncommanded_descent",
            Severity.HOLD,
            f"descending at {abs(climb):.1f} m/s during {ctx.state.phase.value} "
            f"(limit {limit:.1f} m/s)",
            {"climb_rate_ms": climb, "limit_ms": limit},
        )
    return None


# ---------------------------------------------------------------------------
# Geofence and altitude
# ---------------------------------------------------------------------------

@rule
def rule_geofence(ctx: SafetyContext) -> Trigger | None:
    """Predictive cylindrical fence.

    PX4's documentation is explicit that its own fence acts only once the vehicle
    "has breached the geofence". Reacting after the fact wastes the very margin
    the fence exists to protect, so this checks three things in descending
    severity: already outside, inside the soft ring, and projected to breach.
    """
    cfg = ctx.config.geofence
    if not cfg.enabled or not ctx.airborne:
        return None
    position = ctx.telemetry.position
    home = ctx.state.home
    if position is None or home is None:
        return None

    distance = haversine_m(position, home)
    soft_radius = cfg.max_radius_m - cfg.soft_margin_m

    if distance >= cfg.max_radius_m:
        return Trigger(
            "geofence_breach",
            Severity.RETURN,
            f"outside geofence: {distance:.0f} m from home exceeds "
            f"{cfg.max_radius_m:.0f} m",
            {"distance_m": distance, "radius_m": cfg.max_radius_m},
        )
    if distance >= soft_radius:
        return Trigger(
            "geofence_soft",
            Severity.RETURN,
            f"inside geofence margin: {distance:.0f} m from home, turning back at "
            f"{soft_radius:.0f} m",
            {"distance_m": distance, "soft_radius_m": soft_radius},
        )

    speed = ctx.telemetry.groundspeed_ms
    velocity = ctx.telemetry.velocity_ned_ms
    if speed and speed > 0.1 and velocity is not None and cfg.lookahead_s > 0:
        north, east, _ = velocity
        heading = math.degrees(math.atan2(east, north)) % 360.0
        projected = project(position, heading, speed * cfg.lookahead_s)
        projected_distance = haversine_m(projected, home)
        if projected_distance >= soft_radius:
            return Trigger(
                "geofence_predicted",
                Severity.RETURN,
                f"current track reaches {projected_distance:.0f} m from home within "
                f"{cfg.lookahead_s:.0f}s (limit {soft_radius:.0f} m) - turning back early",
                {
                    "distance_m": distance,
                    "projected_m": projected_distance,
                    "lookahead_s": cfg.lookahead_s,
                },
            )
    return None


@rule
def rule_altitude_ceiling(ctx: SafetyContext) -> Trigger | None:
    """Climbing above the configured ceiling, usually the local legal limit.

    Two bands: just over is a hold-and-descend, well over means something is
    wrong with altitude control and the mission ends.
    """
    cfg = ctx.config.altitude
    altitude = ctx.telemetry.altitude_rel_m
    if altitude is None or not ctx.airborne:
        return None
    if altitude > cfg.max_altitude_m + 10.0:
        return Trigger(
            "altitude_ceiling_exceeded",
            Severity.RETURN,
            f"altitude {altitude:.0f} m is more than 10 m above the "
            f"{cfg.max_altitude_m:.0f} m ceiling",
            {"altitude_m": altitude, "ceiling_m": cfg.max_altitude_m},
        )
    if altitude > cfg.max_altitude_m:
        return Trigger(
            "altitude_ceiling",
            Severity.HOLD,
            f"altitude {altitude:.0f} m above ceiling {cfg.max_altitude_m:.0f} m - descending",
            {"altitude_m": altitude, "ceiling_m": cfg.max_altitude_m},
        )
    return None


@rule
def rule_terrain_clearance(ctx: SafetyContext) -> Trigger | None:
    """Height above the ground beneath us, not above the launch point.

    Flying a fixed *relative* altitude toward rising ground is the classic way to
    lose an aircraft, and it is invisible to every altitude check that measures
    from home.
    """
    if ctx.state.phase != MissionPhase.CRUISE:
        return None
    cfg = ctx.config.altitude
    ground = ctx.state.ground_elevation_here_m
    altitude_amsl = ctx.telemetry.altitude_amsl_m
    if ground is None or altitude_amsl is None:
        return None

    clearance = altitude_amsl - ground
    if clearance < cfg.terrain_clearance_m - cfg.clearance_tolerance_m:
        needed_amsl = ground + cfg.terrain_clearance_m
        severity = Severity.HOLD
        note = "climbing"
        if ctx.state.home is not None and needed_amsl - (ctx.state.max_ground_elevation_m or ground) > cfg.max_altitude_m:
            severity = Severity.RETURN
            note = "cannot climb clear within the ceiling"
        return Trigger(
            "terrain_clearance",
            severity,
            f"only {clearance:.0f} m above ground (minimum {cfg.terrain_clearance_m:.0f} m) - {note}",
            {"clearance_m": clearance, "minimum_m": cfg.terrain_clearance_m, "ground_m": ground},
        )
    return None


# ---------------------------------------------------------------------------
# Navigation integrity
# ---------------------------------------------------------------------------

@rule
def rule_position_unknown(ctx: SafetyContext) -> Trigger | None:
    """No position fix at all. Landing here beats flying blind toward a guess."""
    if not ctx.airborne:
        return None
    if ctx.telemetry.position is None:
        return Trigger(
            "position_unknown",
            Severity.LAND,
            "no position fix - cannot navigate home, landing",
            {},
        )
    return None


@rule
def rule_gps_quality(ctx: SafetyContext) -> Trigger | None:
    """Degraded or lost satellite fix, escalating with how long it persists."""
    cfg = ctx.config.gps
    if not ctx.airborne:
        return None
    telemetry = ctx.telemetry

    if telemetry.has_fix is False:
        if ctx.history.sustained(ctx.now, cfg.loss_timeout_s, lambda f: f.has_fix is False):
            return Trigger(
                "gps_lost",
                Severity.LAND,
                f"GPS fix lost for {cfg.loss_timeout_s:.0f}s - landing",
                {"timeout_s": cfg.loss_timeout_s},
            )
        return Trigger("gps_no_fix", Severity.HOLD, "GPS fix lost - holding position", {})

    satellites, hdop = telemetry.satellites, telemetry.hdop
    degraded = (satellites is not None and satellites < cfg.min_satellites) or (
        hdop is not None and hdop > cfg.max_hdop
    )
    if not degraded:
        return None

    def is_degraded(f: Telemetry) -> bool:
        return (f.satellites is not None and f.satellites < cfg.min_satellites) or (
            f.hdop is not None and f.hdop > cfg.max_hdop
        )

    if ctx.history.sustained(ctx.now, cfg.degraded_timeout_s, is_degraded):
        return Trigger(
            "gps_degraded",
            Severity.RETURN,
            f"GPS degraded for {cfg.degraded_timeout_s:.0f}s "
            f"(sats={satellites}, hdop={hdop}) - returning home",
            {"satellites": satellites, "hdop": hdop},
        )
    return Trigger(
        "gps_degraded_warning",
        Severity.WARN,
        f"GPS quality marginal (sats={satellites}, hdop={hdop})",
        {"satellites": satellites, "hdop": hdop},
    )


@rule
def rule_telemetry_stale(ctx: SafetyContext) -> Trigger | None:
    """Absence of telemetry is itself an alarm.

    A monitor that only reacts to the data it receives goes quiet at exactly the
    moment it is most needed. This rule is the reason the engine is driven by an
    external clock rather than by frame arrival.
    """
    if not ctx.airborne:
        return None
    age = ctx.telemetry.age_s(ctx.now)
    limit = ctx.config.link.telemetry_stale_s
    if age > limit:
        return Trigger(
            "telemetry_stale",
            Severity.RETURN,
            f"no telemetry for {age:.1f}s (limit {limit:.1f}s) - flying blind",
            {"age_s": age, "limit_s": limit},
        )
    return None


@rule
def rule_link_loss(ctx: SafetyContext) -> Trigger | None:
    """Control link gone. The response is policy, not physics, so it is configurable:
    an onboard-executed mission may legitimately continue without us."""
    cfg = ctx.config.link
    if not ctx.airborne or ctx.telemetry.link_ok is not False:
        return None
    if not ctx.history.sustained(ctx.now, cfg.loss_timeout_s, lambda f: f.link_ok is False):
        return None
    severity = {
        "continue": Severity.WARN,
        "hold": Severity.HOLD,
        "return": Severity.RETURN,
        "land": Severity.LAND,
    }[cfg.loss_action]
    return Trigger(
        "link_loss",
        severity,
        f"control link lost for {cfg.loss_timeout_s:.0f}s - configured action "
        f"is '{cfg.loss_action}'",
        {"timeout_s": cfg.loss_timeout_s, "action": cfg.loss_action},
    )


@rule
def rule_no_progress(ctx: SafetyContext) -> Trigger | None:
    """The aircraft is commanded to a target but is not getting closer.

    Catches the cases no single sensor reports: fighting a headwind it cannot
    win, a rejected command, a stuck mission upload.
    """
    cfg = ctx.config.timing
    state = ctx.state
    if state.phase != MissionPhase.CRUISE or state.best_distance_at is None:
        return None
    distance = ctx.telemetry.distance_to(state.target)
    if distance is None or state.best_distance_m is None:
        return None

    stalled_for = ctx.now - state.best_distance_at
    if stalled_for >= cfg.no_progress_timeout_s:
        return Trigger(
            "no_progress",
            Severity.RETURN,
            f"no closure on target for {stalled_for:.0f}s "
            f"(still {distance:.0f} m away) - returning home",
            {"stalled_s": stalled_for, "distance_m": distance},
        )
    return None


# ---------------------------------------------------------------------------
# Budgets and environment
# ---------------------------------------------------------------------------

@rule
def rule_mission_timeout(ctx: SafetyContext) -> Trigger | None:
    """Airborne longer than the flight-time budget allows."""
    cfg = ctx.config.timing
    if not ctx.airborne or ctx.state.started_at is None:
        return None
    elapsed = ctx.state.elapsed(ctx.now)
    if elapsed >= cfg.max_flight_time_s * 1.5:
        return Trigger(
            "mission_timeout_hard",
            Severity.LAND,
            f"airborne {elapsed / 60:.1f} min, far past the "
            f"{cfg.max_flight_time_s / 60:.0f} min limit - landing",
            {"elapsed_s": elapsed, "limit_s": cfg.max_flight_time_s},
        )
    if elapsed >= cfg.max_flight_time_s:
        return Trigger(
            "mission_timeout",
            Severity.RETURN,
            f"airborne {elapsed / 60:.1f} min, past the "
            f"{cfg.max_flight_time_s / 60:.0f} min limit - returning home",
            {"elapsed_s": elapsed, "limit_s": cfg.max_flight_time_s},
        )
    return None


@rule
def rule_hover_timeout(ctx: SafetyContext) -> Trigger | None:
    """Loitering at the destination past the hover budget."""
    cfg = ctx.config.timing
    if ctx.state.phase != MissionPhase.HOVER or ctx.state.hover_started_at is None:
        return None
    hovered = ctx.state.hover_elapsed(ctx.now)
    if hovered >= cfg.max_hover_time_s:
        return Trigger(
            "hover_timeout",
            Severity.RETURN,
            f"hovered {hovered / 60:.1f} min at the destination, limit is "
            f"{cfg.max_hover_time_s / 60:.0f} min - returning home",
            {"hovered_s": hovered, "limit_s": cfg.max_hover_time_s},
        )
    return None


@rule
def rule_wind(ctx: SafetyContext) -> Trigger | None:
    """Wind at or above the airframe's limit - come home while it still can."""
    if not ctx.airborne:
        return None
    wind = ctx.telemetry.wind_speed_ms
    limit = ctx.config.flight.max_wind_ms
    if wind is not None and wind >= limit:
        return Trigger(
            "wind_speed",
            Severity.RETURN,
            f"wind {wind:.1f} m/s at or above limit {limit:.1f} m/s - returning home",
            {"wind_ms": wind, "limit_ms": limit},
        )
    return None


# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------

class SafetyEngine:
    """Evaluates every rule each tick and arbitrates between them.

    Holds the only mutable state in the safety path: the latch. Once a return is
    triggered it stays triggered, so a battery reading that bounces back above
    the threshold - or a collision spike that has passed - cannot talk the
    aircraft into carrying on.
    """

    def __init__(self, config: SafetyConfig, rules: Iterable[Rule] | None = None) -> None:
        self.config = config
        self.rules = list(rules if rules is not None else RULES)
        self._latched: Severity = Severity.NONE
        self._latched_triggers: list[Trigger] = []

    @property
    def latched_severity(self) -> Severity:
        return self._latched

    @property
    def latched_triggers(self) -> tuple[Trigger, ...]:
        """The triggers that caused the current latch, for reporting."""
        return tuple(self._latched_triggers)

    def reset(self) -> None:
        self._latched = Severity.NONE
        self._latched_triggers = []

    def evaluate(self, ctx: SafetyContext) -> SafetyVerdict:
        triggers = [t for t in (r(ctx) for r in self.rules) if t is not None]
        severity = max((t.severity for t in triggers), default=Severity.NONE)

        # Only latch actions that abandon the mission. WARN and HOLD are
        # transient by design - latching them would ground the aircraft over a
        # single marginal GPS sample.
        latched = False
        if self.config.behaviour.latch_failsafes:
            if severity >= Severity.RETURN:
                if severity > self._latched:
                    self._latched = severity
                self._latched_triggers = _merge_triggers(self._latched_triggers, triggers)
            if self._latched > severity:
                severity = self._latched
                latched = True
            if self._latched >= Severity.RETURN:
                triggers = _merge_triggers(self._latched_triggers, triggers)

        severity, triggers = self._apply_terminate_policy(severity, triggers)
        return SafetyVerdict(severity=severity, triggers=tuple(triggers), latched=latched)

    def _apply_terminate_policy(
        self, severity: Severity, triggers: list[Trigger]
    ) -> tuple[Severity, list[Trigger]]:
        """Downgrade TERMINATE unless it has been explicitly enabled.

        Cutting the motors is the only action here that is guaranteed to destroy
        the aircraft, so it is opt-in and everything else degrades to a landing.
        """
        if severity < Severity.TERMINATE or self.config.behaviour.allow_terminate:
            return severity, triggers
        downgraded = [
            Trigger(
                t.rule,
                Severity.LAND,
                f"{t.reason} (terminate disabled by config - landing instead)",
                t.detail,
            )
            if t.severity == Severity.TERMINATE
            else t
            for t in triggers
        ]
        self._latched = min(self._latched, Severity.LAND)
        return Severity.LAND, downgraded


def _merge_triggers(existing: list[Trigger], new: list[Trigger]) -> list[Trigger]:
    """Union by rule name, keeping the most severe instance of each.

    Ties go to the newer instance so a still-firing rule reports current numbers
    rather than the reading it first fired on - otherwise a latched trigger keeps
    quoting a battery percentage from minutes ago. A rule that has stopped firing
    keeps its original text, which is the correct historical record of why the
    mission was abandoned.
    """
    merged: dict[str, Trigger] = {t.rule: t for t in existing}
    for t in new:
        current = merged.get(t.rule)
        if current is None or t.severity >= current.severity:
            merged[t.rule] = t
    return list(merged.values())


def update_progress(state: MissionState, telemetry: Telemetry, now: float) -> None:
    """Maintain the closure tracking that `rule_no_progress` reads.

    Kept beside the rules because the two must agree on what "progress" means.
    """
    distance = telemetry.distance_to(state.target)
    if distance is None:
        return
    if state.best_distance_m is None or distance < state.best_distance_m - _CLOSURE_EPSILON:
        state.best_distance_m = distance
        state.best_distance_at = now
    elif state.best_distance_at is None:
        state.best_distance_at = now


_CLOSURE_EPSILON = 1.0

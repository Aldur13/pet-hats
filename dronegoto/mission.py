"""The mission controller.

Wires the safety engine to an aircraft. The controller owns phase progression;
the safety engine owns whether progression is allowed to continue. That split is
deliberate - no failsafe decision is ever made inside flight logic, so none can
be accidentally bypassed by a new flight phase.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from .backends.base import BackendError, DroneBackend
from .blackbox import BlackBox
from .config import SafetyConfig
from .elevation import TerrainProfile
from .geo import GeoPoint, haversine_m
from .preview import MissionPlan
from .safety import (
    MissionPhase,
    MissionState,
    SafetyContext,
    SafetyEngine,
    SafetyVerdict,
    Severity,
    Trigger,
    update_progress,
)
from .telemetry import Telemetry, TelemetryBuffer

log = logging.getLogger(__name__)


class MissionOutcome(StrEnum):
    COMPLETED = "completed"
    RETURNED = "returned"
    LANDED_OUT = "landed_out"
    TERMINATED = "terminated"
    LOST_CONTACT = "lost_contact"
    PREFLIGHT_FAILED = "preflight_failed"
    ERROR = "error"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        dots = "." * max(1, 22 - len(self.name))
        return f"  {'OK  ' if self.passed else 'FAIL'}  {self.name} {dots} {self.detail}"


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def render(self) -> str:
        lines = ["PREFLIGHT", ""]
        lines.extend(check.render() for check in self.checks)
        lines.append("")
        passed = sum(1 for check in self.checks if check.passed)
        lines.append(
            f"  {passed}/{len(self.checks)} checks passed - "
            + ("ARMING PERMITTED" if self.passed else "ARMING BLOCKED")
        )
        return "\n".join(lines)


@dataclass
class MissionResult:
    outcome: MissionOutcome
    reason: str
    preflight: PreflightReport | None = None
    triggers: tuple[Trigger, ...] = ()
    duration_s: float = 0.0
    reached_target: bool = False
    final_position: GeoPoint | None = None
    distance_from_home_m: float | None = None
    final_battery: float | None = None

    @property
    def safe(self) -> bool:
        """Did the aircraft finish somewhere we consider safe?"""
        return self.outcome in (
            MissionOutcome.COMPLETED,
            MissionOutcome.RETURNED,
            MissionOutcome.LANDED_OUT,
            MissionOutcome.PREFLIGHT_FAILED,
            # LOST_CONTACT is a safe *supervisor* outcome, not a safe aircraft:
            # we commanded a return, then went blind. The aircraft is flying the
            # failsafes written to it during preflight, which is exactly why they
            # are uploaded. Continuing to supervise on stale data would be worse.
            MissionOutcome.LOST_CONTACT,
        )

    def render(self) -> str:
        lines = ["MISSION RESULT", "", f"  outcome .......... {self.outcome.value.upper()}"]
        lines.append(f"  reason ........... {self.reason}")
        lines.append(f"  reached target ... {'yes' if self.reached_target else 'no'}")
        lines.append(f"  duration ......... {self.duration_s:.0f}s")
        if self.final_battery is not None:
            lines.append(f"  battery left ..... {self.final_battery * 100:.0f}%")
        if self.distance_from_home_m is not None:
            lines.append(f"  ended ............ {self.distance_from_home_m:.0f} m from home")
        if self.triggers:
            lines.append("")
            lines.append("  safety triggers:")
            for trigger in sorted(self.triggers, key=lambda t: -t.severity):
                lines.append(f"    {trigger}")
        return "\n".join(lines)


TickHook = Callable[[Telemetry, MissionState, SafetyVerdict], Awaitable[None] | None]


class MissionController:
    """Flies one mission, under continuous supervision."""

    def __init__(
        self,
        backend: DroneBackend,
        config: SafetyConfig,
        blackbox: BlackBox | None = None,
        on_tick: TickHook | None = None,
    ) -> None:
        self.backend = backend
        self.config = config
        self.blackbox = blackbox
        self.on_tick = on_tick
        self.engine = SafetyEngine(config)
        self.state = MissionState()
        self.history = TelemetryBuffer()
        self._held = False
        self._reached_target = False
        self._interrupted = False
        self._was_airborne = False
        self._last_position: GeoPoint | None = None
        self._terrain_climb_target_m: float | None = None

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------
    async def preflight(self, plan: MissionPlan) -> PreflightReport:
        checks: list[Check] = []
        telemetry = await self.backend.read_telemetry()

        age = telemetry.age_s(self.backend.now())
        checks.append(
            Check("telemetry", age <= self.config.link.telemetry_stale_s,
                  f"{age:.1f}s old")
        )

        home = await self.backend.home_position()
        checks.append(Check("home position", home is not None, str(home) if home else "not set"))

        has_fix = telemetry.has_fix is True
        satellites = telemetry.satellites
        enough_sats = satellites is not None and satellites >= self.config.gps.min_satellites
        # HDOP is checked when reported. Some backends (MAVSDK) never report it;
        # a 3D fix with enough satellites is still a valid basis for arming.
        hdop_ok = telemetry.hdop is None or telemetry.hdop <= self.config.gps.max_hdop
        hdop_text = "n/a" if telemetry.hdop is None else f"{telemetry.hdop}"
        checks.append(
            Check("gps fix", has_fix and enough_sats and hdop_ok,
                  f"{satellites} sats, hdop {hdop_text}")
        )

        battery = telemetry.battery_remaining
        battery_ok = battery is not None and battery >= self.config.battery.arm_minimum_pct
        checks.append(
            Check("battery", battery_ok,
                  f"{battery * 100:.0f}%" if battery is not None else "unknown")
        )

        checks.append(
            Check("mission plan", plan.go,
                  "GO" if plan.go else f"{len(plan.blockers)} blocker(s): {plan.blockers[0]}")
        )

        checks.append(
            Check("round-trip energy",
                  plan.battery_margin_pct is not None and plan.battery_margin_pct >= 0,
                  f"needs {plan.battery_required_pct:.0f}%, "
                  f"margin {plan.battery_margin_pct:+.0f}%"
                  if plan.battery_margin_pct is not None else "battery level unknown")
        )

        clearance_ok = True
        clearance_detail = "no elevation data"
        if plan.terrain is not None and plan.cruise_altitude_amsl_m is not None:
            clearance = plan.cruise_altitude_amsl_m - plan.terrain.max_elevation_m
            clearance_ok = clearance >= self.config.altitude.terrain_clearance_m
            clearance_detail = f"{clearance:.0f} m over highest ground"
        elif self.config.terrain.enabled and self.config.terrain.required:
            clearance_ok = False
        checks.append(Check("terrain clearance", clearance_ok, clearance_detail))

        # Defence in depth: these must genuinely land on the aircraft, so a
        # backend that cannot store them is reported rather than assumed.
        if self.config.behaviour.upload_autopilot_fence:
            ok, detail = await self._try_upload_fence(home)
            checks.append(Check("onboard geofence", ok, detail))
        if self.config.behaviour.write_autopilot_params:
            ok, detail = await self._try_write_params()
            checks.append(Check("onboard failsafes", ok, detail))

        report = PreflightReport(tuple(checks))
        if self.blackbox:
            self.blackbox.write(
                "preflight",
                passed=report.passed,
                checks=[{"name": c.name, "passed": c.passed, "detail": c.detail} for c in report.checks],
            )
        return report

    async def _try_upload_fence(self, home: GeoPoint | None) -> tuple[bool, str]:
        if home is None:
            return False, "no home position"
        if not self.backend.capabilities.supports_geofence_upload:
            return False, f"{self.backend.capabilities.name} cannot store a fence onboard"
        try:
            await self.backend.upload_geofence(
                home, self.config.geofence.max_radius_m, self.config.altitude.max_altitude_m
            )
        except BackendError as exc:
            return False, str(exc)
        return True, f"r={self.config.geofence.max_radius_m:.0f}m, alt={self.config.altitude.max_altitude_m:.0f}m"

    async def _try_write_params(self) -> tuple[bool, str]:
        if not self.backend.capabilities.supports_failsafe_params:
            return False, f"{self.backend.capabilities.name} cannot store failsafe params"
        params = {
            "battery_low_pct": self.config.battery.low_pct * 100,
            "battery_critical_pct": self.config.battery.critical_pct * 100,
            "rtl_altitude_m": self.config.altitude.rth_altitude_m,
            "max_flight_time_s": self.config.timing.max_flight_time_s,
        }
        try:
            await self.backend.write_failsafe_params(params)
        except BackendError as exc:
            return False, str(exc)
        return True, f"{len(params)} parameters written"

    # ------------------------------------------------------------------
    # Flight
    # ------------------------------------------------------------------
    async def run(
        self,
        plan: MissionPlan,
        terrain: TerrainProfile | None = None,
        report: PreflightReport | None = None,
    ) -> MissionResult:
        """Fly the plan. Pass `report` to reuse a preflight already run for this
        plan - otherwise the fence upload and parameter writes happen twice."""
        terrain = terrain if terrain is not None else plan.terrain
        self._reset()
        if report is None:
            report = await self.preflight(plan)
        if not report.passed and self.config.behaviour.require_preflight_pass:
            return MissionResult(
                outcome=MissionOutcome.PREFLIGHT_FAILED,
                reason="; ".join(c.name for c in report.failures),
                preflight=report,
            )

        home = await self.backend.home_position() or plan.home
        self.state = MissionState(
            home=home,
            target=plan.target,
            phase=MissionPhase.ARMING,
            cruise_altitude_m=min(plan.cruise_altitude_rel_m, self.config.altitude.max_altitude_m),
        )

        try:
            return await self._fly(plan, terrain, report)
        except BackendError as exc:
            log.exception("backend failure")
            await self._emergency_land()
            return MissionResult(
                outcome=MissionOutcome.ERROR, reason=f"backend failure: {exc}", preflight=report
            )

    async def _fly(
        self, plan: MissionPlan, terrain: TerrainProfile | None, report: PreflightReport
    ) -> MissionResult:
        backend, config, state = self.backend, self.config, self.state
        dt = config.timing.tick_interval_s

        await backend.arm()
        self._command("arm")
        telemetry = await backend.read_telemetry()
        state.started_at = backend.now()
        state.battery_at_start = telemetry.battery_remaining
        self._last_position = telemetry.position

        state.phase = MissionPhase.TAKEOFF
        await backend.takeoff(state.cruise_altitude_m or config.altitude.min_altitude_m)
        self._command("takeoff", f"{state.cruise_altitude_m:.0f}m")

        # Hard ceiling on iterations so a misbehaving backend cannot spin forever.
        max_ticks = int(config.timing.max_flight_time_s * 3.0 / dt)
        for _ in range(max_ticks):
            await backend.step(dt)
            telemetry = await backend.read_telemetry()
            now = backend.now()
            self.history.append(telemetry)
            self._update_state(telemetry, terrain, now)

            verdict = self.engine.evaluate(
                SafetyContext(
                    now=now,
                    telemetry=telemetry,
                    history=self.history,
                    config=config,
                    state=state,
                )
            )
            if self.blackbox:
                self.blackbox.telemetry(telemetry, state.phase.value)
                self.blackbox.verdict(now, verdict)
            if self.on_tick:
                result = self.on_tick(telemetry, state, verdict)
                if result is not None:
                    await result

            await self._dispatch(verdict, plan)
            if verdict.severity < Severity.HOLD:
                await self._advance(telemetry, plan, now)

            if self._is_down(telemetry):
                break

            # Going permanently blind is a terminal condition for the supervisor.
            # We have already commanded a return; spinning on a frozen frame
            # until the tick budget runs out would report an error for what is
            # actually a handled, understood situation.
            age = telemetry.age_s(now)
            if age >= config.link.blind_timeout_s:
                return self._result(
                    MissionOutcome.LOST_CONTACT,
                    f"no telemetry for {age:.0f}s - handing over to the aircraft's "
                    f"onboard failsafes",
                    telemetry, report,
                )
        else:
            return self._result(
                MissionOutcome.ERROR, "flight exceeded the controller's tick budget",
                telemetry, report,
            )

        return self._result(self._outcome(), self._reason(), telemetry, report)

    def _reset(self) -> None:
        """Clear everything a previous mission left behind."""
        self.engine.reset()
        self.history.clear()
        self._held = False
        self._reached_target = False
        self._was_airborne = False
        self._interrupted = False
        self._last_position = None
        self._terrain_climb_target_m = None

    def _update_state(
        self, telemetry: Telemetry, terrain: TerrainProfile | None, now: float
    ) -> None:
        state = self.state
        if telemetry.in_air:
            self._was_airborne = True
        if (
            state.home_elevation_m is None
            and telemetry.altitude_amsl_m is not None
            and telemetry.altitude_rel_m is not None
        ):
            # Taken from the aircraft rather than the elevation service, so the
            # terrain rule converts against what the autopilot actually believes.
            state.home_elevation_m = telemetry.altitude_amsl_m - telemetry.altitude_rel_m
        if telemetry.position is not None:
            if self._last_position is not None:
                state.distance_travelled_m += haversine_m(self._last_position, telemetry.position)
            self._last_position = telemetry.position
            if terrain is not None:
                state.ground_elevation_here_m = terrain.elevation_at(telemetry.position)
                state.max_ground_elevation_m = terrain.max_elevation_m
        update_progress(state, telemetry, now, self.config.timing.no_progress_min_closure_m)

    async def _dispatch(self, verdict: SafetyVerdict, plan: MissionPlan) -> None:
        """Translate a verdict into a command, without re-issuing it every tick."""
        state = self.state
        severity = verdict.severity

        if severity >= Severity.TERMINATE:
            if state.phase is not MissionPhase.ABORTED:
                state.phase = MissionPhase.ABORTED
                self._interrupted = True
                await self.backend.terminate()
                self._command("terminate", verdict.primary.rule if verdict.primary else "")
            return

        if severity >= Severity.LAND:
            if state.phase is not MissionPhase.LANDING:
                state.phase = MissionPhase.LANDING
                self._interrupted = True
                await self.backend.land()
                self._command("land", verdict.primary.rule if verdict.primary else "")
            return

        if severity >= Severity.RETURN:
            if state.phase not in (MissionPhase.RETURN, MissionPhase.LANDING):
                # Only an interruption if the mission was not already heading home
                # of its own accord - a low battery during a planned return did
                # not divert anything.
                self._interrupted = True
                state.phase = MissionPhase.RETURN
                await self.backend.return_to_home(self.config.altitude.rth_altitude_m)
                self._command("return", verdict.primary.rule if verdict.primary else "")
            return

        if severity >= Severity.HOLD:
            if await self._climb_clear_of_terrain(verdict, plan):
                return
            if self._terrain_climb_in_progress(verdict):
                # The climb *is* the response. A hold now would freeze the
                # altitude on a real autopilot and the climb would never finish.
                return
            if not self._held:
                self._held = True
                await self.backend.hold()
                self._command("hold", verdict.primary.rule if verdict.primary else "")
            return

        self._terrain_climb_target_m = None
        if self._held:
            self._held = False
            await self._resume(plan)

    async def _climb_clear_of_terrain(self, verdict: SafetyVerdict, plan: MissionPlan) -> bool:
        """Answer a terrain-clearance HOLD by raising the cruise altitude.

        Holding in place over rising ground fixes nothing; the aircraft needs to
        be higher. Re-command the same target at the altitude the rule asked for,
        clamped to the ceiling (the rule escalates to RETURN itself when the
        ceiling makes that impossible).
        """
        state = self.state
        if state.phase is not MissionPhase.CRUISE or state.target is None:
            return False
        trigger = next((t for t in verdict.triggers if t.rule == "terrain_clearance"), None)
        if trigger is None:
            return False
        needed_rel = trigger.detail.get("needed_rel_m")
        if needed_rel is None:
            return False
        altitude_cfg = self.config.altitude
        needed_rel = float(needed_rel)
        # Clear the highest ground on the rest of the route in one climb rather
        # than stair-stepping up a slope one tolerance band at a time.
        if state.max_ground_elevation_m is not None and state.home_elevation_m is not None:
            needed_rel = max(
                needed_rel,
                state.max_ground_elevation_m + altitude_cfg.terrain_clearance_m - state.home_elevation_m,
            )
        target_altitude = min(needed_rel + altitude_cfg.clearance_tolerance_m, altitude_cfg.max_altitude_m)
        current = state.cruise_altitude_m or plan.cruise_altitude_rel_m
        if target_altitude <= current + 0.5:
            return False
        state.cruise_altitude_m = target_altitude
        self._terrain_climb_target_m = target_altitude
        await self.backend.goto(state.target, target_altitude, self.config.flight.cruise_speed_ms)
        self._command("climb", f"terrain: {current:.0f}m -> {target_altitude:.0f}m")
        return True

    def _terrain_climb_in_progress(self, verdict: SafetyVerdict) -> bool:
        if self._terrain_climb_target_m is None:
            return False
        return all(t.rule == "terrain_clearance" for t in verdict.triggers)

    async def _resume(self, plan: MissionPlan) -> None:
        """Pick the mission back up after a transient HOLD has cleared."""
        state = self.state
        if state.phase is MissionPhase.CRUISE and state.target is not None:
            await self.backend.goto(
                state.target,
                state.cruise_altitude_m or plan.cruise_altitude_rel_m,
                self.config.flight.cruise_speed_ms,
            )
            self._command("resume goto")
        elif state.phase is MissionPhase.RETURN:
            await self.backend.return_to_home(self.config.altitude.rth_altitude_m)
            self._command("resume return")

    async def _advance(self, telemetry: Telemetry, plan: MissionPlan, now: float) -> None:
        state = self.state
        altitude = telemetry.altitude_rel_m or 0.0
        cruise_altitude = state.cruise_altitude_m or plan.cruise_altitude_rel_m

        if state.phase is MissionPhase.TAKEOFF:
            if altitude >= cruise_altitude - 1.0:
                state.phase = MissionPhase.CRUISE
                await self.backend.goto(
                    plan.target, cruise_altitude, self.config.flight.cruise_speed_ms
                )
                self._command("goto", str(plan.target))
            return

        if state.phase is MissionPhase.CRUISE:
            distance = telemetry.distance_to(plan.target)
            if distance is not None and distance <= self.config.flight.arrival_radius_m:
                state.phase = MissionPhase.HOVER
                state.hover_started_at = now
                self._reached_target = True
                await self.backend.hold()
                self._command("arrived", f"hovering {plan.hover_s:.0f}s")
            return

        if state.phase is MissionPhase.HOVER:
            if state.hover_elapsed(now) >= plan.hover_s:
                state.phase = MissionPhase.RETURN
                await self.backend.return_to_home(self.config.altitude.rth_altitude_m)
                self._command("return", "hover complete")
            return

    def _is_down(self, telemetry: Telemetry) -> bool:
        if not self._was_airborne:
            return False
        if self.state.phase is MissionPhase.ABORTED:
            return (telemetry.altitude_rel_m or 0.0) <= 0.5
        return not telemetry.in_air and not telemetry.armed

    async def _emergency_land(self) -> None:
        try:
            await self.backend.land()
        except BackendError:
            log.error("emergency land command also failed")

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------
    def _outcome(self) -> MissionOutcome:
        if self.state.phase is MissionPhase.ABORTED:
            return MissionOutcome.TERMINATED
        if not self._interrupted:
            return MissionOutcome.COMPLETED
        latched = self.engine.latched_severity
        if latched >= Severity.LAND:
            return MissionOutcome.LANDED_OUT
        if latched >= Severity.RETURN:
            return MissionOutcome.RETURNED
        return MissionOutcome.COMPLETED

    def _reason(self) -> str:
        primary = max(
            self.engine.latched_triggers, key=lambda t: t.severity, default=None
        )
        if primary is not None and self._interrupted:
            return primary.reason
        if self._reached_target:
            return "reached target, hovered, and returned home"
        return "mission completed"

    def _result(
        self,
        outcome: MissionOutcome,
        reason: str,
        telemetry: Telemetry,
        report: PreflightReport,
    ) -> MissionResult:
        distance_home = telemetry.distance_to(self.state.home)
        return MissionResult(
            outcome=outcome,
            reason=reason,
            preflight=report,
            triggers=self.engine.latched_triggers,
            duration_s=self.state.elapsed(self.backend.now()),
            reached_target=self._reached_target,
            final_position=telemetry.position,
            distance_from_home_m=distance_home,
            final_battery=telemetry.battery_remaining,
        )

    def _command(self, command: str, detail: str = "") -> None:
        log.info("t+%.1fs %s %s", self.backend.now(), command, detail)
        if self.blackbox:
            self.blackbox.command(self.backend.now(), command, detail)

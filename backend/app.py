"""FeelsLike backend: live accelerated simulation + complaint API + dashboard.

Run:  uvicorn backend.app:app --reload      then open http://127.0.0.1:8000
Two twins run in lock-step on identical weather: FeelsLike (constraint-aware)
vs the Static-22degC baseline — that's the racing meter on the dashboard.

WHAT THIS MODULE IS. Every other backend module is a pure, testable subsystem
(parser, constraint store, decision engine, what-if, maintenance, analytics,
privacy). None of them owns a clock or a thread. This file is the ONE place that
does: it runs the simulation loop, holds the single lock, and projects everything
onto HTTP. Subsystems are imported and driven, never reimplemented.

THREE RULES THIS FILE LIVES BY
1. ONE LOCK, HELD FOR EVERY READ OF SHARED MUTABLE STATE. The sim thread appends
   to history, rebinds the feed, appends to the analytics ring buffer and the
   decision deque. FastAPI serialises a response AFTER the handler returns, so
   handing out an internal container by reference is a genuine "list changed size
   during iteration" mid-demo (defect D1). Every endpoint therefore builds its
   payload inside the lock and hands out copies. The lock is an RLock because
   composite operations nest (a demo step takes the lock, then calls advance(),
   which calls handle_complaint(), which takes it again).
2. NO SUBSYSTEM MAY KILL THE LOOP. Each per-step subsystem call goes through
   _safe(), which records the first failure per subsystem and then only counts
   repeats — a broken detector degrades the dashboard, it never stops the physics.
   The health of all of them is reported at /api/state -> health.
3. ADDITIVE ONLY. The live dashboard reads the legacy /api/state keys verbatim.
   Nothing in this file may rename, retype or remove one of them.

CADENCES (sim time, not wall clock):
  control + physics   every 60 s   (DT — the twin's own step)
  decision log        every step   (DecisionLog itself filters to material events)
  comfort memory      every step   (unchanged from the original)
  maintenance         every 180 s  (see MAINT_INTERVAL_S for the derivation)
  analytics + history every 900 s  (AnalyticsStore's documented 15-min cadence)
  retention sweep     every 900 s  (piggybacks the analytics tick)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from backend import parser, privacy, whatif
from backend.analytics import AnalyticsStore
from backend.constraints import ConstraintStore
from backend.contracts import OBJECTIVES, SAFETY_MODES, to_dict
from backend.decisions import DecisionLog
from backend.demo import DemoRunner
from backend.maintenance import MaintenanceMonitor
from backend.memory import ComfortMemory
from backend.privacy import AIDisclosure
from sim.controllers import ConstraintAware, StaticSchedule
from sim.humidity import outdoor_rh
from sim.twin import (COP, DT, FAN_W, GRID_CO2, TARIFF, ZONE_BY_ID, ZONES,
                      DigitalTwin)

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
DASHBOARD = DASHBOARD_DIR / "index.html"
OCCUPANT = DASHBOARD_DIR / "occupant.html"
EXPERIMENTS_FILE = ROOT / "evals" / "results_whatif.json"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

FEED_MAX = 30                # occupant channel depth the dashboard renders
DECISIONS_IN_STATE = 20      # most recent decisions inlined into /api/state
DECISION_LOG_MAX = 500

# Maintenance cadence. The detectors are sustained-evidence windows (capacity and
# sensor 45 min, actuator 30 min) with a GRACE_MIN of 5 sim-minutes during which a
# symptom may blink off without resetting the streak. Sampling must therefore be
# comfortably FINER than that grace window or a real streak would look interrupted;
# 180 s gives a 15-sample capacity window and 100x less work than every step,
# while the actuator detector still sees every vent transition the schedule makes
# (the schedule changes fan level on quarter-hour occupancy boundaries).
MAINT_INTERVAL_S = 180.0
SAMPLE_INTERVAL_S = 900.0    # analytics + energy history + retention sweep
MAX_ADVANCE_MIN = 24 * 60.0  # ceiling on one explicit advance() call


class LiveSim:
    """The live A/B simulation plus every subsystem that reads it.

    INPUT: constructed once at import; mutated only through its own methods and
      the endpoints below. seed fixes the weather, speed is sim-seconds per
      real-second, start_hour is where a fresh (or reset) building begins.
    OUTPUT: state() — the whole /api/state payload, copies only.
    SIDE EFFECTS: starts one daemon thread that steps the physics forever.
    ERROR STATES: none escape the loop; see _safe().
    """

    def __init__(self, seed: int = 7, speed: float = 240.0, start_hour: float = 8.0):
        self.lock = threading.RLock()           # RLock: composite ops nest (see rule 1)
        self.seed = int(seed)
        self.speed = float(speed)               # sim-seconds per real-second
        self.start_hour = float(start_hour)
        # Operator configuration — survives a reset, because it is a choice, not state.
        self.objective = "balanced"
        self.requested_safety_mode = "automatic"
        self.privacy_cfg = privacy.DEFAULTS
        self.errors: dict = {}                  # subsystem -> first failure + count
        self.steps = 0                          # physics steps since process start
        self._build()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        """Create (or recreate) every piece of live state at start_hour.

        INPUT: none; reads self.seed / start_hour / objective / requested mode.
        OUTPUT: None. SIDE EFFECTS: replaces twins, store, memory, controllers,
          logs, monitor, analytics, feed, history and all lock bookkeeping.
        ERROR STATES: none.
        """
        self.store = ConstraintStore()
        self.store.set_objective(self.objective)
        self.memory = ComfortMemory()
        self.us = DigitalTwin(seed=self.seed)
        self.base = DigitalTwin(seed=self.seed)
        self.us.t = self.base.t = self.start_hour * 3600.0
        self.ctrl_us = ConstraintAware(objective=self.objective)
        self.ctrl_base = StaticSchedule()
        self.last_sps, self.last_vents = {}, {}
        self.zone_cool_w = {z.id: 0.0 for z in ZONES}    # exact, from kwh_by_zone
        self.zone_power_w = {z.id: 0.0 for z in ZONES}
        self.history: list = []                 # sampled every 15 sim-min
        self.feed: list = []                    # complaint / event log
        self.decisions = DecisionLog(maxlen=DECISION_LOG_MAX)
        self.monitor = MaintenanceMonitor()
        self.analytics = AnalyticsStore(sample_min=SAMPLE_INTERVAL_S / 60.0)
        self.retention = privacy.RetentionPolicy(self.privacy_cfg.retention_hours)
        self.operator_locks: set = set()        # zones an operator froze
        self.auto_locks: set = set()            # zones a capacity alert froze
        self._next_sample = self.us.t
        self._next_maint = self.us.t
        self._sync_controller()

    # ---------------------------------------------------------------- loop
    def _safe(self, name: str, fn, *a):
        """Run a subsystem tick; record the FIRST failure, then only count repeats.

        INPUT: subsystem name, callable, args. OUTPUT: the callable's result, or
          None when it raised. SIDE EFFECTS: writes self.errors[name].
        ERROR STATES: swallows everything — rule 2.
        """
        try:
            return fn(*a)
        except Exception as e:                  # noqa: BLE001 - deliberate firewall
            rec = self.errors.get(name)
            if rec is None:
                self.errors[name] = {"subsystem": name, "count": 1,
                                     "error": f"{type(e).__name__}: {e}",
                                     "first_t": self.us.t, "last_t": self.us.t}
            else:
                rec["count"] += 1
                rec["last_t"] = self.us.t
            return None

    def _loop(self) -> None:
        acc = 0.0
        while True:
            time.sleep(0.2)
            with self.lock:
                acc += self.speed * 0.2
                while acc >= DT:
                    self._step_once()
                    acc -= DT

    def _step_once(self) -> None:
        """One 60-sim-second step of BOTH twins plus every subsystem tick.

        INPUT: none. OUTPUT: None. SIDE EFFECTS: steps the physics, refreshes the
          controller decision log, comfort memory, maintenance monitor, analytics
          sampler and the retention sweep on their own cadences.
        ERROR STATES: the physics is unguarded (a failure there is a real bug and
          must be loud); every optional subsystem goes through _safe().
        CALLER MUST HOLD self.lock.
        """
        kwh_before = dict(self.us.kwh_by_zone)
        sps, vents = self.ctrl_us.act(self.us, self.store)
        self.us.step(sps, vents)
        bs, bv = self.ctrl_base.act(self.base)
        self.base.step(bs, bv)
        self.last_sps, self.last_vents = sps, vents
        self.steps += 1
        self._measure_zone_power(kwh_before, vents)

        self._safe("decisions", self._tick_decisions)
        self._safe("memory", self._tick_memory)
        if self.us.t >= self._next_maint:
            self._next_maint = self.us.t + MAINT_INTERVAL_S
            self._safe("maintenance", self._tick_maintenance)
        if self.us.t >= self._next_sample:
            self._next_sample = self.us.t + SAMPLE_INTERVAL_S
            self.history.append({"t": self.us.t,
                                 "us": round(self.us.kwh, 2),
                                 "base": round(self.base.kwh, 2)})
            self.history = self.history[-800:]
            self._safe("analytics", self._tick_analytics)
            self._safe("retention", self._tick_retention)

    def _measure_zone_power(self, kwh_before: dict, vents: dict) -> None:
        """Per-zone electrical draw and cooling load for the step just taken.

        Derived from the twin's OWN accounting (the delta of kwh_by_zone) rather
        than re-modelled here, so it is exact: p_zone = q_cool/COP + FAN_W[vent],
        which inverts to q_cool = (p_zone - FAN_W[vent]) * COP. That gives the
        honest capacity utilisation the dashboard shows, with no second physics.
        """
        for z in ZONES:
            d_kwh = self.us.kwh_by_zone[z.id] - kwh_before.get(z.id, 0.0)
            p_w = d_kwh * 3.6e6 / DT
            fan = FAN_W.get(max(0, min(2, int(vents.get(z.id, 0) or 0))), 0.0)
            self.zone_power_w[z.id] = p_w
            self.zone_cool_w[z.id] = max(0.0, (p_w - fan) * COP)

    def _tick_decisions(self) -> None:
        for d in self.ctrl_us.last_decisions:
            self.decisions.record(d)

    def _tick_memory(self) -> None:
        for note in self.memory.tick(self.us, self.store):
            self.add_feed(note)

    def _tick_maintenance(self) -> None:
        self.monitor.tick(self.us, self.store, self.ctrl_us.last_decisions)
        self._sync_auto_locks()

    def _tick_analytics(self) -> None:
        self.analytics.sample(self.us, self.base, self.store)
        self.analytics.note_alerts(self.monitor.alerts())

    def _tick_retention(self) -> None:
        self.feed = self.retention.apply(self.feed, self.us.t)

    # ------------------------------------------------------- controller state
    def _sync_auto_locks(self) -> None:
        """Honour MaintenanceMonitor.suppress_setpoint_chasing() for every zone.

        WHY: once a coil is pinned at 100%, every further degree of "demand" buys
        no cooling — it only guarantees the zone can never satisfy its own
        thermostat, and it hides a maintenance problem behind a control problem.

        HOW, without editing sim/controllers.py: lock_zone()/unlock_zone() is the
        controller's own lever, and it bites in maintenance_lockout mode, where a
        LOCKED zone is frozen at its base schedule setpoint (complaints are still
        recorded, just not applied) while every UNLOCKED zone keeps running
        normally. So a capacity alert adds its zone to auto_locks, and
        _sync_controller() promotes the controller into maintenance_lockout for as
        long as any lock exists. The operator's requested mode is remembered
        separately and restored when the last lock clears, and an operator who has
        deliberately chosen another mode is never overridden.
        """
        auto = {z.id for z in ZONES if self.monitor.suppress_setpoint_chasing(z.id)}
        if auto != self.auto_locks:
            self.auto_locks = auto
            self._sync_controller()

    def _sync_controller(self) -> None:
        """Push objective / locks / effective safety mode onto the controller.

        The effective mode is the requested one, except that "automatic" is
        promoted to "maintenance_lockout" while any zone is locked — otherwise a
        lock would be a control that changes nothing, which this project does not
        ship. Both values are reported at /api/state -> controller.
        """
        locks = set(self.operator_locks) | set(self.auto_locks)
        effective = self.requested_safety_mode
        if locks and effective == "automatic":
            effective = "maintenance_lockout"
        for z in sorted(locks - self.ctrl_us.locked_zones):
            self.ctrl_us.lock_zone(z)               # the controller's own lever
        for z in sorted(set(self.ctrl_us.locked_zones) - locks):
            self.ctrl_us.unlock_zone(z)
        if self.ctrl_us.safety_mode != effective:
            self.ctrl_us.set_safety_mode(effective)
        if self.ctrl_us.objective != self.objective:
            self.ctrl_us.set_objective(self.objective)

    def controller_state(self) -> dict:
        """Everything an operator needs about the controller. CALLER HOLDS THE LOCK."""
        locks = sorted(set(self.operator_locks) | set(self.auto_locks))
        effective = self.ctrl_us.safety_mode
        note = ""
        if self.auto_locks and effective == "maintenance_lockout":
            note = ("A capacity alert froze " + ", ".join(sorted(self.auto_locks)) +
                    ": setpoint chasing is suppressed there until the alert clears.")
        elif locks and effective != "maintenance_lockout":
            note = (f"Zones are locked but safety mode is {effective!r}; a lockout "
                    f"only takes effect in maintenance_lockout mode.")
        return {
            "objective": self.ctrl_us.objective,
            "safety_mode": effective,                       # what really governs writes
            "requested_safety_mode": self.requested_safety_mode,
            "locked_zones": locks,
            "operator_locked_zones": sorted(self.operator_locks),
            "auto_locked_zones": sorted(self.auto_locks),
            "pending_recommendations": [dict(p) for p in
                                        self.ctrl_us.pending_recommendations],
            "objectives": list(OBJECTIVES),
            "safety_modes": {k: v for k, v in SAFETY_MODES.items()},
            "schedule": {"occupied": self.ctrl_us.base_occupied,
                         "precool": self.ctrl_us.base_precool,
                         "unoccupied": self.ctrl_us.base_unoccupied,
                         "lead_h": self.ctrl_us.profile.lead_h},
            "note": note,
        }

    # ------------------------------------------------------------- mutations
    def set_conditions(self, **knobs) -> dict:
        """Apply what-if condition knobs to BOTH twins.

        Both, always: a heat wave applied to the FeelsLike twin alone would rig
        the A/B race the whole dashboard is built on. Values are clamped by
        DigitalTwin.set_conditions to its documented ranges.

        INPUT: any of occ_scale, capacity_scale, solar_scale, outdoor_offset,
          humidity_offset (None or absent = unchanged).
        OUTPUT: the resulting knob dict (identical for both twins).
        SIDE EFFECTS: mutates both twins; every subsequent step uses the new
          conditions. ERROR STATES: TypeError from float() on a non-numeric knob.
        """
        with self.lock:
            self.base.set_conditions(**knobs)
            return self.us.set_conditions(**knobs)

    def advance(self, minutes: float) -> float:
        """Step the simulation forward explicitly, running every subsystem tick.

        Used by the guided demo so a step never has to wait on the 240x loop.

        INPUT: sim-minutes (<= 0 is a no-op; capped at MAX_ADVANCE_MIN).
        OUTPUT: the sim-minutes actually advanced.
        SIDE EFFECTS: identical to that many loop iterations.
        ERROR STATES: none.
        """
        m = max(0.0, min(float(minutes), MAX_ADVANCE_MIN))
        n = int(round(m * 60.0 / DT))
        with self.lock:
            for _ in range(n):
                self._step_once()
        return round(n * DT / 60.0, 2)

    def advance_to_hour(self, hour: float) -> float:
        """Advance to the next occurrence of a wall-clock hour, at most 24 h away.

        INPUT: hour 0..24 (fractional allowed, e.g. 12.5 for 12:30).
        OUTPUT: sim-minutes advanced (0.0 when already within one step of it).
        SIDE EFFECTS: as advance(). ERROR STATES: none.
        """
        with self.lock:
            target = (float(hour) % 24.0) * 3600.0
            delta = (target - (self.us.t % 86400.0)) % 86400.0
            return self.advance(delta / 60.0) if delta >= DT else 0.0

    def add_feed(self, entry: dict) -> dict:
        """Push one occupant-channel entry, newest first. CALLER NEED NOT HOLD LOCK."""
        with self.lock:
            entry["sim_clock"] = self.clock()
            entry["t"] = self.us.t          # numeric stamp: retention needs a clock
            self.feed = ([entry] + self.feed)[:FEED_MAX]
            return entry

    def clock(self) -> str:
        d, h = self.us.day, self.us.hour
        return f"{DAYS[d % 7]} {int(h):02d}:{int((h % 1) * 60):02d}"

    def outdoor(self) -> tuple:
        """(dry-bulb degC, %RH) the building is actually experiencing right now.

        Both include the live condition offsets, exactly as DigitalTwin.step uses
        them, so the dashboard never shows a milder outdoors than the physics saw.
        Computed from the public sim.humidity.outdoor_rh rather than the twin's
        private helper. INPUT: none. OUTPUT: (float, float). SIDE EFFECTS: none.
        """
        t = self.us.t
        rh = outdoor_rh(t, self.us.seed) + self.us.humidity_offset
        return (float(self.us.weather_fn(t)) + self.us.outdoor_offset,
                min(100.0, max(0.0, rh)))

    # ---------------------------------------------------------- complaints
    def handle_complaint(self, text: str, author: str = "occupant",
                         anonymous: bool | None = None) -> dict:
        """The one complaint pipeline: parse -> privacy -> store -> feed.

        MULTI-ZONE: one utterance may name several zones. Every applied complaint
        goes through ConstraintStore.add_many, so it always carries a complaint_id
        and the sibling constraints can be found later; a retraction clears every
        zone it names. The response keeps the ORIGINAL single-zone shape (action,
        parsed, explanation, source, latency_ms) so the live dashboard is
        unaffected, and adds zones / constraints / explanations / complaint_id.

        PRIVACY ORDER MATTERS: PII is scrubbed BEFORE the text is parsed, so a
        phone number cannot reach an external LLM either. Zone names and
        temperatures are shielded by privacy.scrub_pii, so scrubbing never
        damages the control-relevant content.

        INPUT: text (raw occupant message), author handle, anonymous (None = the
          deployment default in privacy_cfg).
        OUTPUT: dict(ok, action, author, text, source, latency_ms, parsed, plus
          explanation/explanations/zones/constraints/complaint_id where they apply).
        SIDE EFFECTS: appends to the feed; may add or expire constraints.
        ERROR STATES: none — parser.parse() degrades to the offline rules path and
          an unparseable message simply becomes an "ignored" feed entry.
        """
        anon = self.privacy_cfg.anonymous_mode if anonymous is None else bool(anonymous)
        cfg = replace(self.privacy_cfg, anonymous_mode=anon)
        clean, found = privacy.scrub_pii(str(text)) if cfg.scrub else (str(text), [])
        shown = privacy.anonymize_author(str(author)) if anon else str(author)

        parsed, source, latency = parser.parse(clean)
        entry = {"author": shown, "text": clean, "source": source,
                 "latency_ms": latency, "parsed": parsed.model_dump(),
                 "external_ai": AIDisclosure.is_external(source)}
        if found:
            entry["redacted"] = found
        if anon:
            entry["author_anonymous"] = shown != str(author)
        zone_ids = list(parsed.zone_ids or ([parsed.zone_id] if parsed.zone_id else []))

        if parser.detect_retraction(clean):
            if not zone_ids:
                entry["action"] = "noted — glad it's better (no zone named, nothing cleared)"
                self.add_feed(entry)
                return {"ok": True, "action": "noted", **entry}
            with self.lock:
                per_zone = {z: self.store.clear_zone(z, self.us.t) for z in zone_ids}
            n = sum(per_zone.values())
            names = ", ".join(_zone_name(z) for z in zone_ids)
            entry["action"] = f"all-clear — {n} constraint(s) cleared in {names}"
            entry["zones"] = zone_ids
            entry["cleared_by_zone"] = per_zone
            self.add_feed(entry)
            return {"ok": True, "action": "cleared", "cleared": n, **entry}

        if not parsed.is_comfort_complaint:
            entry["action"] = "ignored — not a comfort complaint"
            self.add_feed(entry)
            return {"ok": True, "action": "ignored", **entry}

        if not zone_ids:
            entry["action"] = ("clarify — which zone? (" +
                               ", ".join(z.name for z in ZONES) + ")")
            self.add_feed(entry)
            return {"ok": True, "action": "clarify", **entry}

        with self.lock:
            created = self.store.add_many(zone_ids, parsed.issue, parsed.severity,
                                          parsed.confidence, self.us.t,
                                          text=clean, author=shown)
            if anon:
                for c in created:                # the occupant asked for anonymity
                    c.anonymous = True
            cid = created[0].complaint_id if created else ""
            explanations = []
            for z in zone_ids:
                ex = self.store.explain(z, self.us.t)
                ex["zone_name"] = _zone_name(z)
                explanations.append(ex)
        entry["action"] = "applied"
        entry["complaint_id"] = cid
        entry["zones"] = zone_ids
        entry["constraints"] = [c.id for c in created]
        entry["explanations"] = explanations
        entry["explanation"] = explanations[0]   # back-compat: dashboard reads this
        self.add_feed(entry)
        return {"ok": True, "action": "applied", **entry}

    # ------------------------------------------------------------- what-if
    def run_whatif(self, scenario: str, horizon_h: float | None = None,
                   seeds: list | None = None) -> dict:
        """Run one what-if scenario against a FROZEN SNAPSHOT of the live building.

        The snapshot matters twice. It gives the comparison a coherent starting
        state (the sim thread does not step underneath the run), and it makes the
        isolation proof meaningful: fingerprinting the LIVE twin before and after
        would report a false failure simply because the loop advanced it. So the
        clone is fingerprinted, compare() runs, the fingerprint is re-checked, and
        verify_isolation() re-runs the three most dangerous scenario families
        against the same snapshot. The live twin is never passed to the engine.

        INPUT: a backend.whatif.SCENARIOS key, optional horizon_h, optional seeds.
        OUTPUT: compare() output (json-safe) + isolation_verified + snapshot info.
        SIDE EFFECTS: none on live state.
        ERROR STATES: KeyError (unknown scenario or params key), ValueError
          (horizon_h <= 0 or empty seeds) — both propagate to the caller.
        """
        spec = whatif.scenario_spec(
            scenario,
            horizon_h=whatif.DEFAULT_HORIZON_H if horizon_h is None else float(horizon_h),
            seeds=seeds)
        with self.lock:
            twin, store = self.us.clone(), self.store.clone()
            clock = self.clock()
        before = whatif.state_fingerprint(twin, store)
        out = to_dict(whatif.compare(twin, store, spec))
        stable = whatif.state_fingerprint(twin, store) == before
        out["isolation_verified"] = bool(stable and whatif.verify_isolation(twin, store))
        out["snapshot"] = {"clock": clock, "fingerprint": before,
                           "note": "Scenarios run on clones of this snapshot; the "
                                   "live twin is never passed to the engine."}
        return out

    # ------------------------------------------------------------ projections
    def privacy_state(self) -> dict:
        """The privacy panel's payload. CALLER HOLDS THE LOCK."""
        src = str(self.feed[0].get("source", "")) if self.feed else "rules"
        return {
            "anonymous_default": bool(self.privacy_cfg.anonymous_mode),
            "retention_hours": float(self.retention.hours),
            "scrub_default": bool(self.privacy_cfg.scrub),
            "ai_disclosure": {
                "source": src,
                "text": AIDisclosure.describe(src),
                "external": AIDisclosure.is_external(src),
                "provider": AIDisclosure.provider(),
            },
            "retention_audit": self.retention.audit(self.feed, self.us.t),
        }

    def zone_rows(self) -> list:
        """The /api/state zones array. CALLER HOLDS THE LOCK.

        The first nine keys are the frozen legacy set the live dashboard reads
        verbatim; everything after them is additive.
        """
        adjustments = self.store.zone_adjustments(self.us.t)
        alerts_by_zone: dict = {}
        for a in self.monitor.alerts():
            alerts_by_zone.setdefault(a.get("zone", ""), []).append(
                {"id": a.get("id"), "kind": a.get("kind"),
                 "severity": a.get("severity"), "confidence": a.get("confidence")})
        lockout = self.ctrl_us.safety_mode == "maintenance_lockout"
        rows = []
        for z in ZONES:
            adj = adjustments.get(z.id)
            snap = self.us.zone_snapshot(z.id)
            cap = max(1e-6, snap["capacity_w"])
            rows.append({
                # --- legacy keys: name and meaning frozen ---------------------
                "id": z.id, "name": z.name,
                "temp": round(self.us.T[z.id], 1),
                "base_temp": round(self.base.T[z.id], 1),
                "setpoint": self.last_sps.get(z.id),
                "vent": self.last_vents.get(z.id, 0),
                "occ": int(self.us.occupancy_now(z.id)),
                "offset": adj["setpoint_offset"] if adj else 0.0,
                "active_constraints": len(self.store.active(self.us.t, z.id)),
                # --- additive -------------------------------------------------
                "rh": snap["rh_pct"],
                "dew_point_c": snap["dew_point_c"],
                "occ_pct": snap["occ_pct"],
                "capacity_pct": round(min(100.0, 100.0 * self.zone_cool_w[z.id] / cap), 1),
                "capacity_w": snap["capacity_w"],
                "cool_w": round(self.zone_cool_w[z.id], 1),
                "power_w": round(self.zone_power_w[z.id], 1),
                "at_capacity": snap["at_capacity"],
                "locked_out": bool(lockout and z.id in self.ctrl_us.locked_zones),
                "conflict": bool(adj["conflict"]) if adj else False,
                "pending_constraints": int(adj["pending"]) if adj else 0,
                "alerts": alerts_by_zone.get(z.id, []),
            })
        return rows

    def state(self) -> dict:
        """The whole dashboard payload. Copies only — see rule 1 / defect D1.

        INPUT: none. OUTPUT: the /api/state dict; every legacy key present with
          its original name and meaning. SIDE EFFECTS: none.
        ERROR STATES: none.
        """
        with self.lock:
            zones = self.zone_rows()
            t_out, rh_out = self.outdoor()
            m_us, m_base = self.us.metrics(), self.base.metrics()
            saved_kwh = max(0.0, self.base.kwh - self.us.kwh)
            pct = 100.0 * saved_kwh / self.base.kwh if self.base.kwh > 1e-6 else 0.0
            alerts = self.monitor.alerts()
            feed = [dict(e) for e in self.feed]
            return {
                "sim": {"clock": self.clock(), "hour": round(self.us.hour, 2),
                        "t_out": round(t_out, 1),
                        "speed": self.speed,
                        # --- additive -------------------------------------
                        "t": self.us.t, "day": self.us.day,
                        "seed": self.seed, "steps": self.steps,
                        "outdoor_rh": round(rh_out, 1),
                        "conditions": {
                            "occ_scale": self.us.occ_scale,
                            "capacity_scale": self.us.capacity_scale,
                            "solar_scale": self.us.solar_scale,
                            "outdoor_offset": round(self.us.outdoor_offset, 2),
                            "humidity_offset": round(self.us.humidity_offset, 2),
                        }},
                "zones": zones,
                "meters": {"us": m_us, "base": m_base,
                           "saved_kwh": round(saved_kwh, 2),
                           "saved_pct": round(pct, 1),
                           "saved_rs": round(saved_kwh * TARIFF, 1),
                           "saved_co2": round(saved_kwh * GRID_CO2, 2)},
                "history": list(self.history),          # copy: the loop appends
                "feed": feed,                           # copies: the loop rebinds
                # --- additive top-level ------------------------------------
                "controller": self.controller_state(),
                "alerts": alerts,
                "decisions": self.decisions.recent(DECISIONS_IN_STATE),
                "analytics": {"summary": self.analytics.summary(feed, alerts)},
                "privacy": self.privacy_state(),
                "constraint_stats": self.store.stats(self.us.t),
                "health": {"errors": [dict(v) for v in self.errors.values()],
                           "steps": self.steps,
                           "maint_interval_s": MAINT_INTERVAL_S,
                           "sample_interval_s": SAMPLE_INTERVAL_S},
            }


def _zone_name(zone_id: str) -> str:
    z = ZONE_BY_ID.get(zone_id)
    return z.name if z else zone_id


sim = LiveSim()
demo_runner = DemoRunner()
app = FastAPI(title="FeelsLike")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
if DASHBOARD_DIR.is_dir():                  # panels.js / occupant.html / assets
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")


# ==========================================================================
# request bodies. extra="forbid" on the new endpoints so a typo'd knob is a
# loud 422 instead of a silent no-op. ComplaintIn stays permissive: it is the
# one body an existing client already posts.
# ==========================================================================

class ComplaintIn(BaseModel):
    text: str
    author: str = "occupant"
    anonymous: bool | None = None


class ConditionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occ_scale: float | None = None
    capacity_scale: float | None = None
    solar_scale: float | None = None
    outdoor_offset: float | None = None
    humidity_offset: float | None = None


class ControllerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str | None = None
    safety_mode: str | None = None
    lock_zone: str | None = None
    unlock_zone: str | None = None


class WhatIfIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    horizon_h: float | None = None
    seeds: list[int] | None = None


class RedactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str


class DemoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = "next"


# ==========================================================================
# pages
# ==========================================================================

@app.get("/")
def index():
    return FileResponse(DASHBOARD)


@app.get("/occupant")
def occupant():
    """The phone-sized occupant page.

    INPUT: none. OUTPUT: dashboard/occupant.html.
    SIDE EFFECTS: none.
    ERROR STATES: 404 when the file has not been created yet — the dashboard is
      built by a separate agent, and a missing page must say so rather than
      serving the operator console to an occupant.
    """
    if not OCCUPANT.is_file():
        raise HTTPException(404, "dashboard/occupant.html is not present")
    return FileResponse(OCCUPANT)


@app.get("/api/state")
def state():
    return sim.state()


# ==========================================================================
# occupant channel
# ==========================================================================

def handle_complaint(text: str, author: str, anonymous: bool | None = None) -> dict:
    """Module-level shim kept for the Slack/Teams path and any older caller."""
    return sim.handle_complaint(text, author, anonymous)


@app.post("/api/complaint")
def complaint(body: ComplaintIn):
    """File one occupant complaint (or all-clear).

    INPUT: {"text": str, "author": str = "occupant", "anonymous": bool | null}.
      anonymous=true scrubs PII from the text (always on by default) and replaces
      the handle with a salted per-session pseudonym before anything is stored.
    OUTPUT: {ok, action, author, text, source, latency_ms, parsed, ...} —
      action is one of applied / cleared / ignored / clarify / noted; a multi-zone
      complaint also carries complaint_id, zones, constraints, explanations.
    SIDE EFFECTS: appends to the feed; may create or expire constraints.
    ERROR STATES: 422 when text is missing or not a string; 400 for empty text.
      Parser failures never surface — parse() falls back to the offline rules.
    """
    if not body.text.strip():
        raise HTTPException(400, "text must not be empty")
    return sim.handle_complaint(body.text, body.author, body.anonymous)


ZONE_NAMES = {z.id: z.name for z in ZONES}


@app.post("/api/slack")
async def slack_command(request: Request):
    """Slack slash-command webhook. Point the command's Request URL here
    (e.g. via `ngrok http 8000` -> https://xxx.ngrok.app/api/slack).
    Slack posts application/x-www-form-urlencoded and wants a reply in <3 s.
    Teams outgoing-webhook JSON bodies work too (fields: text, from.name)."""
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        author = str((body.get("from") or {}).get("name", "teams"))
    else:
        form = await request.form()
        text = str(form.get("text", "")).strip()
        author = str(form.get("user_name", "slack"))
    if not text:
        return {"response_type": "ephemeral",
                "text": "Tell the building what feels wrong — e.g. "
                        "`/feelslike it's stuffy in Conference Room B`"}

    r = handle_complaint(text, author)
    p = r["parsed"]
    zones = r.get("zones") or ([p["zone_id"]] if p.get("zone_id") else [])
    zone = ", ".join(ZONE_NAMES.get(z, z) for z in zones) or "—"
    badge = f"[{r['source']} · {r['latency_ms']} ms]"
    action = r["action"]  # long form, e.g. "all-clear — 2 constraint(s) cleared..."
    if action == "applied":
        reply = (f":thermometer: Got it — *{p['issue'].replace('_', ' ')}* in "
                 f"*{zone}* (severity {p['severity']}). "
                 f"{r['explanation']['summary']} {badge}")
    elif action.startswith("all-clear"):
        reply = f":white_check_mark: All clear for *{zone}* — {r['cleared']} constraint(s) lifted. {badge}"
    elif action.startswith("clarify"):
        reply = (":grey_question: Which zone? I know: "
                 + ", ".join(ZONE_NAMES.values()) + f". {badge}")
    else:  # ignored / noted
        reply = f":speech_balloon: Noted, but that doesn't look like a comfort complaint. {badge}"
    return {"response_type": "in_channel", "text": reply}


@app.post("/api/speed")
def set_speed(body: dict):
    with sim.lock:
        sim.speed = float(max(1.0, min(3600.0, body.get("speed", 240))))
    return {"ok": True, "speed": sim.speed}


# ==========================================================================
# conditions + controller
# ==========================================================================

@app.post("/api/conditions")
def set_conditions(body: ConditionsIn):
    """Set the live building conditions on BOTH twins.

    INPUT: any subset of {occ_scale, capacity_scale, solar_scale, outdoor_offset,
      humidity_offset}. Omitted or null knobs are unchanged. Out-of-range values
      are CLAMPED by DigitalTwin.set_conditions (occ 0..3, capacity 0.1..1.5,
      solar 0..2, outdoor +/-10 degC, humidity +/-30 %RH) rather than rejected, so
      a slider that runs past its end still produces a legal building.
    OUTPUT: {"ok": true, "conditions": {the five resulting knob values},
             "clamped": [knobs whose value was clamped]}.
    SIDE EFFECTS: mutates both twins — the FeelsLike twin and the baseline — so
      the A/B race stays a fair comparison.
    ERROR STATES: 422 for an unknown knob name or a non-numeric value (the body
      forbids extras); 400 when no knob at all was supplied.
    """
    asked = {k: v for k, v in body.model_dump().items() if v is not None}
    if not asked:
        raise HTTPException(400, "supply at least one of occ_scale, capacity_scale, "
                                 "solar_scale, outdoor_offset, humidity_offset")
    applied = sim.set_conditions(**asked)
    clamped = [k for k, v in asked.items() if abs(float(v) - applied[k]) > 1e-9]
    return {"ok": True, "conditions": applied, "clamped": clamped}


@app.post("/api/controller")
def set_controller(body: ControllerIn):
    """Change the controller's objective, safety mode or zone lockouts.

    INPUT: any subset of {objective, safety_mode, lock_zone, unlock_zone}.
      objective must be in contracts.OBJECTIVES; it re-derives the whole schedule
      (occupied / pre-cool / setback setpoints and the pre-cool lead) and is also
      recorded on the constraint store. safety_mode must be in
      contracts.SAFETY_MODES. lock_zone / unlock_zone take a zone id.
    OUTPUT: the full controller state — see /api/state -> controller. Because a
      lockout only bites in maintenance_lockout mode, locking a zone while the
      requested mode is "automatic" promotes the EFFECTIVE mode to
      maintenance_lockout and unlocking the last zone restores it; both values are
      in the payload as safety_mode and requested_safety_mode.
    SIDE EFFECTS: mutates the controller (and the store's recorded objective) from
      the next step onward. Nothing is retroactive.
    ERROR STATES: 400 for an unknown objective, safety mode or zone id; 400 when
      the body carries no field at all; 422 for an unknown field name.
    """
    if not any(v is not None for v in body.model_dump().values()):
        raise HTTPException(400, "supply at least one of objective, safety_mode, "
                                 "lock_zone, unlock_zone")
    if body.objective is not None and body.objective not in OBJECTIVES:
        raise HTTPException(400, f"unknown objective {body.objective!r}; "
                                 f"expected one of {list(OBJECTIVES)}")
    if body.safety_mode is not None and body.safety_mode not in SAFETY_MODES:
        raise HTTPException(400, f"unknown safety_mode {body.safety_mode!r}; "
                                 f"expected one of {list(SAFETY_MODES)}")
    for z in (body.lock_zone, body.unlock_zone):
        if z is not None and z not in ZONE_BY_ID:
            raise HTTPException(400, f"unknown zone {z!r}; expected one of "
                                     f"{list(ZONE_BY_ID)}")
    with sim.lock:
        if body.objective is not None:
            sim.objective = body.objective
            sim.store.set_objective(body.objective)
        if body.safety_mode is not None:
            sim.requested_safety_mode = body.safety_mode
        if body.lock_zone is not None:
            sim.operator_locks.add(body.lock_zone)
        if body.unlock_zone is not None:
            sim.operator_locks.discard(body.unlock_zone)
        sim._sync_controller()
        return {"ok": True, **sim.controller_state()}


# ==========================================================================
# decisions
# ==========================================================================

@app.get("/api/decisions")
def get_decisions(zone: str | None = None, limit: int = 50):
    """The controller's audit log, newest first.

    INPUT: ?zone=<zone_id> (optional filter), ?limit=1..500 (default 50). The log
      is a CHANGE log, not a sampler: DecisionLog only stores a decision that is
      materially different from the last one for that zone.
    OUTPUT: {"decisions": [ControllerDecision dicts], "zone", "limit", "total"}.
    SIDE EFFECTS: none.
    ERROR STATES: 400 for an unknown zone or a limit outside 1..500; 422 when
      limit is not an integer.
    """
    if limit < 1 or limit > DECISION_LOG_MAX:
        raise HTTPException(400, f"limit must be between 1 and {DECISION_LOG_MAX}")
    if zone is not None and zone not in ZONE_BY_ID:
        raise HTTPException(400, f"unknown zone {zone!r}; expected one of {list(ZONE_BY_ID)}")
    with sim.lock:
        rows = (sim.decisions.for_zone(zone, limit) if zone
                else sim.decisions.recent(limit))
        total = len(sim.decisions.recent(DECISION_LOG_MAX))
    return {"decisions": rows, "zone": zone, "limit": limit, "total": total}


@app.get("/api/decisions/{decision_id}")
def get_decision(decision_id: str):
    """One full decision record by id.

    INPUT: a DecisionLog id, e.g. "dec-0007" (path segment).
    OUTPUT: {"decision": {...}} — the complete audit record including the
      constraint views that drove it and the est_ energy/comfort deltas.
    SIDE EFFECTS: none.
    ERROR STATES: 404 when the id is unknown or has already been evicted (the log
      keeps the newest 500 rows).
    """
    with sim.lock:
        for row in sim.decisions.recent(DECISION_LOG_MAX):
            if row.get("id") == decision_id:
                return {"decision": row}
    raise HTTPException(404, f"no decision {decision_id!r} in the last "
                             f"{DECISION_LOG_MAX} records")


# ==========================================================================
# constraints
# ==========================================================================

def _constraint_payload() -> dict:
    """active / pending views + store stats. CALLER HOLDS THE LOCK."""
    t = sim.us.t
    active = []
    for z in ZONES:
        for v in sim.store.constraint_views(z.id, t):
            v["zone"], v["zone_name"] = z.id, z.name
            active.append(v)
    pending = []
    for c in sim.store.pending_approvals():
        v = c.view(t)
        v["zone"], v["zone_name"] = c.zone, _zone_name(c.zone)
        v["complaint_id"] = c.complaint_id
        pending.append(v)
    return {"active": active, "pending": pending, "stats": sim.store.stats(t),
            "safety_mode": sim.ctrl_us.safety_mode, "now_t": t}


@app.get("/api/constraints")
def get_constraints():
    """Every live constraint, what is waiting for approval, and the totals.

    INPUT: none.
    OUTPUT: {"active": [ConstraintView + zone/zone_name], "pending": [same, for
      constraints neither approved nor rejected], "stats": {total, active,
      expired, pending, rejected, by_zone, by_issue}, "safety_mode", "now_t"}.
      active is ordered heaviest-first per zone, so row 0 of a zone is the
      complaint actually driving its setpoint.
    SIDE EFFECTS: none.
    ERROR STATES: none — an empty store returns empty lists and zeroed stats.
    """
    with sim.lock:
        return _constraint_payload()


@app.post("/api/constraints/{cid}/approve")
def approve_constraint(cid: int):
    """Approve a withheld constraint so it starts influencing control.

    INPUT: cid — the integer Constraint.id (path segment).
    OUTPUT: {"ok": true, "approved": cid, ...the /api/constraints payload}.
    SIDE EFFECTS: sets approved=True / rejected=False on that constraint. The
      decay clock is NOT reset: approving 40 minutes late applies what is left of
      the complaint, not a fresh one.
    ERROR STATES: 404 for an unknown id; 422 for a non-integer path segment.
    """
    with sim.lock:
        if not sim.store.approve(cid):
            raise HTTPException(404, f"no constraint with id {cid}")
        return {"ok": True, "approved": cid, **_constraint_payload()}


@app.post("/api/constraints/{cid}/reject")
def reject_constraint(cid: int):
    """Reject a constraint: it stays in history but never moves a setpoint.

    INPUT: cid — the integer Constraint.id (path segment).
    OUTPUT: {"ok": true, "rejected": cid, ...the /api/constraints payload}.
    SIDE EFFECTS: sets approved=False / rejected=True. created_t is untouched, so
      the comfort-memory pattern miner still sees that the complaint happened.
    ERROR STATES: 404 for an unknown id; 422 for a non-integer path segment.
    """
    with sim.lock:
        if not sim.store.reject(cid):
            raise HTTPException(404, f"no constraint with id {cid}")
        return {"ok": True, "rejected": cid, **_constraint_payload()}


# ==========================================================================
# what-if
# ==========================================================================

@app.get("/api/scenarios")
def get_scenarios():
    """The what-if registry: what can be asked, and what each question means.

    INPUT: none.
    OUTPUT: {"scenarios": [{key, label, kind, params, help}...],
             "default_horizon_h", "default_seeds", "param_keys", "metrics",
             "ci95_note"}. metrics carries the per-metric verb/unit/direction
      METRIC_META so a renderer can write its own verdict lines.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    return {
        "scenarios": [{"key": k, "label": v["label"], "kind": v["kind"],
                       "params": dict(v["params"]), "help": v["help"]}
                      for k, v in whatif.SCENARIOS.items()],
        "default_horizon_h": whatif.DEFAULT_HORIZON_H,
        "default_seeds": list(whatif.DEFAULT_SEEDS),
        "param_keys": sorted(whatif.PARAM_KEYS),
        "metrics": {k: {"verb": m[0], "unit": m[1], "direction": m[2]}
                    for k, m in whatif.METRIC_META.items()},
        "ci95_note": whatif.CI95_NOTE,
    }


@app.post("/api/whatif")
def post_whatif(body: WhatIfIn):
    """Run one counterfactual on clones of the live building and measure it.

    INPUT: {"scenario": <a /api/scenarios key>, "horizon_h": 0<h<=48 (default 6),
      "seeds": [int] (1..5 entries, default [7])}.
    OUTPUT: compare() output — {"baseline", "scenario", "delta", "spec",
      "headline", "ci95_note"} — plus "isolation_verified" (the live-state proof:
      the snapshot fingerprint is unchanged AND verify_isolation() passed) and
      "snapshot" (the clock and fingerprint the run was taken from).
    SIDE EFFECTS: NONE on live state. Every run is on throwaway clones.
    ERROR STATES: 400 for an unknown scenario, a horizon outside (0, 48], an
      empty or oversized seed list, or a negative seed; 422 for a malformed body.
    """
    if body.scenario not in whatif.SCENARIOS:
        raise HTTPException(400, f"unknown scenario {body.scenario!r}; expected one "
                                 f"of {sorted(whatif.SCENARIOS)}")
    h = whatif.DEFAULT_HORIZON_H if body.horizon_h is None else float(body.horizon_h)
    if not (0.0 < h <= 48.0):
        raise HTTPException(400, "horizon_h must be > 0 and <= 48")
    seeds = body.seeds
    if seeds is not None:
        if not 1 <= len(seeds) <= 5:
            raise HTTPException(400, "seeds must hold between 1 and 5 integers")
        if any(s < 0 for s in seeds):
            raise HTTPException(400, "seeds must be non-negative")
    return sim.run_whatif(body.scenario, horizon_h=h, seeds=seeds)


# ==========================================================================
# analytics / maintenance / experiments
# ==========================================================================

@app.get("/api/analytics")
def get_analytics():
    """Everything the analytics tab draws, from the bounded rolling sampler.

    INPUT: none.
    OUTPUT: {"heatmap": zone x hour comfort deviation grid, "energy": hourly /
      daily / per-zone kWh with savings, "complaints": the occupant-feed
      breakdown, "controller": intervention and arbitration stats over the
      decision log, "summary": the overview payload, "window_h", "samples"}.
      Violation minutes inside the heatmap are rectangle-rule ESTIMATES on a
      15-minute cadence; /api/state -> meters carries the twins' exact counters.
    SIDE EFFECTS: none.
    ERROR STATES: none — an empty store returns full, zeroed shapes so a chart
      always has something to draw.
    """
    with sim.lock:
        feed = [dict(e) for e in sim.feed]
        decisions = sim.decisions.recent(DECISION_LOG_MAX)
        return {
            "heatmap": sim.analytics.comfort_heatmap(),
            "energy": sim.analytics.energy_series(),
            "complaints": sim.analytics.complaint_stats(feed),
            "controller": sim.analytics.controller_stats(decisions),
            "summary": sim.analytics.summary(feed, sim.monitor.alerts()),
            "window_h": sim.analytics.window_h(),
            "samples": len(sim.analytics),
        }


@app.get("/api/maintenance")
def get_maintenance():
    """Active equipment alerts and the ones that have resolved.

    INPUT: none.
    OUTPUT: {"alerts": [active, worst first], "history": [resolved, oldest first,
      each with resolved_t], "suppressed_zones": zones where a capacity alert is
      currently stopping the controller from chasing the setpoint down,
      "interval_s": the detector cadence}.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    with sim.lock:
        return {"alerts": sim.monitor.alerts(),
                "history": sim.monitor.history(),
                "suppressed_zones": sorted(sim.auto_locks),
                "interval_s": MAINT_INTERVAL_S}


@app.get("/api/experiments")
def get_experiments():
    """The saved multi-seed what-if experiment sweep from evals/.

    INPUT: none. Reads evals/results_whatif.json, written by
      scripts.run_experiments (not by this process — the API never regenerates it,
      so what a judge sees is what was committed).
    OUTPUT: the file's contents plus {"available": true, "path"}. When the file is
      missing or unreadable, the SAME shape with available=false, an empty
      scenarios map and a "note" explaining how to produce it — an empty-but-valid
      payload, never a 500 and never a null.
    SIDE EFFECTS: none.
    ERROR STATES: none by design; a corrupt file degrades to available=false with
      the parse error in "note".
    """
    empty = {"available": False, "path": str(EXPERIMENTS_FILE), "scenarios": {},
             "seeds": [], "horizon_h": 0.0, "isolation_held": None,
             "ci95_note": whatif.CI95_NOTE,
             "note": "Run `python -m scripts.run_experiments` to generate "
                     "evals/results_whatif.json."}
    try:
        data = json.loads(EXPERIMENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        empty["note"] = f"{empty['note']} ({type(e).__name__}: {e})"
        return empty
    if not isinstance(data, dict):
        empty["note"] = f"{empty['note']} (file is not a JSON object)"
        return empty
    out = dict(data)
    out["available"] = True
    out["path"] = str(EXPERIMENTS_FILE)
    return out


# ==========================================================================
# reset
# ==========================================================================

@app.post("/api/reset")
def reset():
    """Rebuild the whole simulation at the configured start hour.

    INPUT: none.
    OUTPUT: the fresh /api/state payload.
    SIDE EFFECTS: replaces both twins, the constraint store, comfort memory, both
      controllers, the decision log, the maintenance monitor, the analytics
      sampler, the feed, the energy history and every zone lock. The operator's
      speed, objective and requested safety mode SURVIVE — they are configuration,
      not state. The guided demo cursor is reset too, so a fresh building starts
      from a fresh script.
    ERROR STATES: none.
    """
    with sim.lock:
        sim.errors.clear()
        sim.steps = 0
        sim._build()
        demo_runner.forget()
        return sim.state()


# ==========================================================================
# privacy
# ==========================================================================

@app.get("/api/export")
def export(author: str | None = None, scrub: bool = False):
    """"Download my data": every record this process holds, as one JSON object.

    INPUT: ?author=<handle> to export one person (matched against both the raw
      handle and its session pseudonym, so it works in anonymous mode); omit for
      everything. ?scrub=true re-runs the PII scrubber on the way out.
    OUTPUT: {schema, exported_at_t, subject, counts, records, constraints, notes}
      — the raw occupant messages AND the constraints derived from them, because
      "we deleted your message but kept the setpoint it caused" is not deletion.
    SIDE EFFECTS: none. Neither the feed nor the store is modified.
    ERROR STATES: 400 when author is blank or longer than 64 characters.
    """
    if author is not None and (not author.strip() or len(author) > 64):
        raise HTTPException(400, "author must be 1..64 non-blank characters")
    with sim.lock:
        return privacy.export_records([dict(e) for e in sim.feed], sim.store,
                                      author=author, scrub=scrub, now_t=sim.us.t)


@app.post("/api/redact")
def redact(body: RedactIn):
    """Delete one occupant record — the forget button.

    INPUT: {"entry_id": <an id from /api/export records[].id>}.
    OUTPUT: {"ok": true, "entry_id", "remaining": feed length}.
    SIDE EFFECTS: removes the entry from the live feed AND back-dates every
      constraint that shares its text and author past expiry, replacing that text
      with "[redacted at occupant request]" — so the deletion reaches the control
      layer, not just the display. The constraint objects stay in history (the
      pattern miner needs the timestamps) but influence nothing.
    ERROR STATES: 400 for a blank id; 404 when no record carries that id.
    """
    entry_id = body.entry_id.strip()
    if not entry_id:
        raise HTTPException(400, "entry_id must not be empty")
    with sim.lock:
        if not privacy.redact_record(sim.feed, entry_id, sim.store, sim.us.t):
            raise HTTPException(404, f"no feed record with id {entry_id!r}")
        return {"ok": True, "entry_id": entry_id, "remaining": len(sim.feed)}


# ==========================================================================
# guided demo
# ==========================================================================

@app.get("/api/demo")
def demo_position():
    """Where the guided demo currently is, with a live reading of that step.

    INPUT: none.
    OUTPUT: {step:{id,title,narration,state_hint,mutates}, index, total,
      narration, state_hint, title, started, applied, done, steps, result}.
      result.applied is what the step's action returned when it ran;
      result.live is re-read from the twin/store/log on every call.
    SIDE EFFECTS: none — reading the position never advances it.
    ERROR STATES: none.
    """
    return demo_runner.position(sim)


@app.post("/api/demo")
def demo_step(body: DemoIn):
    """Drive the guided demo.

    INPUT: {"action": "start" | "next" | "prev" | "reset"}.
      start - go to step 1 and perform it. next - advance one step and perform it
      (each step's action runs AT MOST ONCE, so re-walking is not destructive).
      prev - move back only, performing nothing. reset - forget the cursor and put
      the condition knobs back to nominal; it does not rewind the clock or drop
      the complaints the demo filed (POST /api/reset is that button).
    OUTPUT: as GET /api/demo, for the step now current.
    SIDE EFFECTS: real ones — the steps move the weather knobs, post complaints
      through the parser, step the twins, run a what-if on clones and read the
      analytics. Nothing is simulated twice and no number is invented.
    ERROR STATES: 400 for an unknown action; 422 for an unknown body field.
    """
    try:
        return demo_runner.dispatch(body.action, sim)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

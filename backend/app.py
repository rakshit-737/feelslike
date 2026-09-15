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

import csv
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

import os
from datetime import datetime, timezone

from fastapi.responses import Response

from backend import building, comfort, comfort_whatif, parser, privacy, tariff, telemetry, whatif
from backend.comfort_events import ComfortTracker
from backend import latest as lv
from backend import telemetry_publish as tp
from backend import loads_bridge, scenario
from backend.simhw import FAULTS as SIMHW_FAULTS, CONFIG_LIMITS as SIMHW_LIMITS, SimHardware, ADAPTERS as SIMHW_ADAPTERS
# Phase 6: security layer (docs/SECURITY.md)
import hashlib
import hmac
import secrets as _secrets
from fastapi import Depends
from fastapi.responses import JSONResponse
from backend.constraints import ISSUE_EFFECTS
from backend.security.auth import ENERGY_FIELDS, ENERGY_MODES, ROLES as SEC_ROLES
from backend.security.devices import DeviceRegistry, sign as sec_sign
from backend.security.ingest import SecureIngest
from backend.security.protocols import (MqttTelemetryAdapter, SimulatedMqttBroker, mqtt_password,
                                        telemetry_topic)
from backend.security.web import SecurityMiddleware, SecurityState, client_ip, make_enforce, require

LIVE_BUILDING_ID = "live-building"
POLL_INTERVAL_S = max(1.0, float(os.environ.get("FL_POLL_S", "5") or 5))
from backend.dataset import analysis as dsa
from backend.dataset import schema as ds_schema
from backend.dataset.config import DEFAULT_DB as DEFAULT_HISTORY_DB
from backend.dataset.generator import epoch as ds_epoch
from backend.dataset.store import SqliteStore, to_csv
from backend.analytics import AnalyticsStore
from backend.external import Dataset, ExternalFeed
from backend.constraints import ConstraintStore
from backend.contracts import OBJECTIVES, SAFETY_MODES, to_dict
from backend.decisions import DecisionLog
from backend.demo import DemoRunner
from backend.hardware import (HW_ZONE, READING_LOG_MAX, SENSOR_NODE_LOG_MAX,
                              HardwareBridge, HttpHVACAdapter, HttpSensorAdapter,
                              SensorNodeStore)
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
RL_PROGRESS_FILE = ROOT / "rl" / "models" / "progress.csv"
ENERGY_RESULTS_FILE = ROOT / "evals" / "results_energy.json"
RL_CURVE_MAX_POINTS = 240    # the panel's chart budget; the csv holds ~500 rows
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
        # Commercial building profile (backend.building). Configuration, like the
        # objective: survives a reset. It never changes the physics; its operating
        # mode pulls the same objective / safety-mode levers as /api/controller.
        self.building = building.default_config("office")
        # Phase 4: latest-value store. Hardware readings are shown beside the twin by default
        # (the rig is a shoebox, not the conference room); FL_HW_AUTHORITATIVE=1 lets fresh
        # hardware values become the primary zone value.
        self.latest = lv.LatestStore()
        self.hw_authoritative = os.environ.get("FL_HW_AUTHORITATIVE") == "1"
        self._kwh_day = None
        # Phase 5: environmental scenario (configuration: survives a building reset) and the
        # SIMULATED hardware layer (not real hardware; off by default)
        self.scenario = scenario.ScenarioConfig()
        self.weather = None
        self.simhw = SimHardware([z.id for z in ZONES], {z.id: int(self.building.zone_floor.get(z.id, 1)) for z in ZONES},
                                 LIVE_BUILDING_ID, seed=self.seed)
        self.errors: dict = {}              # subsystem -> first failure + count
        self.steps = 0                          # physics steps since process start
        # Hardware bridge + the REAL adapters (backend.hardware). Deliberately
        # NOT rebuilt by _build(): the rig is physical state, not sim state — a
        # simulation reset must not zero a fan that is really spinning.
        self.hw_bridge = HardwareBridge()
        self.hw_sensor = HttpSensorAdapter(self.hw_bridge)
        self.hw_hvac = HttpHVACAdapter(self.hw_bridge)
        # Sensor-only nodes (the Uno ambient reference over USB serial). Kept
        # apart from the actuator bridge so a sensor can never be handed a
        # command or overwrite the rig's reading; survives a reset like the rig.
        self.hw_sensors = SensorNodeStore()
        # External REAL feed (Open-Meteo) + HISTORICAL dataset (backend.external).
        # Wall-clock, read-only, never fed to the physics; survive a reset like
        # the rig does. FL_EXTERNAL=0 disables the network thread entirely.
        self.external = ExternalFeed()
        self.dataset = Dataset()
        self._build()
        self.external.start()
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
        self._apply_scenario()
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
        self.telemetry = telemetry.TelemetryStore()   # step-cadence, Monitor tab
        self.comfort = ComfortTracker()               # Phase 3: comfort events + durations
        self._comfort_hist_cache: dict = {}
        self.retention = privacy.RetentionPolicy(self.privacy_cfg.retention_hours)
        self.operator_locks: set = set()        # zones an operator froze
        self.auto_locks: set = set()            # zones a capacity alert froze
        self._next_sample = self.us.t
        self._next_maint = self.us.t
        self._sync_controller()
        # a rebuilt building publishes its starting state at once (sim readings are state,
        # so the old ones go); hardware readings are physical and are republished on the next post
        self.latest.clear()
        self._kwh_day = None
        self._safe("latest", self._tick_latest)

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
        self._safe("telemetry", self._tick_telemetry)
        self._safe("latest", self._tick_latest)       # twin -> latest-value store
        self._safe("comfort", self._tick_comfort)     # store -> comfort -> store (derived)
        self._safe("memory", self._tick_memory)
        self._safe("hardware", self._tick_hardware)
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

    def _tick_telemetry(self) -> None:
        """One step-cadence row for the Monitor tab (backend.telemetry).
        Read-only observer of both twins; the constraint count per zone comes
        from the store so a chart can mark when a complaint was driving control."""
        t = self.us.t
        active = {z.id: len(self.store.active(t, z.id)) for z in ZONES}
        self.telemetry.record(self.us, self.base, self.zone_power_w,
                              self.zone_cool_w, active)

    def _tick_memory(self) -> None:
        for note in self.memory.tick(self.us, self.store):
            self.add_feed(note)

    def _tick_hardware(self) -> None:
        """Mirror HW_ZONE's commanded vent onto the physical rig, through the
        adapter seam (HttpHVACAdapter.write_vent -> bridge -> node poll reply).
        Same controller, same command, no special case in the control law.
        Inert until a node has POSTed at least once, so an all-sim session
        leaves zero hardware footprints."""
        if self.hw_bridge.has_node():
            self.hw_hvac.write_vent(HW_ZONE, int(self.last_vents.get(HW_ZONE, 0) or 0))

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
        # the twin's own RH helper honours a seasonal rh_fn (Phase 5); identical in classic mode
        return (float(self.us.weather_fn(t)) + self.us.outdoor_offset, self.us._outdoor_rh_at(t))

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

        # RESPONSE SHAPE: `action` is the machine-readable code the docstring
        # promises (applied/cleared/ignored/clarify/noted) — placed AFTER the
        # ** expansion so the feed entry's human sentence cannot shadow it.
        # That sentence still reaches the client as `action_text`; the FEED
        # entry keeps `action` long-form because the dashboard badge reads it.
        def respond(code: str, **extra) -> dict:
            return {"ok": True, **entry, "action_text": entry["action"],
                    "action": code, **extra}

        if parser.detect_retraction(clean):
            if not zone_ids:
                entry["action"] = "noted — glad it's better (no zone named, nothing cleared)"
                self.add_feed(entry)
                return respond("noted")
            with self.lock:
                per_zone = {z: self.store.clear_zone(z, self.us.t) for z in zone_ids}
            n = sum(per_zone.values())
            names = ", ".join(_zone_name(z) for z in zone_ids)
            entry["action"] = f"all-clear — {n} constraint(s) cleared in {names}"
            entry["zones"] = zone_ids
            entry["cleared_by_zone"] = per_zone
            self.add_feed(entry)
            return respond("cleared", cleared=n)

        if not parsed.is_comfort_complaint:
            entry["action"] = "ignored — not a comfort complaint"
            self.add_feed(entry)
            return respond("ignored")

        if not zone_ids:
            entry["action"] = ("clarify — which zone? (" +
                               ", ".join(z.name for z in ZONES) + ")")
            self.add_feed(entry)
            return respond("clarify")

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
        return respond("applied")

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
            acts = self.store.active(self.us.t, z.id)
            rows.append({
                # --- legacy keys: name and meaning frozen ---------------------
                "id": z.id, "name": z.name,
                "temp": round(self.us.T[z.id], 1),
                "base_temp": round(self.base.T[z.id], 1),
                "setpoint": self.last_sps.get(z.id),
                "vent": self.last_vents.get(z.id, 0),
                "occ": int(self.us.occupancy_now(z.id)),
                "offset": adj["setpoint_offset"] if adj else 0.0,
                "active_constraints": len(acts),
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
                # Counted directly, NOT from zone_adjustments: that dict omits a
                # zone whose constraints are ALL pending ("exactly as if nothing
                # had been filed"), which is precisely when an operator most
                # needs to see the count (was a strict-xfail defect).
                "pending_constraints": sum(1 for c in acts
                                           if not c.approved and not c.rejected),
                "alerts": alerts_by_zone.get(z.id, []),
            })
        return rows

    # ------------------------------------------------------------- latest values (Phase 4)
    def prefer(self) -> tuple:
        return (("hardware", "real", "sim", "derived", "predicted") if self.hw_authoritative
                else ("sim", "derived", "hardware", "real", "predicted"))

    def floors(self) -> dict:
        cfg = self.building
        return {z.id: min(cfg.floors, int(cfg.zone_floor.get(z.id, 1))) for z in ZONES}

    def _tick_latest(self) -> None:
        """Publish the twin's current state through the SAME ingest path hardware uses.
        CALLER HOLDS THE LOCK (sim.lock -> store lock is the only lock order)."""
        rows = self.zone_rows()
        tel = self.telemetry.latest()
        co2 = {z: v.get("co2") for z, v in tel["zones"].items()} if tel else {}
        reasons = {getattr(d, "zone", None): getattr(d, "reason_code", None) for d in self.ctrl_us.last_decisions}
        day = self.us.day
        if self._kwh_day is None or self._kwh_day[0] != day:
            self._kwh_day = (day, self.us.kwh if self._kwh_day is not None else 0.0)
        cfg = self.building
        exp = (building.expected_hvac_fraction(cfg, building.expected_fraction(cfg, day, self.us.hour))
               * sum(building.design_power_w(z.id) for z in ZONES))
        occ = sum(r["occ"] for r in rows)
        design = sum(building._OCC_PEAK[z.id] for z in ZONES) or 1
        now = time.time()
        observed = tp.OBSERVED if self.simhw.enabled else frozenset()
        extra = {}
        if self.scenario.internal_load == "profile":
            ghi = self.weather.at(self.us.t)["solar_irradiance_w_m2"] if self.weather else 0.0
            extra = {z.id: {"internal_gain": round(sum(loads_bridge.zone_gains(
                self.building, z.id, self.us.occupancy_now(z.id), self.us.t, ghi).values()), 1)} for z in ZONES}
        tp.publish_twin(self.latest, rows, co2, reasons, LIVE_BUILDING_ID, self.floors(), self.us.t, now, {
            "outdoor_temperature": round(self.outdoor()[0], 2), "occupancy": occ,
            "occupancy_pct": round(100.0 * occ / design, 1),
            "power": round(sum(self.zone_power_w.values()), 1),
            "energy_today": round(max(0.0, self.us.kwh - self._kwh_day[1]), 3),
            "expected_demand": round(exp, 1),
            "cooling_capacity_pct": round(max((r.get("capacity_pct") or 0.0) for r in rows), 1),
            "equipment_power": round(sum(v["internal_gain"] for v in extra.values()), 1) if extra else None},
            observed=observed, zone_extra=extra)
        tp.publish_weather(self.latest, scenario.weather_snapshot(self.us, self.weather), LIVE_BUILDING_ID,
                           self.us.t, now)
        if self.simhw.enabled:
            truth = {r["id"]: {"temperature": self.us.T[r["id"]], "humidity": r.get("rh"),
                               "co2": co2.get(r["id"]), "occupancy": r["occ"]} for r in rows}
            self.simhw.tick(self.latest, truth, self.us.t, dict(building._OCC_PEAK))

    # ------------------------------------------------------------- scenario + simulated hardware (Phase 5)
    def _apply_scenario(self) -> None:
        """Point BOTH twins at the scenario (fair A/B). CALLER HOLDS THE LOCK."""
        self.weather = scenario.make_weather(self.scenario, self.seed)
        for tw in (self.us, self.base):
            scenario.apply_to_twin(tw, self.scenario, self.weather, self.building, self.scenario.climate)

    def scenario_state(self) -> dict:
        with self.lock:
            return {**scenario.summary(self.scenario, self.us, self.weather),
                    "causal": scenario.causal(self.telemetry), "sim_clock": self.clock(), "speed": self.speed,
                    "simulated_hardware": self.simhw.enabled}

    def set_scenario(self, changes: dict) -> dict:
        """Validate every change first; apply nothing unless all pass (400 lists the problems)."""
        from dataclasses import replace as _replace
        errs = []
        with self.lock:
            cfg_changes = {k: changes[k] for k in ("mode", "season", "climate", "envelope_scale", "internal_load")
                           if changes.get(k) is not None}
            if changes.get("cloud_auto"):
                cfg_changes["cloud_cover"] = None
            elif changes.get("cloud_cover") is not None:
                cfg_changes["cloud_cover"] = changes["cloud_cover"]
            if changes.get("rain_auto"):
                cfg_changes["rain_mm_h"] = None
            elif changes.get("rain_mm_h") is not None:
                cfg_changes["rain_mm_h"] = changes["rain_mm_h"]
            if changes.get("clear_disturbance"):
                cfg_changes["disturbance"] = None
            elif changes.get("disturbance_delta_c") is not None:
                d, h = changes["disturbance_delta_c"], changes.get("disturbance_hours") or 3.0
                lo, hi = scenario.LIMITS["disturbance_delta_c"]
                hlo, hhi = scenario.LIMITS["disturbance_hours"]
                if not (lo <= d <= hi and hlo <= h <= hhi):
                    errs.append(f"disturbance_delta_c must be {lo:g}..{hi:g} and disturbance_hours {hlo:g}..{hhi:g}")
                cfg_changes["disturbance"] = {"delta_c": float(d), "t0": self.us.t, "t1": self.us.t + float(h) * 3600}
            new = _replace(self.scenario, **cfg_changes)
            errs += new.validate()
            knobs = {k: changes[k] for k in scenario.KNOB_LIMITS if changes.get(k) is not None}
            for k, v in knobs.items():
                lo, hi = scenario.KNOB_LIMITS[k]
                if not lo <= v <= hi:
                    errs.append(f"{k} must be between {lo:g} and {hi:g}")
            if errs:
                raise ValueError(errs)
            self.scenario = new
            self._apply_scenario()
            if knobs:
                self.set_conditions(**knobs)
            self._safe("latest", self._tick_latest)
        return self.scenario_state()

    def reset_scenario(self) -> dict:
        """Back to the baseline environment: classic weather, default knobs, no overrides, no sensor
        faults. The building, its history, configuration and simulated-hardware on/off are kept."""
        with self.lock:
            self.scenario = scenario.ScenarioConfig()
            self._apply_scenario()
            self.set_conditions(occ_scale=1.0, capacity_scale=1.0, solar_scale=1.0, outdoor_offset=0.0,
                                humidity_offset=0.0)
            self.simhw.clear_faults()
            self._safe("latest", self._tick_latest)
        return self.scenario_state()

    def set_simhw(self, enabled: bool | None = None, noise_level: float | None = None) -> None:
        with self.lock:
            if noise_level is not None:
                self.simhw.noise_level = float(noise_level)
            if enabled is None or bool(enabled) == self.simhw.enabled:
                return
            self.simhw.enabled = bool(enabled)
            if self.simhw.enabled:        # sensors take over: the twin stops filling those values
                self.latest.remove(lambda r: r.get("origin") == "twin" and r["metric"] in tp.OBSERVED)
                for d in self.simhw.devices.values():
                    d.last_sample_wall = None
            else:                         # sensors gone: their readings go, the twin publishes again
                self.latest.remove(lambda r: r.get("origin") == "simulated_hardware"
                                   or str(r.get("device_id") or "").startswith("SIM-"))
                self.simhw.pending.clear()
            self._safe("latest", self._tick_latest)

    # ------------------------------------------------------------- comfort (Phase 3)
    def comfort_readings(self, rows: list | None = None) -> list:
        """ComfortReadings for every zone, read from the LATEST-VALUE STORE (Phase 4) with
        every input's source and age. CALLER HOLDS THE LOCK. `rows` is accepted for
        backward compatibility and ignored."""
        return [tp.comfort_reading(self.latest, z.id, self.prefer()) for z in ZONES]

    def _comfort_readings_from_rows(self, rows: list | None = None) -> list:
        """Phase-3 direct path, kept for reference/tests; not used by the live pipeline."""
        rows = rows if rows is not None else self.zone_rows()
        latest = self.telemetry.latest()
        reasons = {getattr(d, "zone", None): getattr(d, "reason_code", None)
                   for d in self.ctrl_us.last_decisions}
        out = []
        for r in rows:
            tz = (latest or {}).get("zones", {}).get(r["id"])
            mode = "cooling" if (r.get("cool_w") or 0) > 0 else "ventilation" if r.get("vent") else (
                "off" if r.get("setpoint") is None else "idle")
            out.append(comfort.ComfortReading(
                zone_id=r["id"], t=self.us.t, temp_c=r["temp"], rh_pct=r.get("rh"),
                co2_ppm=tz.get("co2") if tz else None, occupancy=r["occ"], occupancy_pct=r.get("occ_pct"),
                hvac={"mode": mode, "cooling_pct": r.get("capacity_pct"), "vent": r.get("vent"),
                      "setpoint": r.get("setpoint"), "at_capacity": r.get("at_capacity"),
                      "reason_code": reasons.get(r["id"])},
                source={"temp_c": "sim", "rh_pct": "sim", "co2_ppm": "derived", "occupancy": "sim"}))
        return out

    def comfort_assessments(self, rows: list | None = None) -> list:
        return [comfort.assess(rd, self.building) for rd in self.comfort_readings(rows)]

    def _zone_meta(self) -> dict:
        cfg = self.building
        return {z.id: {"zone_name": z.name, "role": cfg.zone_roles.get(z.id, z.name),
                       "floor": min(cfg.floors, int(cfg.zone_floor.get(z.id, 1)))} for z in ZONES}

    def _tick_comfort(self) -> None:
        assessments = self.comfort_assessments()
        self.comfort.tick(self.us.t, assessments, self._zone_meta(),
                          {"id": self.building.building_type, "name": self.building.name})
        tp.publish_comfort(self.latest, assessments, self.prefer(), LIVE_BUILDING_ID, self.floors(),
                           self.us.t, time.time())

    def latest_state(self, zone: str | None = None) -> dict:
        """The /api/latest payload. Reads the store only (plus zone names/floors); holds
        sim.lock just long enough to read the clock and config."""
        with self.lock:
            meta, floors = self._zone_meta(), self.floors()
            clock, sim_t = self.clock(), self.us.t
            errors = [dict(v) for v in self.errors.values()]
            binfo = {"building_id": LIVE_BUILDING_ID, "type": self.building.building_type, "name": self.building.name}
        prefer = self.prefer()
        zones = [tp.zone_block(self.latest, z.id, meta[z.id]["zone_name"], floors[z.id], prefer)
                 for z in ZONES if zone is None or z.id == zone]
        bm = {m: tp.view(self.latest.get(lv.BUILDING, m, prefer), m)
              for m in ("outdoor_temperature", "occupancy", "occupancy_pct", "power", "energy_today", "demand",
                        "expected_demand", "comfort_score")}
        if bm["energy_today"]["value"] is not None and self._kwh_day and self._kwh_day[1] == 0.0:
            bm["energy_today"]["note"] = "since simulation start (building started mid-day)"
        status = lv.system_status(self.latest, [z.id for z in ZONES])
        status["subsystem_errors"] = errors
        sim_only = status["status"] == "SIMULATION MODE"
        for m in ("cooling_capacity_pct", "heating_demand", "equipment_power"):
            bm[m] = tp.view(self.latest.get(lv.BUILDING, m, prefer), m)
        weather = {m: tp.view(self.latest.get(lv.BUILDING, m, prefer), m)
                   for m in ("outdoor_temperature",) + tp.WEATHER_METRICS}
        with self.lock:
            causal = scenario.causal(self.telemetry)
            sc = {"mode": self.scenario.mode, "season": self.scenario.season if self.scenario.mode == "seasonal" else None,
                  "climate": self.scenario.climate, "internal_load": self.scenario.internal_load,
                  "envelope_scale": self.scenario.envelope_scale, "weather_model": scenario.weather_snapshot(self.us, self.weather)["model"],
                  "heating": scenario.HEATING_NOTE}
            simhw_on = self.simhw.enabled
        return {"timestamp": lv.iso(time.time()), "sim_t": sim_t, "sim_clock": clock,
                "poll_interval_s": POLL_INTERVAL_S, "building": {**binfo, "metrics": bm},
                "weather": weather, "scenario": sc, "causal": causal,
                "telemetry_mode": ("SIMULATED HARDWARE — not real hardware" if simhw_on else "DIGITAL TWIN (direct)"),
                "zones": zones, "data_quality": status["counts"], "system_health": status,
                "sources": tp.source_summary(zones), "hardware_authoritative": self.hw_authoritative,
                "notice": ("All current values come from the digital twin (SIMULATED) or are derived from it — "
                           "this is not physical building telemetry." if sim_only else
                           "Current values mix digital-twin (SIMULATED/DERIVED) and hardware readings; "
                           "each value carries its own source.")}

    def comfort_snapshot(self, floor: int | None = None, zone: str | None = None,
                         issue: str | None = None) -> dict:
        """The /api/comfort payload. CALLER HOLDS THE LOCK."""
        t, meta = self.us.t, self._zone_meta()
        assessments = self.comfort_assessments()
        summary = comfort.building_summary(assessments)
        summary["average_event_duration_s"] = self.comfort.average_event_duration_s(t)
        summary["open_events"] = len(self.comfort.open)
        zones = []
        for a in assessments:
            m = meta[a["zone_id"]]
            if floor is not None and m["floor"] != floor:
                continue
            if zone and a["zone_id"] != zone:
                continue
            if issue and issue not in a["issues"]:
                continue
            zones.append({**a, **m, "durations": self.comfort.durations(a["zone_id"], t),
                          "open_events": [e["event_id"] for e in self.comfort.list(t, a["zone_id"], status="open")]})
        floors: dict = {}
        for zid, m in meta.items():
            floors.setdefault(m["floor"], []).append(zid)
        return {"sim_clock": self.clock(), "sim_t": t,
                "building": {"type": self.building.building_type, "name": self.building.name},
                "thresholds": {"comfort_c": [self.building.comfort_min_c, self.building.comfort_max_c],
                               "humidity_pct": [self.building.humidity_min_pct, self.building.humidity_max_pct],
                               "co2_ppm": self.building.co2_max_ppm,
                               "basis": "indicative / operator configurable"},
                "weights": comfort.weights_for(self.building),
                "priority": comfort.controller_preference(self.building),
                "summary": summary, "zones": zones,
                "floors": [{"floor": f, "zones": sorted(z)} for f, z in sorted(floors.items())],
                "events_open": self.comfort.list(t, status="open"),
                "filters": {"floor": floor, "zone": zone, "issue": issue},
                "index": "engineering comfort index — not a PMV/PPD certification",
                # surfaced, not hidden: the live twin's RH is pinned high by a documented
                # modelling approximation, so humidity discomfort here is partly a model artefact
                "model_notes": ([telemetry.RH_LIMITATION_NOTE]
                                if any(a["humidity"]["status"] == "Humid" and a["humidity"]["source"] == "sim"
                                       for a in assessments) else []),
                "sources": {"sim": "digital twin state", "derived": "computed by backend/comfort.py",
                            "historical": "generated historical dataset", "predicted": "what-if on clones"}}

    def comfort_history_live(self, zone: str, range_s: float, max_points: int = 240) -> dict:
        """Comfort over time from the step-cadence telemetry buffer, scored with the CURRENT
        profile. Rows are stride-sampled (not averaged) to <= max_points, then assessed.
        CALLER HOLDS THE LOCK. Cached per (zone, span, last row t, profile)."""
        last = self.telemetry.latest()
        key = (zone, range_s, last["t"] if last else None, repr(building.config_dict(self.building)))
        hit = self._comfort_hist_cache.get(key)
        if hit is not None:
            return hit
        rows = self.telemetry.rows_between(last["t"] - range_s, None) if last else []
        stride = max(1, -(-len(rows) // max_points))
        pts = []
        for r in rows[::stride]:
            zids = [zone] if zone != "all" else list(r["zones"])
            scored = []
            for zid in zids:
                z = r["zones"][zid]
                a = comfort.assess(comfort.ComfortReading(zone_id=zid, t=r["t"], temp_c=z["temp"], rh_pct=z["rh"],
                                                          co2_ppm=z["co2"], occupancy=z["occ"],
                                                          occupancy_pct=z["occ_pct"]), self.building)
                scored.append((a, z))
            occ = [a for a, _ in scored if a["comfort_relevant"] and a["score"] is not None]
            allv = [a for a, _ in scored if a["score"] is not None]
            mean = lambda xs, k: round(sum(x[k] for x in xs) / len(xs), 2) if xs else None   # noqa: E731
            zs = [z for _, z in scored]
            pts.append({"t": r["t"], "comfort_score": mean(allv, "score"),
                        "occupied_comfort_score": mean(occ, "score"),
                        "temperature_c": mean(zs, "temp"), "humidity_pct": mean(zs, "rh"),
                        "co2_ppm": mean(zs, "co2"), "occupancy": sum(z["occ"] for z in zs),
                        "occupancy_pct": mean(zs, "occ_pct"),
                        "uncomfortable_zones": sum(1 for a in occ if a["condition_severity"] != "none")})
        span = (rows[-1]["t"] - rows[0]["t"]) if len(rows) > 1 else 0.0
        out = {"source": "sim", "score_source": "derived", "zone": zone, "points": pts,
               "requested_s": range_s, "covered_s": span, "stride": stride,
               "available": bool(pts),
               "message": None if pts else "No data available",
               "partial": bool(pts) and span + 120 < range_s,
               "note": "Live twin telemetry (7 sim-day buffer), scored with the current building profile."}
        if len(self._comfort_hist_cache) > 32:
            self._comfort_hist_cache.clear()
        self._comfort_hist_cache[key] = out
        return out

    # ------------------------------------------------------------- building
    def apply_operating_mode(self) -> tuple:
        """Pull the controller levers the building's operating mode maps onto.

        backend.building.mode_levers() names an (objective, safety_mode) pair;
        they are written exactly as POST /api/controller would write them, so
        there is still ONE place control configuration lives (the controller)
        and the mode is a named preset over it. CALLER HOLDS THE LOCK.
        OUTPUT: the (objective, safety_mode) applied.
        """
        obj, mode = building.mode_levers(self.building)
        self.objective = obj
        self.store.set_objective(obj)
        self.requested_safety_mode = mode
        self._sync_controller()
        return obj, mode

    def operating_mode_state(self) -> dict:
        """The mode plus whether the controller still matches it (an operator
        may have changed the objective on the Control tab since). CALLER HOLDS THE LOCK."""
        obj, mode = building.mode_levers(self.building)
        key = self.building.operating_mode
        return {"key": key, "description": building.OPERATING_MODES[key],
                "objective": obj, "safety_mode": mode,
                "in_sync": self.objective == obj and self.requested_safety_mode == mode,
                "controller_objective": self.objective,
                "controller_safety_mode": self.ctrl_us.safety_mode,
                "modes": dict(building.OPERATING_MODES)}

    def building_snapshot(self) -> dict:
        """The /api/building payload: configuration, topology, zone cards,
        building demand and the executive KPIs. CALLER HOLDS THE LOCK.
        Pure projection of live state through backend.building — nothing is
        written anywhere."""
        cfg, t = self.building, self.us.t
        hw = self.hw_bridge.status()
        maint = self.monitor.alerts()
        cards = building.zone_cards(cfg, self.zone_rows(), self.telemetry.latest(), maint,
                                    self.us.day, self.us.hour, hw)
        day_start = (t // 86400) * 86400
        tel_rows = self.telemetry.rows_between(day_start - 2 * DT, None)
        peak = building.peak_today(tel_rows, t)
        saved = max(0.0, self.base.kwh - self.us.kwh)
        meters = {"us": self.us.metrics(), "base": self.base.metrics(),
                  "saved_pct": round(100.0 * saved / self.base.kwh, 1)
                  if self.base.kwh > 1e-6 else 0.0}
        ext_ok = bool(self.external.snapshot(0, 0)["available"])
        health = building.system_health(
            [dict(v) for v in self.errors.values()], len(self.telemetry),
            bool(hw["connected"]), self.hw_bridge.has_node(), ext_ok,
            bool(getattr(self.external, "enabled", True)))
        return {
            "sim_clock": self.clock(), "sim_t": t, "day": self.us.day,
            "hour": round(self.us.hour, 2),
            "config": building.config_dict(cfg),
            "operating_mode": self.operating_mode_state(),
            "topology": building.topology(cfg, cards),
            "zones": cards,
            "demand": building.demand_now(cfg, cards, self.us.day, self.us.hour, peak),
            "kpis": building.kpis(cfg, cards, meters, tel_rows, t, peak, maint, health),
            "sources": {
                "sim": "SIMULATED — digital twin state (sim/twin.py)",
                "derived": "DERIVED — computed from twin state by a documented formula",
                "predicted": "PREDICTED — building profile schedule (planning expectation)",
                "hardware": "HARDWARE — real sensor on the bench rig (one zone)",
                "config": "CONFIGURED — operator-set building profile value",
                "none": "NOT MODELLED — no source in this application",
            },
        }

    def zone_detail(self, zone_id: str, window: str = "6h") -> dict:
        """The zone drill-down: live card, trends, the controller's current
        decision and WHY, constraints, alerts and recent events. CALLER HOLDS THE LOCK."""
        snap = self.building_snapshot()
        card = next(c for c in snap["zones"] if c["id"] == zone_id)
        decision = next((to_dict(d) for d in self.ctrl_us.last_decisions
                         if getattr(d, "zone", None) == zone_id), None)
        cons = self.store.explain(zone_id, self.us.t)
        series = self.telemetry.series(zone_id, window, max_points=120)
        pts = []
        for p in series["points"]:
            pts.append({k: p.get(k) for k in ("t", "temp", "rh", "occ", "occ_pct", "co2",
                                               "power_w", "cool_w", "capacity_pct",
                                               "setpoint", "vent", "kwh")})
            pts[-1]["comfort"] = building.comfort_score(self.building, p.get("temp"), p.get("rh"))
        decisions = [{k: d.get(k) for k in ("id", "t", "sim_clock", "summary", "reason_code",
                                            "new_setpoint", "new_vent", "applied")}
                     for d in self.decisions.for_zone(zone_id, 8)]
        feed = [{k: e.get(k) for k in ("author", "text", "action", "sim_clock", "source")}
                for e in self.feed
                if zone_id in (e.get("zones") or [])
                or (e.get("parsed") or {}).get("zone_id") == zone_id][:8]
        return {
            "sim_clock": snap["sim_clock"], "sim_t": snap["sim_t"],
            "zone": card,
            "floor": next(f for f in snap["topology"]["floors"] if f["floor"] == card["floor"]),
            "explanation": building.explain_zone(self.building, card, decision, cons),
            "trends": {"window": window, "points": pts, "count_raw": series["count_raw"],
                       "fields": {**series["fields"], "comfort": "derived"}},
            "alerts": {"maintenance": [a for a in self.monitor.alerts()
                                       if a.get("zone") == zone_id],
                       "thresholds": self.telemetry.alerts(zone_id)},
            "events": {"decisions": decisions, "feed": feed},
            "config": {"comfort_c": [self.building.comfort_min_c, self.building.comfort_max_c],
                       "humidity_pct": [self.building.humidity_min_pct,
                                        self.building.humidity_max_pct],
                       "co2_ppm": self.building.co2_max_ppm},
            # Phase 3: the comfort engine's view of this zone (root cause, action, durations)
            "comfort": {**next(a for a in self.comfort_assessments() if a["zone_id"] == zone_id),
                        "durations": self.comfort.durations(zone_id, self.us.t),
                        "events": self.comfort.list(self.us.t, zone_id, limit=10)},
        }

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
                           "saved_co2": round(saved_kwh * GRID_CO2, 2),
                           # additive: the SAME measured kWh repriced at the
                           # verified TANGEDCO ToD tariff — display only, the
                           # flat-tariff figures above are untouched (§8.11)
                           "tou": tariff.summary(self.history, self.us.hour)},
                "history": list(self.history),          # copy: the loop appends
                "feed": feed,                           # copies: the loop rebinds
                # --- additive top-level ------------------------------------
                "controller": self.controller_state(),
                "alerts": alerts,
                "decisions": self.decisions.recent(DECISIONS_IN_STATE),
                "analytics": {"summary": self.analytics.summary(feed, alerts)},
                "privacy": self.privacy_state(),
                "hardware": {**self.hw_bridge.status(),
                             "ambient": self.hw_sensors.status()["ambient"]},
                # additive: Monitor-tab summary (full data at /api/monitor,
                # /api/telemetry, /api/external, /api/dataset, /api/forecast)
                "monitor": {"rows": len(self.telemetry),
                            "alerts": len(self.telemetry.alerts()),
                            "external_available": bool(self.external.snapshot(0, 0)["available"]),
                            "dataset_available": self.dataset.available},
                # additive: which building profile is configured (full payload
                # at /api/building)
                "building": {"type": self.building.building_type,
                             "type_label": building.TYPE_LABELS[self.building.building_type],
                             "name": self.building.name,
                             "operating_mode": self.building.operating_mode},
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
# --------------------------------------------------------------------------
# Phase 6 security wiring. The configuration is validated first: outside development an insecure
# configuration refuses to start (fail closed, never silently insecure).
# --------------------------------------------------------------------------
SEC = SecurityState()
_SEC_ERRORS = SEC.config.validate()
if _SEC_ERRORS and SEC.config.mode != "development":
    raise RuntimeError("FeelsLike refuses to start with an insecure security configuration: " + "; ".join(_SEC_ERRORS))

SEC_REGISTRY = DeviceRegistry()
_SIM_PROTOCOL = {"mqtt": "mqtt-simulated", "modbus": "modbus-simulated", "bacnet": "bacnet-simulated",
                 "https": "https-simulated"}
for _d in sim.simhw.devices.values():                 # SIMULATED identities: keys never leave the process
    SEC_REGISTRY.register(_d.device_id, building_id=_d.building_id, floor_id=_d.floor_id, zone_id=_d.zone_id,
                          metrics=[_d.metric], protocol=_SIM_PROTOCOL[_d.protocol], simulated=True,
                          sensor_type=_d.sensor_type, firmware_version=_d.firmware_version,
                          sampling_interval_s=_d.sampling_interval_s)
if SEC.config.device_keys_file:
    SEC_REGISTRY.load_keys_file(SEC.config.device_keys_file)
SEC_INGEST = SecureIngest(SEC_REGISTRY, sim.latest, SEC.audit, SEC.config)
SIM_MQTT = SimulatedMqttBroker(SEC_REGISTRY, SEC.audit)
SIM_MQTT_ADAPTER = MqttTelemetryAdapter(SIM_MQTT, SEC_INGEST)
_SIM_LAST_ENVELOPE: dict = {}


def security_reconfigure(config) -> None:
    """Swap the security configuration at runtime (tests, admin tooling). CORS is fixed at start-up."""
    SEC.configure(config)
    SEC_INGEST.config, SEC_INGEST.audit, SIM_MQTT.audit = config, SEC.audit, SEC.audit


def _simhw_secure_sink(dev, reading):
    """SIMULATED sensor -> SIMULATED secure adapter -> the same SecureIngest used by real devices.
    Security faults deliberately break one step of the chain so the pipeline's response can be seen."""
    ident = SEC_REGISTRY.get(dev.device_id)
    now = time.time()
    if ident is not None and ident.simulated:
        if dev.fault == "expired_identity":
            ident.credential_expires_at = now - 1
        elif ident.credential_expires_at is not None and ident.credential_expires_at < now:
            ident.credential_expires_at = None
    env = {"device_id": dev.device_id, "seq": int(reading["seq"]), "ts": float(reading["t_wall"]),
           "readings": [{"metric": dev.metric, "value": reading["value"], "sim_t": reading.get("sim_t")}]}
    if dev.fault == "unknown_identity":
        env["device_id"] = "SIM-ROGUE-" + dev.zone_id.upper().replace("_", "-")
    if dev.fault == "malformed":
        env["readings"] = "corrupted-frame"
    if dev.fault == "replay" and dev.device_id in _SIM_LAST_ENVELOPE:
        env = dict(_SIM_LAST_ENVELOPE[dev.device_id])        # resend a captured, already-accepted message
    else:
        if dev.fault == "bad_credentials" or env["device_id"] != dev.device_id:
            env["sig"] = sec_sign(_secrets.token_bytes(32), env)
        else:
            env["sig"] = SEC_REGISTRY.sign_for(dev.device_id, env)
        if dev.fault not in ("malformed", "unknown_identity", "bad_credentials"):
            _SIM_LAST_ENVELOPE[dev.device_id] = dict(env)
    zone_claim = dev.zone_id
    if dev.fault == "spoof_zone":
        zone_claim = next(z.id for z in ZONES if z.id != dev.zone_id)
    if dev.protocol == "mqtt":
        cid = dev.device_id
        if cid not in SIM_MQTT.sessions:
            key = SEC_REGISTRY._keys.get(dev.device_id)
            if key is None or not SIM_MQTT.connect(cid, dev.device_id, mqtt_password(key, cid)):
                return None
        out = SIM_MQTT.publish(cid, telemetry_topic(dev.building_id, zone_claim), json.dumps(env).encode(), qos=1)
        res = (out.get("results") or [None])[0] if out.get("ok") else None
    else:
        if dev.fault == "spoof_zone":
            env["zone_id"] = zone_claim
        res = SEC_INGEST.process(env, transport=_SIM_PROTOCOL[dev.protocol], require_signature=True)
    if not res:
        return None
    for r in res.get("results", []):
        if r.get("reading") is not None:
            return r["reading"]
        if r.get("stored") == "invalid":
            return sim.latest.get(dev.zone_id, dev.metric) and next(
                (x for x in sim.latest.query(sensor=dev.device_id)), None)
    return None


sim.simhw.sink = _simhw_secure_sink

app = FastAPI(title="FeelsLike", dependencies=[Depends(make_enforce(SEC))])
app.add_middleware(SecurityMiddleware, state=SEC)
app.add_middleware(CORSMiddleware, **SEC.config.cors(), allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-Request-ID"], allow_credentials=False,
                   expose_headers=["X-Request-ID", "X-FeelsLike-Security"])
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


class HwReadingIn(BaseModel):
    # extra="allow", unlike every other new body: firmware in the field may grow
    # fields (battery_v, rssi) without a lockstep server deploy. The bridge
    # validates the fields it actually uses and ignores the rest.
    model_config = ConfigDict(extra="allow")
    node_id: str
    temp_c: float
    rh_pct: float | None = None
    seq: int = 0
    uptime_s: float = 0.0


class HwHeaterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool


class HwSensorIn(BaseModel):
    # Sensor-only node body. extra="allow" for the same reason as HwReadingIn:
    # firmware may grow fields without a lockstep server deploy.
    model_config = ConfigDict(extra="allow")
    node_id: str
    temp_c: float | None = None
    fault: str | None = None
    counts: float | None = None
    seq: int = 0
    uptime_s: float = 0.0
    role: str = "ambient"
    transport: str = "unknown"
    calibrated: bool = False


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
    Teams outgoing-webhook JSON bodies work too (fields: text, from.name).

    SECURITY (Phase 6): with FL_SLACK_SIGNING_SECRET set, every request must carry a valid Slack
    signature (v0 HMAC-SHA256 over timestamp + body, 5-minute window). Outside development the
    endpoint is refused unless the secret is configured."""
    await _verify_slack(request)
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
    action = r["action"]  # machine-readable code: applied/cleared/clarify/ignored/noted
    if action == "applied":
        reply = (f":thermometer: Got it — *{p['issue'].replace('_', ' ')}* in "
                 f"*{zone}* (severity {p['severity']}). "
                 f"{r['explanation']['summary']} {badge}")
    elif action == "cleared":
        reply = f":white_check_mark: All clear for *{zone}* — {r['cleared']} constraint(s) lifted. {badge}"
    elif action == "clarify":
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
def set_controller(body: ControllerIn, request: Request):
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
    # Phase 6: each field needs its own permission; zone locks are zone-scoped
    if body.objective is not None:
        principal = require(SEC, request, "controller:objective", action="controller objective")
    if body.safety_mode is not None:
        principal = require(SEC, request, "controller:safety", action="controller safety mode")
    for z in (body.lock_zone, body.unlock_zone):
        if z is not None:
            principal = require(SEC, request, "hvac:control", zone=z, action="zone lock")
    with sim.lock:
        before = {"objective": sim.objective, "safety_mode": sim.requested_safety_mode,
                  "locked_zones": sorted(sim.operator_locks)}
    SEC.audit.record("control_command", principal.username, "controller", target="controller",
                     zone=body.lock_zone or body.unlock_zone, result="accepted", role=principal.role,
                     old=before, new={k: v for k, v in body.model_dump().items() if v is not None},
                     request_id=request.scope.get("fl_request_id"))
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


def _constraint_zone_guard(cid: int, request: Request, action: str) -> None:
    """Zone authorization for constraint decisions: the zone comes from the store, not the client."""
    with sim.lock:
        zone = next((c.zone for c in sim.store.items if c.id == cid), None)
    if zone is None:
        raise HTTPException(404, f"no constraint with id {cid}")
    p = require(SEC, request, "hvac:control", zone=zone, action=action)
    SEC.audit.record("control_command", p.username, action, target=f"constraint {cid}", zone=zone,
                     result="accepted", role=p.role, request_id=request.scope.get("fl_request_id"))


@app.post("/api/constraints/{cid}/approve")
def approve_constraint(cid: int, request: Request):
    """Approve a withheld constraint so it starts influencing control.

    INPUT: cid — the integer Constraint.id (path segment).
    OUTPUT: {"ok": true, "approved": cid, ...the /api/constraints payload}.
    SIDE EFFECTS: sets approved=True / rejected=False on that constraint. The
      decay clock is NOT reset: approving 40 minutes late applies what is left of
      the complaint, not a fresh one.
    ERROR STATES: 404 for an unknown id; 422 for a non-integer path segment.
    """
    _constraint_zone_guard(cid, request, "approve constraint")
    with sim.lock:
        if not sim.store.approve(cid):
            raise HTTPException(404, f"no constraint with id {cid}")
        return {"ok": True, "approved": cid, **_constraint_payload()}


@app.post("/api/constraints/{cid}/reject")
def reject_constraint(cid: int, request: Request):
    """Reject a constraint: it stays in history but never moves a setpoint.

    INPUT: cid — the integer Constraint.id (path segment).
    OUTPUT: {"ok": true, "rejected": cid, ...the /api/constraints payload}.
    SIDE EFFECTS: sets approved=False / rejected=True. created_t is untouched, so
      the comfort-memory pattern miner still sees that the complaint happened.
    ERROR STATES: 404 for an unknown id; 422 for a non-integer path segment.
    """
    _constraint_zone_guard(cid, request, "reject constraint")
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
# hardware-in-the-loop (the shoebox rig; see backend/hardware.py)
# ==========================================================================

def _legacy_device_gate(payload: dict, request: Request) -> None:
    """Phase 6 gate for the rig / ambient-node endpoints (existing firmware posts plain JSON).

    development: allowed unauthenticated (labelled LEGACY). Otherwise the node must be a registered,
    non-quarantined device and sign {"device_id", "seq", "ts", "body"} with its HMAC key
    ("sig" field), with a fresh timestamp and a strictly increasing seq. The firmware in hardware/
    does not sign yet — enabling enforced mode requires that firmware update (docs/SECURITY.md)."""
    if SEC.config.legacy_device_endpoints:
        return
    node = payload.get("node_id")
    ident = SEC_REGISTRY.get(node) if isinstance(node, str) else None
    rid = request.scope.get("fl_request_id")
    if ident is None:
        SEC.audit.record("unknown_device", str(node)[:64], "legacy device post", result="rejected",
                         reason="device not registered", request_id=rid, ip=client_ip(request))
        raise HTTPException(401, "Device authentication failed.")
    if ident.status in ("INACTIVE", "QUARANTINED"):
        raise HTTPException(403, "Device not permitted.")
    ts, seq = payload.get("ts"), payload.get("seq")
    env = {"device_id": node, "seq": seq, "ts": ts,
           "body": {k: v for k, v in payload.items() if k not in ("sig", "ts", "seq", "node_id")},
           "sig": payload.get("sig")}
    fresh = isinstance(ts, (int, float)) and abs(time.time() - float(ts)) <= SEC.config.telemetry_freshness_s
    if not fresh or SEC_REGISTRY.credential_state(ident) != "valid" or not SEC_REGISTRY.verify(node, env):
        SEC_INGEST._penalize(node, "legacy device authentication failed", "http-legacy")
        SEC.audit.record("auth_failure", node, "legacy device post", result="rejected",
                         reason="signature/timestamp/credential check failed", request_id=rid)
        raise HTTPException(401, "Device authentication failed.")
    if ident.last_seq is not None and (not isinstance(seq, int) or seq <= ident.last_seq):
        SEC.audit.record("replay_detected", node, "legacy device post", result="rejected",
                         reason="non-increasing sequence number", request_id=rid)
        raise HTTPException(409, "Replayed or out-of-order message.")
    ident.last_seq, ident.last_seen = seq, time.time()


@app.post("/api/hw/reading")
def hw_reading(body: HwReadingIn, request: Request):
    """The ESP32 node's poll: one reading in, the actuator commands back.

    INPUT: {"node_id": str, "temp_c": float, "rh_pct": float|null, "seq": int,
      "uptime_s": float} — extra fields are accepted and ignored.
    OUTPUT: {"ok": true, "fan": 0|1|2, "heater": bool, "watchdog_s", "poll_s"}.
      Field names are FROZEN — the firmware parses them by name.
    SIDE EFFECTS: stores the reading (bounded log), marks the node live; the
      fan command it returns is whatever the controller last wrote for HW_ZONE
      through HttpHVACAdapter.
    ERROR STATES: 422 for a missing/implausible field (temp outside -20..70,
      RH outside 0..100), naming the field.
    """
    _legacy_device_gate(body.model_dump(), request)
    try:
        out = sim.hw_bridge.post_reading(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    # Phase 4 hardware seam: the accepted reading enters the SAME latest-value pipeline
    reading = sim.hw_bridge.status()["reading"]
    if reading:
        with sim.lock:
            floor = sim.floors().get(HW_ZONE, 1)
        tp.publish_hardware(sim.latest, reading, HW_ZONE, LIVE_BUILDING_ID, floor)
    return out


@app.get("/api/hw/status")
def hw_status():
    """Everything about the rig: bridge state, inferred sensor health, honest
    HVAC capabilities, recent command audit.

    INPUT: none. OUTPUT: the bridge status() block plus sensor_health (the
      SensorAdapter's INFERRED verdict — no_data / stale / stuck / out_of_range),
      hvac_capabilities (vent-only, and says so) and recent_writes.
      connected=false with null reading when no node has ever posted.
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    st = sim.hw_bridge.status()
    st["sensor_health"] = sim.hw_sensor.health(HW_ZONE)
    st["hvac_capabilities"] = sim.hw_hvac.capabilities()
    st["recent_writes"] = list(sim.hw_bridge.writes[-20:])
    st["ambient"] = sim.hw_sensors.status()["ambient"]
    return st


@app.post("/api/hw/heater")
def hw_heater(body: HwHeaterIn, request: Request):
    """Manual calibration heater. NOT a controller output — the heater is the
    step-input for sim-to-real calibration, driven by a human only.

    INPUT: {"on": bool}.
    OUTPUT: {"ok": true, "requested", "heater", "duty_limited", "duty"} —
      heater is what the duty envelope actually allows (max 50% of any
      10-minute window); duty_limited=true means the cap is holding it off.
    SIDE EFFECTS: changes the command the node receives on its next poll.
    ERROR STATES: 422 for a malformed body; 403 when the user is not authorized for the rig's zone.
    """
    p = require(SEC, request, "hvac:control", zone=HW_ZONE, action="rig heater")
    out = sim.hw_bridge.set_heater(body.on)
    SEC.audit.record("control_command", p.username, "rig heater", target="heater", zone=HW_ZONE,
                     result="accepted", new={"on": body.on}, applied=out.get("heater"),
                     request_id=request.scope.get("fl_request_id"))
    return {"ok": True, **out}


@app.get("/api/hw/log")
def hw_log(limit: int = 4096):
    """The reading log, oldest first — the calibration script's input.

    INPUT: ?limit=1..8192 (default 4096).
    OUTPUT: {"zone", "rows": [{node_id, temp_c, rh_pct, seq, uptime_s, t_wall}],
      "count"}. Empty rows when no node has posted.
    SIDE EFFECTS: none. ERROR STATES: 400 for a limit outside 1..8192.
    """
    if not 1 <= limit <= READING_LOG_MAX:
        raise HTTPException(400, f"limit must be between 1 and {READING_LOG_MAX}")
    rows = sim.hw_bridge.log_rows(limit)
    return {"zone": HW_ZONE, "rows": rows, "count": len(rows)}


@app.post("/api/hw/sensor")
def hw_sensor(body: HwSensorIn, request: Request):
    """A sensor-only node's reading (the Uno ambient reference, via
    scripts/serial_bridge.py). Separate from /api/hw/reading on purpose: the
    reply is an acknowledgement, never an actuator command, and it cannot
    overwrite the shoebox rig's reading.

    INPUT: {"node_id": str, "temp_c": float | null, "fault": str | null,
      "counts": float | null, "seq": int, "uptime_s": float, "role": str,
      "transport": str, "calibrated": bool}. temp_c OR fault; extra fields are
      ignored. calibrated is the node's own claim that it was cross-calibrated;
      the dashboard labels every other reading uncalibrated.
    OUTPUT: {"ok": true, "node_id", "accepted"}. accepted=false for a fault.
    SIDE EFFECTS: stores the reading in that node's bounded log.
    ERROR STATES: 422 naming the field for a missing or implausible value.
    """
    _legacy_device_gate(body.model_dump(), request)
    try:
        out = sim.hw_sensors.post(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if out.get("accepted"):
        tp.publish_ambient(sim.latest, body.model_dump(), time.time(), LIVE_BUILDING_ID)
    return out


@app.get("/api/hw/sensors")
def hw_sensors():
    """Every sensor-only node: latest reading, staleness, inferred health, and
    the current ambient reference (null unless a connected ambient node's
    latest reading is a real temperature).

    INPUT: none. OUTPUT: {"nodes": [...], "ambient": {...} | null, "stale_after_s"}.
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    return sim.hw_sensors.status()


@app.get("/api/hw/sensors/{node_id}/log")
def hw_sensor_log(node_id: str, limit: int = 4096):
    """One sensor-only node's reading log, oldest first (cross-calibration input).

    INPUT: node_id (path segment), ?limit=1..8192 (default 4096).
    OUTPUT: {"node_id", "rows", "count"}.
    SIDE EFFECTS: none. ERROR STATES: 400 for a bad limit; 404 for a node that
      has never posted.
    """
    if not 1 <= limit <= SENSOR_NODE_LOG_MAX:
        raise HTTPException(400, f"limit must be between 1 and {SENSOR_NODE_LOG_MAX}")
    try:
        rows = sim.hw_sensors.log_rows(node_id, limit)
    except KeyError:
        raise HTTPException(404, f"no sensor node {node_id!r} has posted") from None
    return {"node_id": node_id, "rows": rows, "count": len(rows)}


# ==========================================================================
# monitoring (Monitor tab): step-cadence telemetry, KPIs, alerts, forecast,
# the real external feed and the historical dataset — see backend/telemetry.py
# and backend/external.py for the provenance rules every payload carries.
# ==========================================================================

MONITOR_ZONES = ["all"] + [z.id for z in ZONES]


def _check_zone(zone: str) -> str:
    if zone not in MONITOR_ZONES:
        raise HTTPException(400, f"unknown zone {zone!r}; expected one of {MONITOR_ZONES}")
    return zone


@app.get("/api/telemetry")
def get_telemetry(zone: str = "all", window: str = "24h",
                  t_from: float | None = None, t_to: float | None = None,
                  max_points: int = telemetry.DEFAULT_MAX_POINTS):
    """Step-cadence series for one zone (or the whole building) over a window.

    INPUT: ?zone=all|zone_a..e, ?window=live|1h|6h|24h|7d|custom (custom takes
      t_from / t_to in sim seconds), ?max_points=2..2000 (bucket-averaged).
    OUTPUT: {"zone","window","t_from","t_to","step_s","points":[...],
      "count_raw","fields": {field: "sim"|"derived"}, "source": "sim"} — every
      field's provenance travels with the data. co2 and comfort are DERIVED
      (documented formulas in backend/telemetry.py), never sensor readings.
    SIDE EFFECTS: none. ERROR STATES: 400 for an unknown zone/window or a
      max_points outside 2..2000.
    """
    _check_zone(zone)
    if window not in telemetry.WINDOWS and window != "custom":
        raise HTTPException(400, f"unknown window {window!r}; expected one of "
                                 f"{sorted(telemetry.WINDOWS) + ['custom']}")
    if not 2 <= max_points <= 2000:
        raise HTTPException(400, "max_points must be between 2 and 2000")
    with sim.lock:
        out = sim.telemetry.series(zone, window, t_from, t_to, max_points)
    out["source"] = "sim"
    out["co2_estimated"] = True
    return out


@app.get("/api/monitor")
def get_monitor(zone: str = "all"):
    """One call for the Monitor tab's KPI strip: current vs previous values,
    status per metric, threshold table, active threshold alerts, the rig's
    latest real reading and the external feed's current values.

    INPUT: ?zone=all|zone_id.
    OUTPUT: {"kpis": telemetry.kpis(), "alerts": [...], "thresholds": {...},
      "hardware": {...real rig reading or connected=false...},
      "external": {...current outdoor real values or available=false...},
      "sources": legend text per source tag, "sim_clock", "sim_t"}.
    SIDE EFFECTS: none. ERROR STATES: 400 for an unknown zone.
    """
    _check_zone(zone)
    with sim.lock:
        kpis = sim.telemetry.kpis(zone)
        alerts = sim.telemetry.alerts(zone)
        clock, t = sim.clock(), sim.us.t
        hw = sim.hw_bridge.status()
    hw["sensor_health"] = sim.hw_sensor.health(HW_ZONE)
    hw["ambient"] = sim.hw_sensors.status()["ambient"]
    ext = sim.external.snapshot(0, 0)
    return {
        "zone": zone, "sim_clock": clock, "sim_t": t,
        "kpis": kpis, "alerts": alerts, "thresholds": telemetry.THRESHOLDS,
        "hardware": hw,
        "external": {k: ext[k] for k in ("available", "provider", "site", "current",
                                          "fetched_at", "age_s", "error", "attribution")},
        "sources": {
            "sim": "Digital twin state (sim/twin.py) — simulated building, seeded weather",
            "derived": "Computed from twin state by a documented formula (backend/telemetry.py)",
            "hardware": "ESP32 shoebox rig, real sensor over HTTP (backend/hardware.py) — one zone",
            "real": "Open-Meteo live outdoor weather / air quality for the site (backend/external.py)",
            "historical": "UCI Occupancy Detection dataset replay (data/uci_occupancy.csv)",
            "predicted": "Same controller run forward on clones (backend/telemetry.forecast)",
        },
    }


@app.get("/api/forecast")
def get_forecast(zone: str = "all", horizon_h: float = 3.0, max_points: int = 90):
    """PREDICTED series: the live building stepped forward on throwaway clones.

    INPUT: ?zone=all|zone_id, ?horizon_h in (0, 24], ?max_points 2..500.
    OUTPUT: {"kind":"predicted","horizon_h","zone","t_from","t_to","points",
      "note"} — kwh is the delta over the horizon. Runs the same ConstraintAware
      controller (same objective) with the live constraint store cloned.
    SIDE EFFECTS: none on live state. ERROR STATES: 400 for bad arguments.
    """
    _check_zone(zone)
    if not 0.0 < horizon_h <= 24.0:
        raise HTTPException(400, "horizon_h must be > 0 and <= 24")
    if not 2 <= max_points <= 500:
        raise HTTPException(400, "max_points must be between 2 and 500")
    with sim.lock:
        twin, store = sim.us.clone(), sim.store.clone()
        objective, co2 = sim.objective, dict(sim.telemetry.co2)
    return telemetry.forecast(twin, store, lambda: ConstraintAware(objective=objective),
                              horizon_h, zone, max_points, co2_init=co2)


@app.get("/api/external")
def get_external(hours_past: float = 168.0, hours_ahead: float = 24.0):
    """The REAL outdoor feed (Open-Meteo) for the configured site.

    INPUT: ?hours_past (default 168 = 7 d), ?hours_ahead (default 24).
    OUTPUT: backend.external.ExternalFeed.snapshot() — available=false with the
      reason in "error" when offline or disabled; never a 500.
    SIDE EFFECTS: none (the refresher runs on its own thread).
    """
    return sim.external.snapshot(max(0.0, hours_past), max(0.0, hours_ahead))


@app.post("/api/external/refresh")
def refresh_external():
    """Force one synchronous fetch (a few seconds). OUTPUT: the new snapshot."""
    ok = sim.external.refresh()
    out = sim.external.snapshot(0, 0)
    out["refreshed"] = ok
    return out


@app.get("/api/dataset")
def get_dataset(window: str = "24h", end: str | None = None,
                start: str | None = None, max_points: int = 360):
    """HISTORICAL dataset replay (UCI occupancy, data/uci_occupancy.csv).

    INPUT: ?window=live|1h|6h|24h|7d|custom, ?end / ?start ISO timestamps
      ("2015-02-10 09:00:00"), ?max_points 2..2000.
    OUTPUT: {"info": dataset provenance, "points": [...], ...}.
    SIDE EFFECTS: none. ERROR STATES: 400 for bad arguments.
    """
    from backend.external import DATASET_WINDOWS
    if window not in DATASET_WINDOWS and window != "custom":
        raise HTTPException(400, f"unknown window {window!r}")
    if not 2 <= max_points <= 2000:
        raise HTTPException(400, "max_points must be between 2 and 2000")
    out = sim.dataset.window(window, end, start, max_points)
    out["info"] = sim.dataset.info()
    return out


# ==========================================================================
# RL trajectory (file-backed, like /api/experiments: committed artifacts only)
# ==========================================================================

@app.get("/api/rl")
def get_rl():
    """The PPO training trajectory and the measured ablation it lost on.

    INPUT: none. Reads rl/models/progress.csv (SB3 logger output — written by
      `python -m rl.train`, NEVER by this process) and evals/results_energy.json
      (written by scripts.demo_day). What a judge sees is what was committed.
    OUTPUT: {"available", "points": [{steps, ep_rew_mean}] thinned to <= 240,
      "final": the last point, "table": the four measured controller rows,
      "decision": the M4 sentence, "note"}. Missing/corrupt files degrade to
      available=false with the reason in "note" — an empty-but-valid payload,
      never a 500.
    SIDE EFFECTS: none. ERROR STATES: none by design.
    """
    out: dict = {
        "available": False, "points": [], "final": None, "table": [],
        "decision": ("M4 decision: the demo ships ConstraintAware. PPO saves more "
                     "energy (512.7 kWh, -29.0%) but books 22 violation-minutes; "
                     "the shipped controller holds 530.3 kWh (-26.6%) at ZERO. "
                     "Zero-violations is the thesis, so RL is shown as trajectory."),
        "note": "",
    }
    try:
        with RL_PROGRESS_FILE.open(newline="", encoding="utf-8") as f:
            rows = [(float(r["time/total_timesteps"]), float(r["rollout/ep_rew_mean"]))
                    for r in csv.DictReader(f)
                    if r.get("time/total_timesteps") and r.get("rollout/ep_rew_mean")]
    except (OSError, ValueError, KeyError) as e:
        out["note"] = (f"rl/models/progress.csv unavailable ({type(e).__name__}: {e}); "
                       f"run `python -m rl.train` to produce it.")
        return out
    if not rows:
        out["note"] = "progress.csv holds no reward rows yet."
        return out
    rows.sort(key=lambda p: p[0])
    step = max(1, -(-len(rows) // RL_CURVE_MAX_POINTS))   # ceil: honor the budget
    thinned = rows[::step]
    if thinned[-1] != rows[-1]:
        thinned.append(rows[-1])
    out["points"] = [{"steps": int(s), "ep_rew_mean": round(v, 3)} for s, v in thinned]
    out["final"] = out["points"][-1]
    out["available"] = True
    try:
        data = json.loads(ENERGY_RESULTS_FILE.read_text(encoding="utf-8"))
        out["table"] = [{"name": k, "kwh": v.get("kwh"), "viol_min": v.get("viol_min"),
                         "saved_pct": v.get("saved_pct_vs_baseline")}
                        for k, v in (data.get("results") or {}).items()]
    except (OSError, ValueError) as e:
        out["note"] = f"results_energy.json unavailable ({type(e).__name__}) — run scripts.demo_day."
    return out


# ==========================================================================
# building (Building tab): commercial profile, topology, demand, KPIs, zone
# drill-down — see backend/building.py for the provenance rules.
# ==========================================================================

class BuildingProfileIn(BaseModel):
    """Every field optional; omitted = unchanged. extra="forbid" so a typo is a
    loud 422. Range and cross-field checks live in backend.building.validate()
    and come back as 400 with the full list of problems."""
    model_config = ConfigDict(extra="forbid")
    building_type: str | None = None
    name: str | None = None
    floors: int | None = None
    zones: int | None = None
    occupancy_capacity: int | None = None
    hvac_capacity_w: float | None = None
    open_hour: float | None = None
    close_hour: float | None = None
    open_days: list[int] | None = None
    comfort_min_c: float | None = None
    comfort_max_c: float | None = None
    humidity_min_pct: float | None = None
    humidity_max_pct: float | None = None
    co2_max_ppm: float | None = None
    comfort_priority: float | None = None
    energy_priority: float | None = None
    occupancy_sensitivity: float | None = None
    base_load_fraction: float | None = None
    expected_occupancy_pct: float | None = None
    oa_per_person_ls: float | None = None
    oa_per_area_ls_m2: float | None = None
    lighting_w_m2: float | None = None
    comfort_weights: dict[str, float] | None = None
    operating_mode: str | None = None


# A change to any of these re-derives the controller levers (objective follows
# the priorities in normal / simulation mode).
_LEVER_FIELDS = {"building_type", "operating_mode", "comfort_priority", "energy_priority"}


@app.get("/api/building")
def get_building():
    """Everything the Building tab renders in one call.

    INPUT: none.
    OUTPUT: {"config", "operating_mode", "topology" (floors -> zone ids, unmodelled
      floors flagged), "zones" (cards: temp/humidity/occupancy/CO2/comfort/HVAC/
      energy/demand/status flags, each value source-tagged), "demand" (building
      demand concept), "kpis" (executive strip), "sources", "sim_clock", "sim_t"}.
    SIDE EFFECTS: none. ERROR STATES: none by design.
    """
    with sim.lock:
        return sim.building_snapshot()


@app.get("/api/building/profile")
def get_building_profile():
    """The active building configuration plus the catalog of the six profiles,
    validation limits and operating modes a configuration form needs."""
    with sim.lock:
        return {"config": building.config_dict(sim.building),
                "operating_mode": sim.operating_mode_state(),
                "catalog": building.catalog()}


@app.post("/api/building/profile")
def set_building_profile(body: BuildingProfileIn, request: Request):
    """Switch building type and/or edit profile fields and/or the operating mode.

    INPUT: any subset of BuildingProfileIn. A building_type different from the
      current one first resets every field to that profile's defaults, then the
      other supplied fields apply on top (the operating mode carries over).
    OUTPUT: {"ok", "config", "operating_mode", "levers_applied", "controller"}.
    SIDE EFFECTS: replaces LiveSim.building (survives /api/reset). When the type,
      mode or a priority changed, the mode's (objective, safety_mode) is written to
      the controller — the same levers /api/controller writes. The twin's physics
      and zones are never changed.
    ERROR STATES: 400 when no field is supplied or validation fails (detail
      carries {"errors": [...]}, nothing is applied); 422 for unknown fields or
      wrong types.
    """
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "supply at least one building profile field")
    # Phase 6 field-level authorization: energy managers may change only energy-related settings
    p = request.state.principal
    if not p.can("building:configure"):
        if set(changes) - ENERGY_FIELDS or changes.get("operating_mode", "normal") not in ENERGY_MODES:
            SEC.audit.record("authorization_failure", p.username, "building profile", result="rejected",
                             reason=f"role {p.role} may change only {sorted(ENERGY_FIELDS)} (non-emergency modes)",
                             fields=sorted(changes))
            raise HTTPException(403, "Not permitted.")
    with sim.lock:
        before = {k: getattr(sim.building, k, None) for k in changes}
    SEC.audit.record("configuration_change", p.username, "building profile", target="building", result="accepted",
                     role=p.role, fields=sorted(changes), old=before, new=changes,
                     request_id=request.scope.get("fl_request_id"))
    with sim.lock:
        new, errs = building.apply_update(sim.building, changes)
        if errs:
            raise HTTPException(400, {"errors": errs})
        sim.building = new
        if sim.scenario.internal_load == "profile":
            sim._apply_scenario()           # profile loads follow the new building profile
        levers = bool(_LEVER_FIELDS & set(changes))
        if levers:
            sim.apply_operating_mode()
        return {"ok": True, "config": building.config_dict(sim.building),
                "operating_mode": sim.operating_mode_state(),
                "levers_applied": levers, "controller": sim.controller_state()}


@app.get("/api/building/zones/{zone_id}")
def get_building_zone(zone_id: str, window: str = "6h"):
    """Zone drill-down: live card, trends over ?window (live|1h|6h|24h|7d),
    the controller's current decision with the factual WHY, constraints,
    alerts and recent events.

    ERROR STATES: 404 for an unknown zone; 400 for an unknown window.
    """
    if zone_id not in ZONE_BY_ID:
        raise HTTPException(404, f"unknown zone {zone_id!r}; expected one of {list(ZONE_BY_ID)}")
    if window not in telemetry.WINDOWS:
        raise HTTPException(400, f"unknown window {window!r}; expected one of "
                                 f"{sorted(telemetry.WINDOWS)}")
    with sim.lock:
        return sim.zone_detail(zone_id, window)


@app.get("/api/building/demand")
def get_building_demand(zone: str = "all", hours: int = 24, ahead_h: int = 6):
    """Hourly actual (telemetry) vs expected (profile schedule) demand.

    INPUT: ?zone=all|zone_id, ?hours 1..168 of history, ?ahead_h 0..24 expected-only.
    OUTPUT: backend.building.demand_series() — unsampled hours carry actual_* null.
    SIDE EFFECTS: none. ERROR STATES: 400 for bad arguments.
    """
    _check_zone(zone)
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours must be between 1 and 168")
    if not 0 <= ahead_h <= 24:
        raise HTTPException(400, "ahead_h must be between 0 and 24")
    with sim.lock:
        t = sim.us.t
        rows = sim.telemetry.rows_between(t - (hours + 1) * 3600.0, None)
        out = building.demand_series(sim.building, rows, t, zone, hours, ahead_h)
        out["building"] = {"type": sim.building.building_type, "name": sim.building.name}
    return out


# ==========================================================================
# security (Phase 6) — authentication, users, devices, secure ingestion, secure zone control,
# occupant rooms, security health. docs/SECURITY.md. Nothing here returns secrets except a new
# device key, exactly once, to an admin.
# ==========================================================================

async def _verify_slack(request: Request) -> None:
    secret = os.environ.get("FL_SLACK_SIGNING_SECRET")
    if not secret:
        if SEC.config.auth_enabled:
            raise HTTPException(403, "Integration not configured.")
        return
    ts = request.headers.get("x-slack-request-timestamp", "")
    sig = request.headers.get("x-slack-signature", "")
    body = await request.body()
    ok = ts.isdigit() and abs(time.time() - int(ts)) <= 300
    if ok:
        expected = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expected, sig)
    if not ok:
        SEC.audit.record("auth_failure", "slack", "slack webhook", result="rejected", reason="invalid Slack signature")
        raise HTTPException(401, "Authentication failed.")


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


@app.post("/api/security/login")
def security_login(body: LoginIn, request: Request):
    """Exchange username + password for an opaque bearer token (Authorization: Bearer <token>)."""
    ip = client_ip(request)
    ok, retry = SEC.limiter.allow("login", f"{ip}:{body.username[:32]}")
    if not ok:
        SEC.audit.record("rate_limited", body.username[:32], "login", result="rejected", reason="login rate limit", ip=ip)
        raise HTTPException(429, "Too many requests.", headers={"Retry-After": str(int(retry) + 1)})
    user = SEC.users.verify(body.username, body.password)
    if user is None:
        SEC.audit.record("auth_failure", body.username[:32], "login", result="rejected",
                         reason="invalid credentials", ip=ip, request_id=request.scope.get("fl_request_id"))
        raise HTTPException(401, "Authentication failed.")
    token, exp = SEC.sessions.issue(user)
    SEC.audit.record("auth_success", user.username, "login", result="success", role=user.role, ip=ip)
    return {"token": token, "token_type": "bearer", "expires_at": exp, "user": user.public(),
            "permissions": sorted(SEC_ROLES[user.role])}


@app.post("/api/security/logout")
def security_logout(request: Request):
    auth = request.headers.get("authorization", "")
    revoked = SEC.sessions.revoke(auth[7:].strip()) if auth.lower().startswith("bearer ") else False
    SEC.audit.record("logout", request.state.principal.username, "logout", result="success" if revoked else "noop")
    return {"ok": True, "revoked": revoked}


@app.get("/api/security/whoami")
def security_whoami(request: Request):
    p = request.state.principal
    return {"principal": p.public(), "mode": SEC.config.mode, "auth_enabled": SEC.config.auth_enabled,
            "label": ("DEVELOPMENT — authentication not enforced; plain HTTP, not encrypted in transit"
                      if SEC.config.mode == "development" else
                      "ENFORCED — authentication required" + ("; HTTPS required" if SEC.config.https_required
                                                               else "; plain HTTP (local demo, not encrypted)"))}


def _security_metrics() -> dict:
    a = SEC.audit
    devices = SEC_REGISTRY.list()
    by_status = {}
    for d in devices:
        by_status[d["effective_status"]] = by_status.get(d["effective_status"], 0) + 1
    ing = SEC_INGEST.metrics()
    return {
        "security_events": {
            "failed_authentication": a.counts.get("auth_failure", 0),
            "unauthorized_requests": a.counts.get("authorization_failure", 0),
            "unauthenticated_requests": a.counts.get("unauthenticated_request", 0),
            "rate_limit_violations": a.counts.get("rate_limited", 0),
            "replay_attempts": ing["replay"], "duplicate_messages": ing["DUPLICATE"],
            "unknown_device_attempts": ing["UNKNOWN_DEVICE"], "spoof_attempts": ing["spoof"],
            "device_auth_failures": ing["auth_failed"], "quarantined_devices": by_status.get("QUARANTINED", 0),
            "control_commands_last_5min": a.count_since("control_command", 300),
            "failed_auth_last_5min": a.count_since("auth_failure", 300)},
        "telemetry_security": {"envelopes": ing["envelopes"], "accepted": ing["ACCEPTED"], "stale": ing["STALE"],
                               "rejected": ing["REJECTED"], "malformed": ing["malformed"],
                               "recent_rejections": list(SEC_INGEST.rejections)[-25:]},
        "devices": {"total": len(devices), "by_status": by_status,
                    "simulated": sum(1 for d in devices if d["simulated"]),
                    "hardware": sum(1 for d in devices if not d["simulated"]),
                    "offline": by_status.get("OFFLINE", 0)},
        # data quality is reported separately: a stale or invalid reading is not a security event
        "data_quality": sim.latest.counts(),
    }


@app.get("/api/security/status")
def security_status(request: Request):
    p = request.state.principal
    warnings = list(SEC.config.validate())
    if SEC.config.mode == "development":
        warnings.append("Development mode: authentication is not enforced and traffic is plain HTTP.")
    if not SEC.config.https_required:
        warnings.append("HTTPS is not required: not encrypted in transit.")
    with sim.lock:
        sim_health = {"steps": sim.steps, "subsystem_errors": [dict(v) for v in sim.errors.values()],
                      "speed": sim.speed, "simulated_hardware": sim.simhw.enabled}
    return {"config": SEC.config.public(), "warnings": warnings, "principal": p.public(),
            "active_sessions": SEC.sessions.active(), "api": {"status": "ok"},
            "telemetry_health": lv.system_status(sim.latest, [z.id for z in ZONES]),
            "simulator": sim_health, "metrics": _security_metrics(),
            "protocols": {"https_device_ingest": "implemented (POST /api/telemetry/ingest, HMAC-signed envelopes); "
                                                 "encrypted in transit only behind TLS",
                          "mqtt": SIM_MQTT.label, "mqtt_broker_stats": dict(SIM_MQTT.stats),
                          "mqtts_client": "configuration + validation only (MqttsClientConfig); broker deployment required",
                          "bacnet_modbus": "local building-network protocols behind a registered gateway only; "
                                           "BACnet/SC not implemented"}}


@app.get("/api/security/metrics")
def security_metrics():
    return _security_metrics()


@app.get("/api/security/events")
def security_events(event_type: str | None = None, result: str | None = None, limit: int = 200):
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")
    return {"events": SEC.audit.list(event_type=event_type, result=result, limit=limit),
            "counts": dict(SEC.audit.counts)}


@app.get("/api/security/devices")
def security_devices():
    return {"devices": SEC_REGISTRY.list(), "unknown_attempts": list(SEC_INGEST.unknown_attempts)[-50:],
            "recent_rejections": list(SEC_INGEST.rejections)[-50:],
            "note": "Key material is never returned here. SIMULATED devices are not physical hardware."}


class DeviceRegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str
    zone_id: str
    metrics: list[str]
    protocol: str = "https"
    device_type: str = "sensor"
    sensor_type: str | None = None
    firmware_version: str | None = None
    sampling_interval_s: float | None = None
    credential_ttl_s: float | None = None


@app.post("/api/security/devices")
def security_register_device(body: DeviceRegisterIn, request: Request):
    """Register a PHYSICAL device. Returns its HMAC key exactly once; store it on the device."""
    if body.zone_id not in ZONE_BY_ID:
        raise HTTPException(400, "unknown zone")
    if body.device_type not in ("sensor", "sensor_node", "gateway"):
        raise HTTPException(400, "device_type must be sensor, sensor_node or gateway")
    bad = [m for m in body.metrics if lv.ALIASES.get(m, m) not in lv.METRICS]
    if bad:
        raise HTTPException(400, f"unknown metrics {bad}")
    if body.credential_ttl_s is not None and not 3600 <= body.credential_ttl_s <= 400 * 86400:
        raise HTTPException(400, "credential_ttl_s must be between 3600 and 400 days")
    with sim.lock:
        floor = f"F{sim.floors().get(body.zone_id, 1)}"
    try:
        ident, key = SEC_REGISTRY.register(body.device_id, building_id=LIVE_BUILDING_ID, floor_id=floor,
                                           zone_id=body.zone_id, metrics=body.metrics, protocol=body.protocol,
                                           simulated=False, device_type=body.device_type, sensor_type=body.sensor_type,
                                           firmware_version=body.firmware_version,
                                           sampling_interval_s=body.sampling_interval_s,
                                           credential_ttl_s=body.credential_ttl_s)
    except ValueError as e:
        raise HTTPException(400, {"errors": e.args[0]})
    SEC.audit.record("device_registered", request.state.principal.username, "register device", target=body.device_id,
                     zone=body.zone_id, result="success", protocol=body.protocol, metrics=body.metrics)
    return {"device": SEC_REGISTRY.public(ident), "device_key": key,
            "warning": "This key is shown once. It is not stored anywhere readable and cannot be retrieved again."}


@app.post("/api/security/devices/{device_id}/{action}")
def security_device_action(device_id: str, action: str, request: Request):
    """disable | enable | quarantine | reinstate | rotate | revoke."""
    ident = SEC_REGISTRY.get(device_id)
    if ident is None:
        raise HTTPException(404, "unknown device")
    actor = request.state.principal.username
    key = None
    if action in ("disable", "enable", "quarantine", "reinstate"):
        status = {"disable": "INACTIVE", "enable": "ACTIVE", "reinstate": "ACTIVE", "quarantine": "QUARANTINED"}[action]
        SEC_REGISTRY.set_status(device_id, status, reason=f"manual {action} by {actor}")
        if status == "ACTIVE":
            SEC_INGEST.clear_failures(device_id)
        SEC.audit.record("device_status_changed", actor, action, target=device_id, zone=ident.zone_id, result="success",
                         new_status=SEC_REGISTRY.get(device_id).status)
    elif action == "rotate":
        new = SEC_REGISTRY.rotate_key(device_id)
        key = None if ident.simulated else new
        SEC.audit.record("device_key_rotated", actor, "rotate key", target=device_id, zone=ident.zone_id,
                         result="success", key_id=SEC_REGISTRY.get(device_id).key_id)
    elif action == "revoke":
        SEC_REGISTRY.revoke_credential(device_id)
        SEC.audit.record("device_status_changed", actor, "revoke credential", target=device_id, result="success")
    else:
        raise HTTPException(400, "action must be disable, enable, quarantine, reinstate, rotate or revoke")
    out = {"device": SEC_REGISTRY.public(SEC_REGISTRY.get(device_id))}
    if key:
        out["device_key"] = key
        out["warning"] = "This key is shown once."
    return out


class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str
    role: str
    zones: list[str] | None = None


class UserUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = None
    zones: list[str] | None = None
    disabled: bool | None = None


@app.get("/api/security/users")
def security_users():
    return {"users": [u.public() for u in SEC.users.users.values()],
            "roles": {r: sorted(p) for r, p in SEC_ROLES.items()},
            "note": "In-memory user changes last until restart; persist users with scripts/security_users.py."}


@app.post("/api/security/users")
def security_create_user(body: UserIn, request: Request):
    try:
        u = SEC.users.add(body.username, body.password, body.role, body.zones)
    except ValueError as e:
        raise HTTPException(400, {"errors": e.args[0] if isinstance(e.args[0], list) else [str(e.args[0])]})
    SEC.audit.record("user_created", request.state.principal.username, "create user", target=u.username,
                     result="success", role=u.role, zones=u.zones)
    return {"user": u.public()}


@app.post("/api/security/users/{username}")
def security_update_user(username: str, body: UserUpdateIn, request: Request):
    fields = body.model_dump(exclude_unset=True)
    try:
        before = SEC.users.users[username].public()
        u = SEC.users.update(username, role=fields.get("role"), zones=fields["zones"] if "zones" in fields else ...,
                             disabled=fields.get("disabled"))
    except KeyError:
        raise HTTPException(404, "unknown user")
    except ValueError as e:
        raise HTTPException(400, {"errors": e.args[0]})
    revoked = SEC.sessions.revoke_user(username)
    SEC.audit.record("role_change", request.state.principal.username, "update user", target=username, result="success",
                     old={k: before[k] for k in ("role", "zones", "disabled")},
                     new={k: u.public()[k] for k in ("role", "zones", "disabled")}, sessions_revoked=revoked)
    return {"user": u.public(), "sessions_revoked": revoked}


@app.post("/api/telemetry/ingest")
async def telemetry_ingest(request: Request):
    """HTTPS device ingestion: one HMAC-signed envelope (docs/SECURITY.md §7). The trusted source comes
    from the device registry, never from the payload. Encrypted in transit only when served over TLS."""
    raw = await request.body()
    if len(raw) > 16384:
        raise HTTPException(413, "Payload too large.")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        envelope = None
    dev = envelope.get("device_id") if isinstance(envelope, dict) else None
    ok, retry = SEC.limiter.allow("telemetry", str(dev)[:64] if isinstance(dev, str) else client_ip(request))
    if not ok:
        SEC.audit.record("rate_limited", str(dev)[:64], "telemetry ingest", result="rejected", reason="telemetry rate limit")
        raise HTTPException(429, "Too many requests.", headers={"Retry-After": str(int(retry) + 1)})
    res = SEC_INGEST.process(envelope, transport="https")
    reason = res.get("reason") or ""
    code = 200
    if res["status"] == "UNKNOWN_DEVICE" or reason == "device authentication failed":
        code = 401
    elif res["status"] == "QUARANTINED" or "does not match" in reason or reason == "device disabled":
        code = 403
    elif "replay" in reason or "sequence number outside" in reason:
        code = 409
    elif reason.startswith("malformed") or "timestamp" in reason:
        code = 422
    body = {"status": res["status"], "device_id": res.get("device_id"), "accepted": res.get("accepted", 0),
            "reason": reason or None, "trusted_source": res.get("trusted_source"),
            "results": [{k: v for k, v in r.items() if k != "reading"} for r in res.get("results", [])]}
    if code >= 400:
        body = {"status": res["status"], "detail": {401: "Device authentication failed.", 403: "Device not permitted.",
                                                     409: "Replayed or out-of-order message.",
                                                     422: "Invalid telemetry."}[code], "reason": reason}
    return JSONResponse(body, status_code=code)


class ZoneControlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str | None = None              # cooler | warmer | more_ventilation | less_ventilation | clear
    target_setpoint_c: float | None = None
    severity: int = 2
    reason: str | None = None


ZONE_ACTIONS = {"cooler": "too_hot", "warmer": "too_cold", "more_ventilation": "stuffy", "less_ventilation": "drafty"}


@app.post("/api/control/zone/{zone_id}")
def control_zone(zone_id: str, body: ZoneControlIn, request: Request):
    """Secure zone HVAC command: authenticate -> role -> zone -> validate -> safe bounds -> operating mode ->
    execute through the EXISTING constraint store (the controller applies it on its next step, inside its
    21.5-29 °C / ±1.8 K / fan 0-2 envelope) -> audit with old and requested values."""
    if zone_id not in ZONE_BY_ID:
        raise HTTPException(404, "unknown zone")
    p = require(SEC, request, "hvac:control", zone=zone_id, action="zone control")
    if (body.action is None) == (body.target_setpoint_c is None):
        raise HTTPException(400, "supply exactly one of action or target_setpoint_c")
    if body.action is not None and body.action not in (*ZONE_ACTIONS, "clear"):
        raise HTTPException(400, f"action must be one of {[*ZONE_ACTIONS, 'clear']}")
    if not 1 <= body.severity <= 3:
        raise HTTPException(400, "severity must be 1..3")
    if body.target_setpoint_c is not None and not 21.5 <= body.target_setpoint_c <= 29.0:
        raise HTTPException(400, "target_setpoint_c must be within the controller's safe envelope 21.5..29.0 °C")
    reason = (body.reason or "")[:200]
    rid = request.scope.get("fl_request_id")
    with sim.lock:
        mode = sim.ctrl_us.safety_mode
        if mode == "emergency_override" or (mode == "maintenance_lockout" and zone_id in sim.ctrl_us.locked_zones):
            SEC.audit.record("control_command", p.username, "zone control", target=zone_id, zone=zone_id,
                             result="rejected", reason=f"operating mode {mode} does not permit zone control", request_id=rid)
            raise HTTPException(409, "The current operating mode does not permit zone control.")
        old_sp = sim.last_sps.get(zone_id)
        t = sim.us.t
        applied = {}
        if body.action == "clear":
            n = sim.store.clear_zone(zone_id, t)
            applied = {"cleared_constraints": n}
        else:
            if body.target_setpoint_c is not None:
                current = old_sp if old_sp is not None else sim.ctrl_us.base_occupied
                delta = body.target_setpoint_c - current
                if abs(delta) < 0.2:
                    applied = {"no_change": True, "reason": "target within 0.2 K of the current setpoint"}
                    issue = None
                else:
                    issue = "too_hot" if delta < 0 else "too_cold"
                    mags = {s: abs(ISSUE_EFFECTS[issue][0][s]) for s in (1, 2, 3)}
                    sev = min(mags, key=lambda s: abs(mags[s] - abs(delta)))
            else:
                issue, sev = ZONE_ACTIONS[body.action], body.severity
            if issue:
                created = sim.store.add_many([zone_id], issue, sev, 1.0, t,
                                             text=f"operator command ({body.action or 'target setpoint'}): {reason}".strip(),
                                             author=f"operator:{p.username}")
                applied = {"issue": issue, "severity": sev, "constraint_ids": [c.id for c in created],
                           "expected_offset_c": ISSUE_EFFECTS[issue][0][sev],
                           "expected_vent_delta": ISSUE_EFFECTS[issue][1]}
    SEC.audit.record("control_command", p.username, "zone control", target=zone_id, zone=zone_id, result="accepted",
                     role=p.role, old={"setpoint_c": old_sp},
                     new={"action": body.action, "target_setpoint_c": body.target_setpoint_c, **applied},
                     reason=reason or None, request_id=rid)
    return {"ok": True, "zone_id": zone_id, "old_setpoint_c": old_sp, "applied": applied,
            "note": "Applied through the constraint store; the controller writes it on its next step within its "
                    "safety envelope (it may be clamped or arbitrated with occupant complaints)."}


@app.get("/api/occupant/rooms")
def occupant_rooms(request: Request):
    """Minimal room list for the occupant page: names, temperatures, targets, request counts. No controller,
    hardware, device or security internals. Occupants with assigned rooms see only those rooms."""
    p = request.state.principal
    with sim.lock:
        rows = sim.zone_rows()
        t_out = sim.outdoor()[0]
        clock = sim.clock()
    return {"sim": {"clock": clock, "t_out": round(t_out, 1)},
            "zones": [{"id": r["id"], "name": r["name"], "temp": r["temp"], "setpoint": r["setpoint"],
                       "active_constraints": r["active_constraints"]} for r in rows if p.can_zone(r["id"])]}


# ==========================================================================
# scenario + simulated hardware (Phase 5) — backend/scenario.py, backend/simhw.py.
# SIMULATED HARDWARE IS NOT REAL HARDWARE.
# ==========================================================================

class ScenarioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = None
    season: str | None = None
    climate: str | None = None
    cloud_cover: float | None = None
    cloud_auto: bool | None = None
    rain_mm_h: float | None = None
    rain_auto: bool | None = None
    envelope_scale: float | None = None
    internal_load: str | None = None
    outdoor_offset: float | None = None
    humidity_offset: float | None = None
    solar_scale: float | None = None
    occ_scale: float | None = None
    capacity_scale: float | None = None
    disturbance_delta_c: float | None = None
    disturbance_hours: float | None = None
    clear_disturbance: bool | None = None


class SimHwIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    noise_level: float | None = None


class SimHwFaultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    duration_s: float | None = None


class SimHwConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sampling_interval_s: float | None = None
    noise_sd: float | None = None
    bias: float | None = None
    drift_per_hour: float | None = None
    failure_probability: float | None = None
    comm_delay_s: float | None = None


@app.get("/api/scenario")
def get_scenario():
    """Current environment: weather model, season, climate, overrides, knobs, envelope, internal
    loads, humidity model, heating status, weather now, speed presets, causal explanation."""
    return sim.scenario_state()


@app.post("/api/scenario")
def post_scenario(body: ScenarioIn):
    """Change the environment the twin runs in. All values bounded; nothing is applied unless
    every field is valid (400 with the list). The weather model itself is never modified —
    overrides are a layer, removed by /api/scenario/reset."""
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "supply at least one scenario field")
    try:
        return sim.set_scenario(changes)
    except ValueError as e:
        raise HTTPException(400, {"errors": e.args[0] if isinstance(e.args[0], list) else [str(e)]})


@app.post("/api/scenario/reset")
def post_scenario_reset():
    """Restore the baseline environment without rebuilding the building or touching history."""
    return sim.reset_scenario()


@app.get("/api/scenario/compare")
def get_scenario_compare(horizon_h: float = 6.0):
    """Summer vs monsoon vs winter vs transition from the current state — SIMULATED / WHAT-IF on clones."""
    if not 0 < horizon_h <= 24:
        raise HTTPException(400, "horizon_h must be > 0 and <= 24")
    with sim.lock:
        twin, store, cfg = sim.us.clone(), sim.store.clone(), sim.scenario
        objective, co2, profile = sim.objective, dict(sim.telemetry.co2), sim.building
        before = whatif.state_fingerprint(sim.us, sim.store)
    out = scenario.compare_seasons(twin, store, lambda: ConstraintAware(objective=objective), cfg, profile,
                                   horizon_h, co2_init=co2)
    with sim.lock:
        out["live_state_untouched"] = True  # clones only; the live twin keeps stepping on its own
        out["fingerprint_at_start"] = before
    return out


@app.get("/api/simhw")
def get_simhw():
    """SIMULATED hardware registry: device -> sensor -> zone -> floor -> building, protocol (simulated),
    sampling and characteristics, fault, status from the latest-value store."""
    return {"enabled": sim.simhw.enabled, "noise_level": sim.simhw.noise_level,
            "notice": "SIMULATED HARDWARE — these are not physical devices; protocols are in-process simulations.",
            "devices": sim.simhw.registry(sim.latest), "faults": list(SIMHW_FAULTS),
            "protocols": {k: f"SIMULATED {k.upper()}" for k in SIMHW_ADAPTERS},
            "config_limits": {k: list(v) for k, v in SIMHW_LIMITS.items()}}


@app.post("/api/simhw")
def post_simhw(body: SimHwIn):
    """Enable / disable simulated hardware (and its global noise level 0..3)."""
    if body.noise_level is not None and not 0 <= body.noise_level <= 3:
        raise HTTPException(400, "noise_level must be between 0 and 3")
    sim.set_simhw(body.enabled, body.noise_level)
    return get_simhw()


@app.post("/api/simhw/devices/{device_id}/fault")
def post_simhw_fault(device_id: str, body: SimHwFaultIn):
    """Inject a controlled fault (none|offline|stuck|drift|invalid|delay), optionally for duration_s.
    Only registry ids are accepted. The device samples once immediately so the effect is visible."""
    try:
        with sim.lock:
            sim.simhw.set_fault(device_id, body.mode, body.duration_s)
            sim._safe("latest", sim._tick_latest)
            truth = {r["id"]: {"temperature": sim.us.T[r["id"]], "humidity": r.get("rh"),
                               "co2": (sim.telemetry.latest() or {}).get("zones", {}).get(r["id"], {}).get("co2"),
                               "occupancy": r["occ"]} for r in sim.zone_rows()}
            sim.simhw.tick(sim.latest, truth, sim.us.t, dict(building._OCC_PEAK), force=True, only=device_id)
    except KeyError:
        raise HTTPException(404, f"unknown simulated device {device_id!r}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return next(d for d in sim.simhw.registry(sim.latest) if d["device_id"] == device_id)


@app.post("/api/simhw/devices/{device_id}/config")
def post_simhw_config(device_id: str, body: SimHwConfigIn):
    """Set a simulated sensor's characteristics (bounded)."""
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    if not params:
        raise HTTPException(400, "supply at least one parameter")
    try:
        with sim.lock:
            sim.simhw.configure(device_id, **params)
    except KeyError:
        raise HTTPException(404, f"unknown simulated device {device_id!r}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return next(d for d in sim.simhw.registry(sim.latest) if d["device_id"] == device_id)


# ==========================================================================
# latest values (Phase 4): "what is happening right now" — backend/latest.py.
# The digital twin is the main source today; that is NOT physical telemetry.
# Route order matters: the fixed paths are declared before /api/latest/{metric}.
# ==========================================================================

@app.get("/api/latest")
def get_latest(zone: str | None = None):
    """Current building state: every value with unit, source, quality, timestamp, age.
    Missing values say "No current data available" — never a fake zero.
    ERROR STATES: 400 unknown zone."""
    if zone is not None and zone not in ZONE_BY_ID:
        raise HTTPException(400, f"unknown zone {zone!r}; expected one of {list(ZONE_BY_ID)}")
    return sim.latest_state(zone)


@app.get("/api/latest/health")
def get_latest_health():
    """Telemetry health (counts by quality, last update, status) + sensor status counts."""
    st = lv.system_status(sim.latest, [z.id for z in ZONES])
    sensors = sim.latest.sensors()
    by = {}
    for s in sensors:
        by[s["status"]] = by.get(s["status"], 0) + 1
    return {"system_health": st, "sensor_status": by, "sensors": len(sensors),
            "stale_after_s": lv.STALE_S, "aging_after_s": lv.GOOD_S, "offline_after_s": lv.OFFLINE_S}


@app.get("/api/latest/sensors")
def get_latest_sensors(zone: str | None = None, source: str | None = None, status: str | None = None):
    """Per-sensor health. Twin sensors are explicitly simulated (SIM-/EST-/DRV- ids)."""
    if source is not None and source not in lv.SOURCES:
        raise HTTPException(400, f"source must be one of {list(lv.SOURCES)}")
    rows = [s for s in sim.latest.sensors() if (zone is None or s["zone_id"] == zone)
            and (source is None or s["source"] == source) and (status is None or s["status"] == status)]
    return {"sensors": rows, "count": len(rows)}


@app.get("/api/latest/trend")
def get_latest_trend(zone: str = "zone_a", metrics: str = "temperature,hvac_power,occupancy_pct,comfort_score",
                     minutes: float = 15.0):
    """Recent in-memory buffer (<= 15 min wall clock) for a live trend. Not history."""
    if zone != lv.BUILDING and zone not in ZONE_BY_ID:
        raise HTTPException(400, f"unknown zone {zone!r}")
    if not 0 < minutes <= lv.BUFFER_S / 60:
        raise HTTPException(400, "minutes must be > 0 and <= 15")
    ms = [m.strip() for m in metrics.split(",") if m.strip()]
    bad = [m for m in ms if lv.ALIASES.get(m, m) not in lv.METRICS]
    if bad:
        raise HTTPException(400, f"unknown metrics {bad}")
    out = {m: sim.latest.recent(zone, m, minutes * 60) for m in ms}
    return {"zone": zone, "minutes": minutes, "series": out,
            "label": "LIVE SIMULATION" if all(s["source"] in ("sim", "derived", "predicted")
                                             for v in out.values() for s in v) else "LIVE (mixed sources)"}


@app.get("/api/latest/zone/{zone_id}")
def get_latest_zone(zone_id: str):
    """One zone's current state + alerts (maintenance, thresholds, open comfort events,
    data-quality) + alternatives from other sources + its sensors."""
    if zone_id not in ZONE_BY_ID:
        raise HTTPException(404, f"unknown zone {zone_id!r}")
    state = sim.latest_state(zone_id)
    block = state["zones"][0]
    with sim.lock:
        maint = [a for a in sim.monitor.alerts() if a.get("zone") == zone_id]
        thresh = sim.telemetry.alerts(zone_id)
        events = sim.comfort.list(sim.us.t, zone_id, status="open")
    dq = [{"kind": "sensor_" + s["status"].lower(), "sensor_id": s["sensor_id"], "metric": s["metric"],
           "age_s": s["age_s"], "source": s["source"]}
          for s in sim.latest.sensors() if s["zone_id"] == zone_id and s["status"] in ("Stale", "Invalid", "Offline")]
    return {**block, "timestamp": state["timestamp"], "sim_clock": state["sim_clock"],
            "system_health": state["system_health"]["status"],
            "alerts": {"maintenance": maint, "thresholds": thresh, "comfort_events": events, "data_quality": dq},
            "sensors": [s for s in sim.latest.sensors() if s["zone_id"] == zone_id]}


@app.get("/api/latest/{metric}")
def get_latest_metric(metric: str, zone: str | None = None, source: str | None = None, sensor: str | None = None):
    """Latest readings of one metric (temperature, humidity, co2, occupancy, hvac_power, energy,
    demand, comfort_score, …), optionally filtered by zone / source / sensor."""
    m = lv.ALIASES.get(metric, metric)
    if m not in lv.METRICS:
        raise HTTPException(404, f"unknown metric {metric!r}; expected one of {sorted(lv.METRICS)}")
    if source is not None and source not in lv.SOURCES:
        raise HTTPException(400, f"source must be one of {list(lv.SOURCES)}")
    rs = sim.latest.query(zone=zone, metric=m, source=source, sensor=sensor)
    return {"metric": m, "unit": lv.METRICS[m][0], "readings": [tp.view(r, m) | {"zone_id": r["zone_id"]} for r in rs],
            "message": None if rs else tp.NOT_AVAILABLE}


# ==========================================================================
# comfort (Phase 3): occupant comfort engine, events, durations, history,
# trade-off and the occupant view — see backend/comfort.py and docs/COMFORT.md.
# ==========================================================================

COMFORT_RANGES = {"1h": 3600.0, "6h": 6 * 3600.0, "12h": 12 * 3600.0, "24h": 86400.0,
                  "7d": 7 * 86400.0, "30d": 30 * 86400.0}


def _check_comfort_zone(zone: str | None) -> None:
    if zone is not None and zone not in ZONE_BY_ID:
        raise HTTPException(400, f"unknown zone {zone!r}; expected one of {list(ZONE_BY_ID)}")


def _check_issue(issue: str | None) -> None:
    if issue is not None and issue not in comfort.ISSUES:
        raise HTTPException(400, f"unknown issue {issue!r}; expected one of {list(comfort.ISSUES)}")


@app.get("/api/comfort")
def get_comfort(floor: int | None = None, zone: str | None = None, issue: str | None = None):
    """Building comfort: KPIs, every zone's assessment (score, per-dimension status,
    factors, root cause, recommendation, occupancy relevance, data quality), durations,
    open events, weights and priority. Filters: ?floor, ?zone, ?issue.
    ERROR STATES: 400 for unknown zone / issue."""
    _check_comfort_zone(zone)
    _check_issue(issue)
    with sim.lock:
        return sim.comfort_snapshot(floor, zone, issue)


@app.get("/api/comfort/zone/{zone_id}")
def get_comfort_zone(zone_id: str):
    """One zone: assessment + root cause + durations + its events + 6 h live trend."""
    if zone_id not in ZONE_BY_ID:
        raise HTTPException(404, f"unknown zone {zone_id!r}")
    with sim.lock:
        snap = sim.comfort_snapshot(zone=zone_id)
        return {**snap["zones"][0], "sim_clock": snap["sim_clock"], "thresholds": snap["thresholds"],
                "events": sim.comfort.list(sim.us.t, zone_id, limit=50),
                "trend": sim.comfort_history_live(zone_id, 6 * 3600.0, 120)}


@app.get("/api/comfort/events")
def get_comfort_events(zone: str | None = None, issue: str | None = None, status: str = "all",
                       limit: int = 100):
    """Comfort events, newest first. ?status=open|resolved|all, ?limit 1..500."""
    _check_comfort_zone(zone)
    _check_issue(issue)
    if status not in ("open", "resolved", "all"):
        raise HTTPException(400, "status must be open, resolved or all")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    with sim.lock:
        return {"events": sim.comfort.list(sim.us.t, zone, issue, status, limit), "source": "derived"}


@app.get("/api/comfort/history")
def get_comfort_history(zone: str = "all", range: str = "6h", source: str = "live",
                        building_id: str | None = None):
    """Comfort over time. source=live: the twin's telemetry buffer (up to 7 sim-days) scored
    with the current profile; ranges beyond the buffer come back partial or with
    "No data available". source=historical: the Phase-2 generated dataset (comfort as stored
    at generation) for ?building_id. Nothing is synthesised to fill a gap."""
    if range not in COMFORT_RANGES:
        raise HTTPException(400, f"range must be one of {list(COMFORT_RANGES)}")
    if source == "live":
        if zone != "all":
            _check_comfort_zone(zone)
        with sim.lock:
            return {**sim.comfort_history_live(zone, COMFORT_RANGES[range]), "range": range}
    if source != "historical":
        raise HTTPException(400, "source must be live or historical")
    if not HISTORY.available():
        return {"source": "historical", "available": False, "points": [], "message": "No data available",
                "hint": "run `python -m scripts.generate_dataset`"}
    level, zid = ("building", None) if zone == "all" else ("zone", zone)
    w = _hist_window(HISTORY, building_id, "30d" if range == "30d" else range if range in dsa.RANGES else "24h",
                     None, None, "auto", level, None, zid)
    if range == "12h":
        w["t0"] = w["t1"] - int(COMFORT_RANGES["12h"])
    rows = HISTORY.series(w["building_id"], w["t0"], w["t1"], w["interval_s"], level, None, zid,
                          ["comfort_score", "temperature_comfort_score", "indoor_temperature_c",
                           "indoor_humidity_percent", "indoor_co2_ppm", "occupancy_count", "occupancy_percent"])
    pts = [{"t": r["t"], "ts": _iso(r["t"]), "comfort_score": r["comfort_score"],
            "temperature_c": r["indoor_temperature_c"], "humidity_pct": r["indoor_humidity_percent"],
            "co2_ppm": r["indoor_co2_ppm"], "occupancy": r["occupancy_count"],
            "occupancy_pct": r["occupancy_percent"]} for r in rows]
    return {"source": "historical", "score_source": "historical", "range": range, "zone": zone,
            "building_id": w["building_id"], "interval_s": w["interval_s"], "points": pts,
            "available": bool(pts), "message": None if pts else "No data available",
            "note": ("Generated dataset (SIMULATED history). comfort_score is as stored at generation "
                     "(weights 60/25/15, backend/dataset/comfort.py), not re-weighted by the live profile.")}


@app.get("/api/comfort/tradeoff")
def get_comfort_tradeoff(zone: str, action: str = "auto", horizon_h: float = 1.0):
    """Comfort vs energy for one comfort action — SIMULATED / WHAT-IF on clones (never a
    guaranteed result). action=auto|cool|raise|vent; horizon_h in (0, 6]."""
    _check_comfort_zone(zone)
    with sim.lock:
        twin, store = sim.us.clone(), sim.store.clone()
        objective, co2 = sim.objective, dict(sim.telemetry.co2)
        cfg = sim.building
        a = next(x for x in sim.comfort_assessments() if x["zone_id"] == zone)
    try:
        return comfort_whatif.tradeoff(twin, store, lambda: ConstraintAware(objective=objective), cfg, zone,
                                       action, horizon_h, co2, a)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/comfort/occupant")
def get_comfort_occupant(zone: str, request: Request):
    """Occupant-facing comfort for one room: plain statuses, three readings, a one-line
    explanation and a short trend. Deliberately excludes hardware, network, controller
    internals and every other zone."""
    _check_comfort_zone(zone)
    require(SEC, request, "view:occupant", zone=zone, action="occupant comfort")
    with sim.lock:
        a = next(x for x in sim.comfort_assessments() if x["zone_id"] == zone)
        trend = sim.comfort_history_live(zone, 6 * 3600.0, 36)
        clock = sim.clock()
    th, hu, aq = a["thermal"], a["humidity"], a["air_quality"]
    if a["status"] == "Unoccupied":
        why = "Nobody is in this room right now, so the building is not conditioning it for comfort."
    elif a["primary_cause"]:
        pc = a["primary_cause"]
        why = (f"{pc['status']}: {pc['value']:g} {pc['unit']} against "
               f"{'a limit of ' + format(pc['target'][1], 'g') if pc['dimension'] == 'air_quality' else format(pc['target'][0], 'g') + '–' + format(pc['target'][1], 'g')} "
               f"{pc['unit']}. The building's response: {a['recommendation']['text']}")
    elif a["score"] is None:
        why = "Room readings are unavailable right now."
    else:
        why = "Temperature, humidity and CO₂ are all within this building's comfort ranges."
    return {"zone_id": zone, "zone_name": ZONE_BY_ID[zone].name, "sim_clock": clock,
            "status": a["status"], "score": a["score"], "occupancy_state": a["occupancy_state"],
            "issue": a["status"] not in ("Excellent", "Comfortable", "Acceptable", "Unoccupied", "Unavailable"),
            "temperature": {"value": th["value"], "status": th["status"], "range": th["target"], "source": th["source"]},
            "humidity": {"value": hu["value"], "status": hu["status"], "range": hu["target"], "source": hu["source"]},
            "air_quality": {"co2_ppm": aq["value"], "status": aq["status"], "limit": aq["target"][1],
                            "source": aq["source"], "label": "CO₂ (ventilation indicator)"},
            "explanation": why,
            "trend": [{"t": p["t"], "score": p["comfort_score"], "temperature_c": p["temperature_c"]}
                      for p in trend["points"]],
            "data_note": "Simulated building (digital twin). Comfort is an engineering index.",
            "updated_age_s": (sim.latest.get(zone, "comfort_score", sim.prefer()) or {}).get("age_s"),
            "source": "derived"}


# ==========================================================================
# history (Phase 2): the generated historical dataset — SIMULATED history in
# SQLite, built by `python -m scripts.generate_dataset`. See backend/dataset/.
# Never labelled live/real/hardware. Separate from the live twin by design.
# ==========================================================================

HISTORY = SqliteStore(os.environ.get("FL_HISTORY_DB") or DEFAULT_HISTORY_DB)
HISTORY_STATE = ("simulated history — generated from the sim/twin.py physics by backend/dataset; "
                 "not live, not real, not hardware")


def _history():
    if not HISTORY.available():
        raise HTTPException(503, {"available": False,
                                  "hint": "no dataset yet: run `python -m scripts.generate_dataset`"})
    return HISTORY


def _hist_window(store, building_id, range_, start_time, end_time, interval, level, floor_id, zone_id):
    """Resolve and validate the common history filters -> dict."""
    cat = store.catalog()
    ids = [b["building_id"] for b in cat["buildings"]]
    bid = building_id or ids[0]
    if bid not in ids:
        raise HTTPException(400, f"unknown building_id {bid!r}; expected one of {ids}")
    b = next(x for x in cat["buildings"] if x["building_id"] == bid)
    if level not in ("building", "floor", "zone"):
        raise HTTPException(400, "level must be building, floor or zone")
    if level == "floor" and floor_id not in b["floor_ids"]:
        raise HTTPException(400, f"floor_id must be one of {b['floor_ids']}")
    zone_ids = [z["zone_id"] for z in b["zone_list"]]
    if level == "zone" and zone_id not in zone_ids:
        raise HTTPException(400, f"zone_id must be one of {zone_ids}")
    end_all = cat["t_end"] + 1
    try:
        t1 = ds_epoch(datetime.fromisoformat(end_time)) if end_time else end_all
        if start_time:
            t0 = ds_epoch(datetime.fromisoformat(start_time))
        else:
            if range_ not in dsa.RANGES:
                raise HTTPException(400, f"range must be one of {list(dsa.RANGES)}")
            t0 = t1 - dsa.RANGES[range_]
    except ValueError:
        raise HTTPException(400, "start_time / end_time must be ISO timestamps, e.g. 2026-06-01T08:00")
    t0, t1 = max(t0, cat["t_start"]), min(t1, end_all)
    if t1 <= t0:
        raise HTTPException(400, "empty time window (outside the dataset span?)")
    if interval == "auto":
        iv = dsa.auto_interval(t1 - t0)
    elif interval in dsa.INTERVALS:
        iv = dsa.INTERVALS[interval]
    else:
        raise HTTPException(400, f"interval must be auto or one of {list(dsa.INTERVALS)}")
    adjusted = False
    while (t1 - t0) / iv > dsa.MAX_POINTS:          # never ship thousands of points
        iv, adjusted = iv * 2, True
    return {"building_id": bid, "building": {k: b[k] for k in ("building_id", "building_type", "building_name",
                                                                "floors", "zones", "pv_kwp")},
            "level": level, "floor_id": floor_id if level == "floor" else None,
            "zone_id": zone_id if level == "zone" else None, "t0": t0, "t1": t1,
            "interval_s": iv, "interval_adjusted": adjusted}


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes")


@app.get("/api/history/catalog")
def history_catalog():
    """What the generated dataset contains: buildings -> floors -> zones, time span,
    row counts, generation config (public part), ranges, intervals, comparison pairs.
    OUTPUT: {"available": false, "hint"} when no dataset exists — never a 500."""
    if not HISTORY.available():
        return {"available": False, "hint": "run `python -m scripts.generate_dataset`"}
    cat = HISTORY.catalog()
    return {"available": True, "data_state": HISTORY_STATE, **cat,
            "start": _iso(cat["t_start"]), "end": _iso(cat["t_end"]),
            "ranges": list(dsa.RANGES), "intervals": ["auto"] + list(dsa.INTERVALS),
            "pairs": list(dsa.PAIRS) + ["current_vs_historical_demand"],
            "metrics": sorted(dsa.VALID_METRICS), "groups": ds_schema.GROUPS,
            "states": ds_schema.DATA_STATE}


@app.get("/api/history")
def history(building_id: str | None = None, level: str = "building", floor_id: str | None = None,
            zone_id: str | None = None, range: str = "24h", start_time: str | None = None,
            end_time: str | None = None, interval: str = "auto", metrics: str | None = None):
    """Historical series for a building / floor / zone: zone-derived aggregates (SUM
    additive, AVG intensive — no double counting), site weather, and at building level
    PV / grid / forecast / peak fields. Downsampled to <= 1000 buckets.

    INPUT: building_id, level=building|floor|zone (+floor_id|zone_id), range=1h|6h|24h|7d|30d
      (relative to the dataset END) or start_time/end_time ISO, interval=auto|5min|15min|1h|3h|1d,
      metrics=comma list (default all).
    ERROR STATES: 400 bad filters; 503 when no dataset exists.
    """
    store = _history()
    w = _hist_window(store, building_id, range, start_time, end_time, interval, level, floor_id, zone_id)
    mets = None
    if metrics:
        mets = [m.strip() for m in metrics.split(",") if m.strip()]
        bad = [m for m in mets if m not in dsa.VALID_METRICS]
        if bad:
            raise HTTPException(400, f"unknown metrics {bad}")
    pts = dsa.joined(store, w["building_id"], w["t0"], w["t1"], w["interval_s"], w["level"],
                     w["floor_id"], w["zone_id"], mets)
    for p in pts:
        p["ts"] = _iso(p["t"])
    return {**w, "start": _iso(w["t0"]), "end": _iso(w["t1"]), "data_state": HISTORY_STATE,
            "states": ds_schema.DATA_STATE, "points": pts}


@app.get("/api/history/compare")
def history_compare(pair: str, building_id: str | None = None, level: str = "building",
                    floor_id: str | None = None, zone_id: str | None = None, range: str = "7d",
                    start_time: str | None = None, end_time: str | None = None, interval: str = "auto"):
    """A named relationship from the generated values with its Pearson r, or the
    current-day vs historical hourly demand profile (pair=current_vs_historical_demand)."""
    store = _history()
    w = _hist_window(store, building_id, range, start_time, end_time, interval, level, floor_id, zone_id)
    if pair == "current_vs_historical_demand":
        return {**w, "pair": pair, "data_state": HISTORY_STATE,
                **dsa.demand_profile(store, w["building_id"], w["t0"], w["t1"])}
    if pair not in dsa.PAIRS:
        raise HTTPException(400, f"pair must be one of {list(dsa.PAIRS) + ['current_vs_historical_demand']}")
    rows = dsa.joined(store, w["building_id"], w["t0"], w["t1"], w["interval_s"], w["level"],
                      w["floor_id"], w["zone_id"])
    return {**w, "data_state": HISTORY_STATE, **dsa.compare(rows, pair)}


@app.get("/api/history/anomalies")
def history_anomalies(building_id: str | None = None, start_time: str | None = None,
                      end_time: str | None = None):
    """Anomaly metadata injected into the generated dataset (id, type, severity, window, zone)."""
    store = _history()
    return {"data_state": HISTORY_STATE,
            "anomalies": store.anomalies(building_id, start_time, end_time)}


@app.get("/api/history/quality")
def history_quality(zone_id: str, building_id: str | None = None, range: str = "24h",
                    start_time: str | None = None, end_time: str | None = None):
    """RAW vs CLEAN sensor layers for one zone, plus quality-flag counts."""
    store = _history()
    w = _hist_window(store, building_id, range, start_time, end_time, "auto", "zone", None, zone_id)
    rows = store.quality_series(w["building_id"], zone_id, w["t0"], w["t1"])
    flags: dict = {}
    for r in rows:
        for k in ("temperature_quality", "co2_quality"):
            flags[f"{k}:{r[k]}"] = flags.get(f"{k}:{r[k]}", 0) + 1
    stride = max(1, -(-len(rows) // dsa.MAX_POINTS))
    return {**w, "start": _iso(w["t0"]), "end": _iso(w["t1"]),
            "data_state": {"raw": "simulated sensor readings (noise/drift/missing/outliers injected)",
                                "clean": "derived by validation + cleaning; raw is never overwritten"},
            "flag_counts": flags, "stride": stride, "points": rows[::stride]}


@app.get("/api/history/export")
def history_export(format: str = "csv", layer: str = "zone", building_id: str | None = None,
                   zone_id: str | None = None, range: str = "24h", start_time: str | None = None,
                   end_time: str | None = None):
    """Download generated rows (layer=zone|building|raw|clean) as CSV or JSON.
    Capped at 50 000 rows (truncated=true says so). Only dataset rows are exported —
    no server paths or internal configuration."""
    store = _history()
    if format not in ("csv", "json"):
        raise HTTPException(400, "format must be csv or json")
    if layer not in ("zone", "building", "raw", "clean"):
        raise HTTPException(400, "layer must be zone, building, raw or clean")
    lvl = "zone" if zone_id else "building"
    w = _hist_window(store, building_id, range, start_time, end_time, "auto", lvl, None, zone_id)
    rows, truncated = store.export_rows(layer, w["building_id"], w["t0"], w["t1"], zone_id)
    name = f"feelslike_{w['building_id']}_{layer}_{_iso(w['t0'])[:10]}"
    if format == "json":
        return {"data_state": HISTORY_STATE, "truncated": truncated, "count": len(rows), "rows": rows}
    return Response(to_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"',
                             "X-Truncated": str(truncated).lower(), "X-Data-State": "simulated-history"})


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
def export(request: Request, author: str | None = None, scrub: bool = False):
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
    SEC.audit.record("configuration_change", request.state.principal.username, "privacy export",
                     target="occupant records", result="accepted", scope="one author" if author else "all")
    with sim.lock:
        return privacy.export_records([dict(e) for e in sim.feed], sim.store,
                                      author=author, scrub=scrub, now_t=sim.us.t)


@app.post("/api/redact")
def redact(body: RedactIn, request: Request):
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
        SEC.audit.record("configuration_change", request.state.principal.username, "privacy redact",
                         target=entry_id, result="accepted")
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

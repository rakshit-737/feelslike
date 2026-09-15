"""Hardware-in-the-loop bridge: one physical "shoebox" zone over plain HTTP.

WHAT THIS IS. An ESP32 node (temp/RH sensor, a 5V fan as the ventilation
actuator, a small heater as a calibration step-input) POSTs a reading every
couple of seconds to POST /api/hw/reading and receives its actuator commands in
the response body. This module owns that exchange: the latest-reading store,
the command buffer, the safety envelope, and the two REAL adapter
implementations (HttpSensorAdapter, HttpHVACAdapter) that pass the exact same
assert_conforms() gate as the Sim adapters in backend.adapters.

DESIGN DECISIONS (STATUS.md §8, decisions 8-9):
- HTTP POST, not MQTT: zero new runtime dependencies, no broker process to die
  on stage, works over a phone hotspot. The node polls; commands ride back on
  the poll response, so there is exactly one moving part.
- The rig mirrors ONE twin zone (HW_ZONE). The controller's vent command for
  that zone is forwarded to the physical fan through HttpHVACAdapter — same
  controller, same seam, no special case in the control law. The twin's
  physics for that zone keeps running untouched: overriding twin state with
  measured air would make the A/B race asymmetric and quietly falsify the
  frozen headline numbers. Real sensing is displayed, health-checked and
  logged for calibration instead.
- The heater is NOT an HVAC actuator. It is the step-input for sim-to-real
  calibration (fit R and C of the twin's RC model to a logged step response)
  and is driven manually via POST /api/hw/heater, never by the controller.

SAFETY MODEL (three layers; only layer 2 lives in this file):
  layer 0  physical toggle switch in the actuator supply line (hardware/README)
  layer 1  firmware watchdog: no good server response for watchdog_s -> all off
  layer 2  HERE: fan clamped to VENT_LEVELS; heater duty-cycled (at most
           HEATER_MAX_DUTY of any HEATER_WINDOW_S window) and forced off when
           the cap is hit; every command change is appended to an audit log.

TIME. Hardware lives on the WALL clock (time.time()), not sim time — a real
sensor does not speed up at 960x. Nothing here feeds the physics, so sim
determinism (same seed -> identical kWh) is untouched; the whole module is
inert when no node has ever POSTed.

Stdlib only. No new runtime dependencies (preservation guarantee).
"""
from __future__ import annotations

import threading
import time
from collections import deque

from backend.adapters import VENT_LEVELS

HW_ZONE = "zone_b"            # the twin zone the rig mirrors (Conference Room B)

POLL_S = 2.0                  # node posts a reading this often
WATCHDOG_S = 10.0             # node kills actuators after this long without a reply
STALE_S = 15.0                # server treats the node as gone after this long
READING_LOG_MAX = 8192        # ~4.5 h at POLL_S — enough for a calibration run
WRITES_MAX = 500              # audit ring, same size as SimHVACAdapter's

HEATER_WINDOW_S = 600.0       # duty-cycle accounting window
HEATER_MAX_DUTY = 0.5         # heater may be on at most this fraction of the window

TEMP_RANGE_C = (-20.0, 70.0)  # same plausibility band SimSensorAdapter.health uses
RH_RANGE = (0.0, 100.0)

FAN_LEVEL_W = {0: 0.0, 1: 1.5, 2: 2.5}   # nominal 40 mm 5 V fan draw, for display

SENSOR_NODE_LOG_MAX = 8192    # per sensor-only node; same horizon as the rig log
SENSOR_NODE_MAX = 8           # distinct sensor-only nodes kept; least recently seen evicted
STUCK_WINDOW = 8              # this many bit-identical readings in a row -> "stuck"


class HardwareBridge:
    """Latest reading + command buffer + safety envelope for one physical node.

    INPUT: constructed once at import (backend.app owns the instance). All
      methods take an optional now (wall seconds) so tests can drive the clock.
    OUTPUT: post_reading() returns the command dict the node applies.
    SIDE EFFECTS: keeps a bounded reading log and a bounded write audit log.
    ERROR STATES: post_reading raises ValueError on an invalid payload (the
      endpoint maps it to 422); everything else degrades, never raises.

    Thread safety: one internal lock. Endpoints run in Starlette's threadpool
    and the sim loop calls set_fan() each step — both cross this lock only for
    microseconds; it is never held around I/O and is disjoint from sim.lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.node_id: str | None = None
        self.last: dict | None = None          # last accepted reading
        self.readings: deque = deque(maxlen=READING_LOG_MAX)
        self.fan: int = 0                      # commanded fan level 0|1|2
        self.heater_requested: bool = False    # what the operator asked for
        self.heater: bool = False              # what the envelope actually allows
        self.duty_limited: bool = False        # True while the cap is forcing off
        self.writes: list = []                 # audit ring (kind/value/ok/reason)
        self._heater_edges: deque = deque(maxlen=512)   # (t, on) transitions
        self._polls = 0

    # ------------------------------------------------------------ node I/O
    def post_reading(self, payload: dict, now: float | None = None) -> dict:
        """Accept one node reading; return the commands the node must apply.

        INPUT: payload {node_id: str, temp_c: float, rh_pct: float|None,
          seq: int, uptime_s: float} — extra keys are ignored so firmware can
          grow without a lockstep server deploy. now = wall seconds (tests).
        OUTPUT: {ok, fan, heater, watchdog_s, poll_s} — the node-side contract;
          field names are FROZEN (the firmware parses them by name).
        SIDE EFFECTS: stores the reading (bounded log), re-evaluates the heater
          duty envelope, counts the poll.
        ERROR STATES: ValueError naming the field for a missing/invalid value.
        """
        now = time.time() if now is None else float(now)
        node = str(payload.get("node_id") or "").strip()
        if not node:
            raise ValueError("node_id must be a non-empty string")
        try:
            temp = float(payload["temp_c"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("temp_c must be a number") from None
        if not (TEMP_RANGE_C[0] <= temp <= TEMP_RANGE_C[1]):
            raise ValueError(f"temp_c {temp} outside plausible {TEMP_RANGE_C}")
        rh = payload.get("rh_pct")
        if rh is not None:
            try:
                rh = float(rh)
            except (TypeError, ValueError):
                raise ValueError("rh_pct must be a number or null") from None
            if not (RH_RANGE[0] <= rh <= RH_RANGE[1]):
                raise ValueError(f"rh_pct {rh} outside {RH_RANGE}")
        try:
            seq = int(payload.get("seq", 0))
        except (TypeError, ValueError):
            raise ValueError("seq must be an integer") from None
        reading = {"node_id": node, "temp_c": temp, "rh_pct": rh, "seq": seq,
                   "uptime_s": float(payload.get("uptime_s", 0.0) or 0.0),
                   "t_wall": now,
                   # actuator state AT reading time — the calibration fit needs
                   # the heater timeline aligned with the temperature series
                   "fan": int(self.fan), "heater": bool(self.heater)}
        with self._lock:
            self.node_id = node
            self.last = reading
            self.readings.append(reading)
            self._polls += 1
            self._enforce_heater_cap(now)
            return {"ok": True, "fan": int(self.fan), "heater": bool(self.heater),
                    "watchdog_s": WATCHDOG_S, "poll_s": POLL_S}

    # ------------------------------------------------------------ commands
    def set_fan(self, level: int, now: float | None = None) -> bool:
        """Command the fan. The ONLY caller on the control path is
        HttpHVACAdapter.write_vent — the seam stays the seam.

        INPUT: level, must be in VENT_LEVELS. OUTPUT: True if accepted.
        SIDE EFFECTS: audit log on CHANGE only (the sim loop repeats the same
        command every step; logging repeats would drown the ring).
        ERROR STATES: none — rejection is False.
        """
        now = time.time() if now is None else float(now)
        try:
            v = int(level)
        except (TypeError, ValueError):
            return self._log("fan", level, False, "not an integer", now)
        if v not in VENT_LEVELS:
            return self._log("fan", v, False, f"not in {VENT_LEVELS}", now)
        with self._lock:
            if v != self.fan:
                self.fan = v
                self._log_locked("fan", v, True, "", now)
        return True

    def set_heater(self, on: bool, now: float | None = None) -> dict:
        """Operator request to turn the calibration heater on/off.

        INPUT: on (bool). OUTPUT: {requested, heater, duty_limited, duty} —
          heater is what the envelope actually allows right now.
        SIDE EFFECTS: records the request, re-runs the duty envelope, audits.
        ERROR STATES: none.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            self.heater_requested = bool(on)
            self._enforce_heater_cap(now)
            self._log_locked("heater", bool(on), self.heater == bool(on),
                             "duty cap" if self.duty_limited else "", now)
            return {"requested": self.heater_requested, "heater": self.heater,
                    "duty_limited": self.duty_limited,
                    "duty": round(self._duty(now), 3)}

    # ------------------------------------------------------------- queries
    def has_node(self) -> bool:
        """True once any node has ever posted. Lock-held read so callers on the
        sim loop keep clean lock discipline (sim.lock -> bridge lock is the one
        permitted order; no bridge path ever takes sim.lock)."""
        with self._lock:
            return self.last is not None

    def stale_s(self, now: float | None = None) -> float | None:
        """Seconds since the last reading; None when no node has ever posted."""
        now = time.time() if now is None else float(now)
        with self._lock:
            return None if self.last is None else max(0.0, now - self.last["t_wall"])

    def connected(self, now: float | None = None) -> bool:
        s = self.stale_s(now)
        return s is not None and s <= STALE_S

    def log_rows(self, limit: int = READING_LOG_MAX) -> list:
        """Newest-last copy of the reading log (calibration consumes this)."""
        with self._lock:
            rows = list(self.readings)
        return rows[-int(limit):]

    def status(self, now: float | None = None) -> dict:
        """The /api/hw/status payload and the /api/state -> hardware block.

        INPUT: now (wall seconds, tests only).
        OUTPUT: json-safe dict; connected=False with every other field null/zero
          when no node has ever posted — an empty-but-valid shape, never None.
        SIDE EFFECTS: none. ERROR STATES: none.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            last = dict(self.last) if self.last else None
            stale = None if last is None else max(0.0, now - last["t_wall"])
            return {
                "zone": HW_ZONE,
                "connected": bool(stale is not None and stale <= STALE_S),
                "node_id": self.node_id,
                "reading": last,
                "stale_s": None if stale is None else round(stale, 1),
                "stale_after_s": STALE_S,
                "fan": int(self.fan),
                "fan_w": FAN_LEVEL_W.get(int(self.fan), 0.0),
                "heater": bool(self.heater),
                "heater_requested": bool(self.heater_requested),
                "duty": round(self._duty(now), 3),
                "duty_limited": bool(self.duty_limited),
                "duty_cap": HEATER_MAX_DUTY,
                "duty_window_s": HEATER_WINDOW_S,
                "polls": self._polls,
                "log_len": len(self.readings),
                "watchdog_s": WATCHDOG_S,
                "poll_s": POLL_S,
                "protocol": "http-esp32",
            }

    # ------------------------------------------------------------ internals
    def _duty(self, now: float) -> float:
        """Fraction of the trailing HEATER_WINDOW_S the heater was actually on.
        CALLER HOLDS THE LOCK (or is the lock holder via a public method)."""
        cut = now - HEATER_WINDOW_S
        on_s, t, state = 0.0, cut, False
        for et, eon in self._heater_edges:
            if et <= cut:
                state = eon
                continue
            if state:
                on_s += et - t
            t, state = et, eon
        if state:
            on_s += now - t
        return min(1.0, on_s / HEATER_WINDOW_S)

    def _enforce_heater_cap(self, now: float) -> None:
        """Drive self.heater from the request + the duty envelope.
        CALLER HOLDS THE LOCK.

        Edges older than the window are pruned to a single boundary edge that
        carries the state at the window's left edge, so the deque is bounded by
        the transitions INSIDE the window and the maxlen backstop can never
        silently drop an edge that still matters to the duty accounting."""
        cut = now - HEATER_WINDOW_S
        state_at_cut, pruned = False, False
        while self._heater_edges and self._heater_edges[0][0] <= cut:
            _, state_at_cut = self._heater_edges.popleft()
            pruned = True
        if pruned:
            self._heater_edges.appendleft((cut, state_at_cut))
        duty = self._duty(now)
        allow = self.heater_requested and duty < HEATER_MAX_DUTY
        self.duty_limited = self.heater_requested and not allow
        if allow != self.heater:
            self.heater = allow
            self._heater_edges.append((now, allow))

    def _log(self, kind: str, value, ok: bool, reason: str, now: float) -> bool:
        with self._lock:
            self._log_locked(kind, value, ok, reason, now)
        return ok

    def _log_locked(self, kind: str, value, ok: bool, reason: str, now: float) -> None:
        self.writes.append({"t_wall": round(now, 3), "kind": kind, "value": value,
                            "ok": ok, "reason": reason})
        if len(self.writes) > WRITES_MAX:
            del self.writes[:-WRITES_MAX]


class SensorNodeStore:
    """Sensor-only nodes: latest reading and a bounded log per node_id.

    WHY THIS IS NOT HardwareBridge. The bridge is the ONE actuator node: its
    reply carries fan/heater commands and it owns the safety envelope. A second
    node POSTing to /api/hw/reading would overwrite the rig's latest reading and
    be handed commands it cannot apply. Sensor-only nodes (the Uno ambient
    reference over USB serial, and any future wired-bus sensor) land here, and
    nothing they send can command anything: the reply is an acknowledgement.

    INPUT: post(payload) per reading. A node reports EITHER temp_c OR a fault
      string (the Uno sends fault="sensor_open_or_short" when its ADC sits at a
      rail). A fault is recorded, never converted into a number.
    OUTPUT: status() and log_rows(node_id), json-safe, empty-but-valid shapes.
    SIDE EFFECTS: bounded per-node logs; at most SENSOR_NODE_MAX nodes, least
      recently seen evicted first, so a typo'd node_id cannot grow memory.
    ERROR STATES: post raises ValueError naming the field (endpoint -> 422);
      log_rows raises KeyError for an unknown node (endpoint -> 404).

    Wall clock, like the rig. Nothing here feeds the physics or the controller.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._nodes: dict = {}

    def post(self, payload: dict, now: float | None = None) -> dict:
        """Accept one sensor-only reading. See the class docstring."""
        now = time.time() if now is None else float(now)
        node = str(payload.get("node_id") or "").strip()
        if not node:
            raise ValueError("node_id must be a non-empty string")
        if len(node) > 64:
            raise ValueError("node_id must be at most 64 characters")
        fault = payload.get("fault")
        temp = None
        if fault is not None:
            fault = str(fault).strip()[:64] or "unspecified"
        else:
            try:
                temp = float(payload["temp_c"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("temp_c must be a number (or send a fault string)") from None
            if not (TEMP_RANGE_C[0] <= temp <= TEMP_RANGE_C[1]):
                raise ValueError(f"temp_c {temp} outside plausible {TEMP_RANGE_C}")
        counts = payload.get("counts")
        if counts is not None:
            try:
                counts = float(counts)
            except (TypeError, ValueError):
                raise ValueError("counts must be a number or null") from None
        try:
            seq = int(payload.get("seq", 0) or 0)
            uptime = float(payload.get("uptime_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            raise ValueError("seq must be an integer and uptime_s a number") from None
        reading = {"node_id": node,
                   "role": str(payload.get("role") or "ambient")[:32],
                   "transport": str(payload.get("transport") or "unknown")[:32],
                   "temp_c": temp, "fault": fault, "counts": counts,
                   "seq": seq, "uptime_s": uptime, "t_wall": now,
                   # the node's own claim; only a real boolean true counts
                   "calibrated": payload.get("calibrated") is True}
        with self._lock:
            n = self._nodes.get(node)
            if n is None:
                if len(self._nodes) >= SENSOR_NODE_MAX:
                    oldest = min(self._nodes, key=lambda k: self._nodes[k]["last"]["t_wall"])
                    del self._nodes[oldest]
                n = self._nodes[node] = {"log": deque(maxlen=SENSOR_NODE_LOG_MAX),
                                         "polls": 0, "faults": 0, "last": None}
            n["last"] = reading
            n["log"].append(reading)
            n["polls"] += 1
            n["faults"] += 1 if fault is not None else 0
        return {"ok": True, "node_id": node, "accepted": fault is None}

    def log_rows(self, node_id: str, limit: int = SENSOR_NODE_LOG_MAX) -> list:
        """One node's readings, oldest first. KeyError for an unknown node."""
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None:
                raise KeyError(node_id)
            rows = list(n["log"])
        return rows[-int(limit):]

    def status(self, now: float | None = None) -> dict:
        """Every node with staleness and INFERRED health, plus the ambient
        reference: the first connected role=ambient node whose latest reading
        is a real temperature, else None. Health faults: stale, sensor_fault
        (latest report was a fault), stuck (STUCK_WINDOW identical readings)."""
        now = time.time() if now is None else float(now)
        with self._lock:
            snap = {k: {"last": dict(v["last"]), "polls": v["polls"], "faults": v["faults"],
                        "recent": [r["temp_c"] for r in list(v["log"])[-STUCK_WINDOW:]]}
                    for k, v in self._nodes.items()}
        nodes = []
        for node_id in sorted(snap):
            v = snap[node_id]
            last = v["last"]
            stale = max(0.0, now - last["t_wall"])
            connected = stale <= STALE_S
            faults: list = []
            if not connected:
                faults.append("stale")
            if last["fault"] is not None:
                faults.append("sensor_fault")
            recent = v["recent"]
            if len(recent) >= STUCK_WINDOW and None not in recent and len(set(recent)) == 1:
                faults.append("stuck")
            nodes.append({"node_id": node_id, "role": last["role"],
                          "transport": last["transport"], "connected": connected,
                          "stale_s": round(stale, 1), "reading": last,
                          "polls": v["polls"], "faults_reported": v["faults"],
                          "health": {"ok": not faults, "faults": faults}})
        amb = next((n for n in nodes if n["role"] == "ambient" and n["connected"]
                    and n["reading"]["temp_c"] is not None), None)
        return {"nodes": nodes,
                "ambient": None if amb is None else {
                    "node_id": amb["node_id"], "temp_c": amb["reading"]["temp_c"],
                    "stale_s": amb["stale_s"], "transport": amb["transport"],
                    "calibrated": amb["reading"]["calibrated"],
                    "health": amb["health"]},
                "stale_after_s": STALE_S}


# ==========================================================================
# The real adapters — same protocols, same conformance gate as the Sim ones
# ==========================================================================

class HttpSensorAdapter:
    """SensorAdapter over the HTTP bridge. Serves exactly one zone (the rig).

    A building's real sensors arrive bus by bus; an adapter that pretends to
    read five zones from one ESP32 would be architecture theatre. Zones other
    than the bound one raise KeyError, exactly like a point that is not on the
    bus (adapter convention: reads are loud).
    """

    def __init__(self, bridge: HardwareBridge, zone: str = HW_ZONE,
                 history: int = 16):
        self.bridge = bridge
        self.zone = zone
        self.history = int(history)

    def read(self, zone: str) -> dict:
        """See SensorAdapter.read. A stale or absent node reads as a dropout
        (None values) rather than a lie — health() then reports it."""
        if zone != self.zone:
            raise KeyError(zone)
        now = time.time()
        st = self.bridge.status(now)
        fresh = st["connected"] and st["reading"] is not None
        r = st["reading"] or {}
        return {
            "zone": zone,
            "temp_c": r.get("temp_c") if fresh else None,
            "rh_pct": r.get("rh_pct") if fresh else None,
            "co2_ppm": None,                      # no CO2 point on this node
            "co2_estimated": False,
            "t_wall": r.get("t_wall"),
            "age_s": st["stale_s"],
            "seq": r.get("seq"),
            "source": "esp32-http",
        }

    def health(self, zone: str) -> dict:
        """See SensorAdapter.health. Same inference rules as SimSensorAdapter:
        no_data / dropout(stale) / out_of_range / stuck / impossible — inferred
        from the reading log, never echoed from a flag."""
        if zone != self.zone:
            raise KeyError(zone)
        now = time.time()
        rows = self.bridge.log_rows(self.history)
        if not rows:
            return {"zone": zone, "ok": False, "faults": ["no_data"], "reads": 0,
                    "last_read_t": None, "stale_s": None}
        last = rows[-1]
        stale = max(0.0, now - last["t_wall"])
        faults: list = []
        if stale > STALE_S:
            faults.append("stale")
        t_c, rh = last["temp_c"], last["rh_pct"]
        if t_c is not None and not (TEMP_RANGE_C[0] <= t_c <= TEMP_RANGE_C[1]):
            faults.append("out_of_range")
        if rh is not None and not (RH_RANGE[0] <= rh <= RH_RANGE[1]):
            faults.append("out_of_range")
        temps = [r["temp_c"] for r in rows if r["temp_c"] is not None]
        if len(temps) >= max(4, self.history // 2) and len(set(temps)) == 1:
            faults.append("stuck")
        return {"zone": zone, "ok": not faults, "faults": faults,
                "reads": len(rows), "last_read_t": last["t_wall"],
                "stale_s": round(stale, 1)}


class HttpHVACAdapter:
    """HVACAdapter over the HTTP bridge. Vent-only, and says so.

    The rig has a fan (real ventilation actuation) and no cooling plant, so
    write_vent works and write_setpoint is REJECTED honestly (False + audited
    reason) — per the adapter convention, writes never raise, and per this
    repo's rules, nothing pretends. capabilities() carries the same honesty in
    machine-readable form so a caller can discover it without a failed write.
    """

    def __init__(self, bridge: HardwareBridge, zone: str = HW_ZONE):
        self.bridge = bridge
        self.zone = zone

    @property
    def writes(self) -> list:
        return self.bridge.writes          # one audit trail for the whole rig

    def read_state(self, zone: str) -> dict:
        """See HVACAdapter.read_state. temp_c is the node's measured air."""
        if zone != self.zone:
            raise KeyError(zone)
        st = self.bridge.status()
        r = st["reading"] or {}
        return {
            "zone": zone,
            "temp_c": r.get("temp_c"),
            "setpoint_c": None,                  # no cooling plant to command
            "applied_setpoint_c": None,
            "vent": int(st["fan"]),
            "applied_vent": int(st["fan"]) if st["connected"] else 0,
            "rh_pct": r.get("rh_pct"),
            "at_capacity": False,
            "capacity_w": 0.0,                   # honest: zero cooling capacity
            "fan_w": st["fan_w"],
            "connected": st["connected"],
            "mode": "vent-only",
        }

    def write_setpoint(self, zone: str, celsius: float | None) -> bool:
        """See HVACAdapter.write_setpoint. Always rejected: the rig has no
        cooling plant. The rejection is audited so the seam shows its shape."""
        now = time.time()
        if zone != self.zone:
            return self.bridge._log("setpoint", celsius, False, "unknown zone", now)
        return self.bridge._log("setpoint", celsius, False,
                                "rig is vent-only (no cooling plant)", now)

    def write_vent(self, zone: str, level: int) -> bool:
        """See HVACAdapter.write_vent. This IS the physical fan."""
        if zone != self.zone:
            return self.bridge._log("fan", level, False, "unknown zone", time.time())
        return self.bridge.set_fan(level)

    def capabilities(self) -> dict:
        """See HVACAdapter.capabilities. protocol/writable tell the truth."""
        return {
            "protocol": "http-esp32",
            "vendor": "feelslike shoebox node",
            "writable": True,                    # vent writes are real
            "supports_setpoint": False,          # and this one is not
            "supports_heating": False,           # heater is calibration-only
            "zones": [self.zone],
            "setpoint_range_c": [],              # nothing commandable
            "vent_levels": list(VENT_LEVELS),
            "per_zone": {self.zone: {"name": "shoebox rig",
                                     "fan_levels_w": dict(FAN_LEVEL_W),
                                     "max_cool_w": 0.0}},
        }

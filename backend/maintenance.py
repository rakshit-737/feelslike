"""Maintenance intelligence: infer equipment faults from how the building
behaves over TIME, not from any single bad reading.

Why this exists: a complaint tells you a room is uncomfortable. It does not tell
you that the AHU serving that room is undersized, that a zone sensor is lying by
five kelvin, that a damper is commanded open and does nothing, or that the same
room has been complaining every afternoon all week. Those are only visible as
patterns, and they are the difference between "nudge the setpoint" and "send a
technician".

Four detectors, each with a sustained-evidence window:
  capacity  - occupied, coil pinned at max, zone still above the comfort band.
  sensor    - zone reads persistently far off the median of its ADJACENCY
              neighbours while its own cooling behaviour looks normal.
  actuator  - airflow commanded up, and nothing measurable responded.
  recurring - the same (zone, issue) complained about on 3+ distinct sim-days,
              or 4+ times inside 24 sim-hours.

Rules that keep this credible instead of noisy:
- Nothing fires on an instant. One hot minute is weather, not a fault.
- Alerts are STATEFUL: one underlying problem is one alert whose evidence grows
  and whose confidence rises. It never spawns duplicates.
- Alerts auto-resolve once the symptom has been absent for COOLDOWN_MIN, and
  move to history() rather than vanishing.
- Confidence is computed from duration + repetition and always reported. No
  detector is ever allowed to claim 1.0.
- O(zones) work per tick: rolling scalars, short bounded deques, and an
  incremental cursor over ConstraintStore.items. Nothing re-scans history.

THREE HONEST CAVEATS
1. The actuator detector cannot, without running a counterfactual clone every
   tick, separate "the fan is dead" from "the load is larger than the fan could
   ever fix". The moisture corroboration below is the closest cheap
   discriminator, so it RAISES confidence rather than gating the alert, and
   actuator confidence is capped lower than the other detectors on purpose.
2. The actuator detector measures moisture response as DEW POINT, not RH.
   RH conflates moisture with temperature - a room that heats up at constant
   moisture shows falling RH, which would read as "the airflow helped" when
   nothing was removed. Dew point is a monotone function of the humidity ratio
   W alone (Magnus inverse), so it is the honest variable for "did this air
   actually exchange with anywhere".
3. The sensor detector deliberately refuses to judge a free-floating zone. A
   building with the HVAC off separates by 5-8 K across zones purely on
   orientation and thermal mass, and flagging that as a miscalibrated sensor is
   the classic FDD false alarm; both measured empirically while building this
   file. It therefore requires the zone AND its neighbours to be under a
   comparable commanded setpoint, which means a sensor drifting overnight is
   only caught the next morning once control resumes. It also reports only the
   single strongest outlier per neighbourhood (see the shadowing rule), because
   a median test genuinely cannot tell "I read high" from "my neighbour reads
   low" - without that rule, one zone stuck +7 K raised a second, mirror-image
   alert against the innocent neighbour it had dragged.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from backend.contracts import MaintenanceAlert, id_counter, new_id, to_dict
from sim.humidity import dew_point, outdoor_rh
from sim.twin import ADJACENCY, BAND, RH_HUMID, ZONE_BY_ID, ZONE_IDS

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# --- detector tunables (sim-minutes, kelvin, percentage points) -------------
CAPACITY_WINDOW_MIN = 45.0    # sustained minutes before a capacity alert
SENSOR_WINDOW_MIN = 45.0      # sustained minutes before a sensor alert
SENSOR_DEV_K = 3.0            # |zone - neighbour median| that counts as "off"
ACTUATOR_WINDOW_MIN = 30.0    # minutes of elevated airflow before we judge it
GRACE_MIN = 5.0              # symptom may blink off this long without resetting
COOLDOWN_MIN = 30.0           # symptom absent this long -> alert resolves
EVIDENCE_INTERVAL_MIN = 30.0  # do not append a new evidence line faster than this
MAX_EVIDENCE = 6              # keep the first line + the newest few

TEMP_IMPROVE_K = 0.2          # temp fall that counts as "the airflow worked"
DEW_IMPROVE_K = 0.3           # dew-point fall that counts as moisture removal
FLAT_DEW_K = 0.15             # |dew change| below this is "air is not moving"
DEW_GRADIENT_K = 1.5          # ...and only meaningful with this much gradient
SETPOINT_TOL_K = 2.0          # zone vs neighbour setpoint spread still "same"

RECUR_DAYS = 3                # distinct sim-days of the same (zone, issue)
RECUR_BURST_N = 4             # ...or this many complaints...
RECUR_BURST_S = 24 * 3600.0   # ...inside this window
RECUR_QUIET_S = 24 * 3600.0   # no new complaint this long -> recurrence resolves
MEMORY_AUTHOR = "comfort-memory"   # backend.memory pre-applies are NOT evidence

# Undirected neighbour map from the real inter-zone conductance graph.
NEIGHBOURS: dict = {z: [] for z in ZONE_IDS}
for _a, _b in ADJACENCY:
    NEIGHBOURS[_a].append(_b)
    NEIGHBOURS[_b].append(_a)


def _median(vals: list) -> float:
    """Median of a non-empty list of floats (mean of the middle two if even)."""
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _clock(t: float) -> str:
    """Sim seconds -> "Wed 14:35", same format as /api/state sim_clock."""
    d, h = int(t // 86400.0), (t % 86400.0) / 3600.0
    return f"{DAYS[d % 7]} {int(h):02d}:{int((h % 1) * 60):02d}"


def _name(zone: str) -> str:
    z = ZONE_BY_ID.get(zone)
    return z.name if z else zone


def _commanded(decisions) -> dict:
    """zone -> {vent, sp} from a list of ControllerDecision objects or dicts.

    INPUT: None, or an iterable of ControllerDecision dataclasses / plain dicts.
    OUTPUT: dict keyed by zone_id; only decisions with applied is not False are
      kept, because a recommend-only decision never reached the equipment.
    SIDE EFFECTS: none.  ERROR STATES: none - malformed entries are skipped.
    """
    out: dict = {}
    for d in decisions or []:
        if isinstance(d, dict):
            zone, vent, sp = d.get("zone"), d.get("new_vent"), d.get("new_setpoint")
            applied = d.get("applied", True)
        else:
            zone = getattr(d, "zone", None)
            vent = getattr(d, "new_vent", None)
            sp = getattr(d, "new_setpoint", None)
            applied = getattr(d, "applied", True)
        if zone and applied is not False:
            out[zone] = {"vent": vent, "sp": sp}
    return out


@dataclass
class _ZoneState:
    """Rolling per-zone detector state. All scalars: no growing history."""
    # capacity streak
    cap_start: float | None = None
    cap_last: float = 0.0
    cap_max: float = 0.0
    cap_sum: float = 0.0
    cap_n: int = 0
    # sensor streak
    sen_start: float | None = None
    sen_last: float = 0.0
    sen_sign: int = 0
    sen_max: float = 0.0
    sen_sum: float = 0.0
    sen_n: int = 0
    # actuator episode
    act_prev: int | None = None
    act_ep: dict | None = None


@dataclass
class _Tracked:
    """An alert plus the bookkeeping that keeps it single and self-resolving."""
    alert: MaintenanceAlert
    clear_since: float | None = None
    last_ev_t: float = -1e18
    seen: set = field(default_factory=set)   # evidence lines already recorded


class MaintenanceMonitor:
    """Evidence-based fault detection over the live twin and constraint store.

    INPUT: tick() is called once per simulation step with the twin, the
      ConstraintStore, and optionally the controller's decisions for this step.
    OUTPUT: tick() returns the alerts that were newly raised or MATERIALLY
      updated this tick (new evidence line or a severity change) as json-safe
      dicts; a steady ongoing fault therefore reports at most once per
      EVIDENCE_INTERVAL_MIN instead of every tick.
    SIDE EFFECTS: mutates only this monitor's own state. It never writes to the
      twin, the store, or the controller - alerts are advice, not control.
    ERROR STATES: none by design. A twin missing an optional attribute degrades
      via getattr defaults; a store without .items simply yields no recurrence.
    """

    def __init__(self, capacity_window_min: float = CAPACITY_WINDOW_MIN,
                 sensor_window_min: float = SENSOR_WINDOW_MIN,
                 sensor_dev_k: float = SENSOR_DEV_K,
                 actuator_window_min: float = ACTUATOR_WINDOW_MIN,
                 cooldown_min: float = COOLDOWN_MIN,
                 grace_min: float = GRACE_MIN):
        self.cap_win = float(capacity_window_min)
        self.sen_win = float(sensor_window_min)
        self.sen_dev = float(sensor_dev_k)
        self.act_win = float(actuator_window_min)
        self.cooldown_s = float(cooldown_min) * 60.0
        self.grace_s = float(grace_min) * 60.0
        self._ids = id_counter(1)
        self._zs: dict = {z: _ZoneState() for z in ZONE_IDS}
        self._alerts: dict = {}        # key tuple -> _Tracked
        self._history: list = []       # resolved alert dicts, newest last
        self._rec: dict = {}           # (zone, issue) -> rolling complaint stats
        self._cursor = 0               # how many store.items we have consumed
        self._last_t = 0.0

    # ---- public surface --------------------------------------------------
    def tick(self, twin, store, decisions=None) -> list[dict]:
        """Run all four detectors for one simulation step. See class docstring."""
        now = float(getattr(twin, "t", 0.0))
        if now < self._last_t:
            self.clear()               # twin rewound (clone/reset): start clean
        self._last_t = now

        snaps = {z: twin.zone_snapshot(z) for z in ZONE_IDS}
        t_out = float(twin.weather_fn(now)) + float(getattr(twin, "outdoor_offset", 0.0))
        rh_out = min(100.0, max(0.0, outdoor_rh(now, getattr(twin, "seed", 0))
                                + float(getattr(twin, "humidity_offset", 0.0))))
        dew_out = dew_point(t_out, rh_out)
        cmd = _commanded(decisions)

        # Pre-pass, needed before any sensor verdict. devs is the detection
        # signal the brief specifies (offset from the ADJACENCY neighbour
        # median). outlier is the RANKING signal (offset from the whole-building
        # median): a zone reading 5 K high drags its neighbours' local medians
        # with it, so the local number cannot say which of two adjacent zones is
        # the liar, but the building median - robust to one bad reading out of
        # five - can. See _sensor's shadowing rule.
        devs = {z: snaps[z]["temp_c"] - _median([snaps[n]["temp_c"]
                                                 for n in NEIGHBOURS[z]])
                for z in ZONE_IDS if NEIGHBOURS.get(z)}
        bmed = _median([snaps[z]["temp_c"] for z in ZONE_IDS])
        outlier = {z: snaps[z]["temp_c"] - bmed for z in ZONE_IDS}

        changed: dict = {}
        for z in ZONE_IDS:
            self._capacity(z, snaps[z], t_out, now, changed)
            self._sensor(z, snaps, devs, outlier, now, changed)
            self._actuator(z, snaps[z], cmd.get(z), dew_out, now, changed)
        self._recurring(store, now, changed)
        self._expire(now)
        return [to_dict(tr.alert) for k, tr in changed.items() if k in self._alerts]

    def alerts(self) -> list[dict]:
        """All currently active alerts, worst first.

        INPUT: none.  OUTPUT: list of json-safe dicts sorted by severity then
        confidence, both descending.  SIDE EFFECTS: none.  ERROR STATES: none.
        """
        rank = {"high": 3, "medium": 2, "low": 1}
        out = [to_dict(tr.alert) for tr in self._alerts.values()]
        out.sort(key=lambda a: (rank.get(a["severity"], 0), a["confidence"]), reverse=True)
        return out

    def history(self) -> list[dict]:
        """Alerts that resolved, oldest first, each with an extra resolved_t key.

        INPUT: none.  OUTPUT: list of json-safe dicts.  SIDE EFFECTS: none.
        ERROR STATES: none.
        """
        return list(self._history)

    def clear(self) -> None:
        """Forget everything: active alerts, history, streaks and the store cursor.

        INPUT: none.  OUTPUT: None.  SIDE EFFECTS: resets this monitor only.
        ERROR STATES: none.
        """
        self._zs = {z: _ZoneState() for z in ZONE_IDS}
        self._alerts.clear()
        self._history.clear()
        self._rec.clear()
        self._cursor = 0
        self._last_t = 0.0

    def suppress_setpoint_chasing(self, zone: str) -> bool:
        """True when a capacity alert is active for this zone.

        The controller layer should stop ratcheting the setpoint downwards while
        this is True: the equipment is already saturated, so every further
        degree of "demand" buys no cooling and only guarantees the zone can
        never satisfy its own thermostat. NOT WIRED HERE on purpose - the
        controller is another module's file; this is the read-only hook.

        INPUT: zone_id.  OUTPUT: bool.  SIDE EFFECTS: none.  ERROR STATES: none
        (an unknown zone is simply False).
        """
        return ("capacity", zone) in self._alerts

    # ---- detector 1: capacity -------------------------------------------
    def _capacity(self, z, s, t_out, now, changed) -> None:
        st = self._zs[z]
        key = ("capacity", z)
        cond = s["occ"] > 0 and bool(s["at_capacity"]) and s["temp_c"] > BAND[1]
        if cond:
            if st.cap_start is None:
                st.cap_start, st.cap_max, st.cap_sum, st.cap_n = now, s["temp_c"], 0.0, 0
            st.cap_last = now
            st.cap_max = max(st.cap_max, s["temp_c"])
            st.cap_sum += s["temp_c"]
            st.cap_n += 1
            self._unclear(key)
        elif st.cap_start is not None and now - st.cap_last > self.grace_s:
            st.cap_start, st.cap_n, st.cap_sum = None, 0, 0.0
        if st.cap_start is None:
            self._mark_clear(key, now)
            return
        dur = (now - st.cap_start) / 60.0
        if dur < self.cap_win or st.cap_n == 0:
            return
        mean = st.cap_sum / st.cap_n
        excess = mean - BAND[1]
        # Confidence: 0.50 once the window is met, +0.10 per further window of
        # persistence, +0.08 per kelvin of mean overshoot. Never above 0.95.
        conf = min(0.95, 0.50 + 0.10 * (dur / self.cap_win - 1.0) + 0.08 * min(excess, 3.0))
        sev = "high" if (dur >= 4 * self.cap_win or excess >= 2.0) else \
              ("medium" if dur >= 2 * self.cap_win else "low")
        ev = (f"{_name(z)} held {mean:.1f} degC (peak {st.cap_max:.1f} degC) for "
              f"{dur:.0f} min while at 100% cooling ({s['capacity_w']:.0f} W), "
              f"outdoor {t_out:.1f} degC, {s['occ']} occupants")
        self._raise(key, "capacity", z, sev, conf, ev,
                    f"Inspect the AHU/DX unit serving {_name(z)}: check refrigerant "
                    f"charge, coil fouling and supply airflow. The unit cannot hold "
                    f"the band at this load - it is undersized or degraded.",
                    now, changed)

    # ---- detector 2: sensor ---------------------------------------------
    def _sensor(self, z, snaps, devs, outlier, now, changed) -> None:
        st = self._zs[z]
        key = ("sensor", z)
        s = snaps[z]
        nbrs = NEIGHBOURS.get(z) or []
        if not nbrs:
            return
        med = _median([snaps[n]["temp_c"] for n in nbrs])
        dev = devs[z]
        # A single neighbour is a weaker reference, so demand a bigger offset.
        thresh = self.sen_dev * (1.5 if len(nbrs) == 1 else 1.0)
        # "Own cooling behaviour looks normal": the coil is not saturated (that
        # would be a capacity story) and the zone is being asked for roughly the
        # same setpoint as its neighbours (otherwise the gap is intentional).
        # ...and both are only meaningful while the zone is actually being
        # CONTROLLED to a setpoint alongside its neighbours. A building left to
        # free-float all afternoon separates by several kelvin purely on
        # orientation and thermal mass; calling that a sensor fault is the
        # classic FDD false alarm, so an unsetpointed zone is never judged.
        sp = s["setpoint_c"]
        nsp = [snaps[n]["setpoint_c"] for n in nbrs if snaps[n]["setpoint_c"] is not None]
        sp_ok = sp is not None and bool(nsp) and abs(sp - _median(nsp)) <= SETPOINT_TOL_K
        sign = 1 if dev > 0 else -1
        # Shadowing rule (kills the mirror-image false positive): a local median
        # test cannot tell "I read high" from "my neighbour reads low" - one
        # zone stuck +7 K makes every neighbour look several K low. Ranked
        # against the building median, only the most anomalous zone in a
        # neighbourhood is reported; the others are explained by it.
        shadowed = any(abs(outlier.get(n, 0.0)) > abs(outlier.get(z, 0.0)) for n in nbrs)
        cond = abs(dev) > thresh and not s["at_capacity"] and sp_ok and not shadowed
        if cond:
            if st.sen_start is None or st.sen_sign != sign:
                st.sen_start, st.sen_sign = now, sign
                st.sen_max, st.sen_sum, st.sen_n = 0.0, 0.0, 0
            st.sen_last = now
            st.sen_max = max(st.sen_max, abs(dev))
            st.sen_sum += dev
            st.sen_n += 1
            self._unclear(key)
        elif st.sen_start is not None and now - st.sen_last > self.grace_s:
            st.sen_start, st.sen_n, st.sen_sum = None, 0, 0.0
        if st.sen_start is None:
            self._mark_clear(key, now)
            return
        dur = (now - st.sen_start) / 60.0
        if dur < self.sen_win or st.sen_n == 0:
            return
        mean_dev = st.sen_sum / st.sen_n
        cap = 0.70 if len(nbrs) == 1 else 0.92
        # Confidence: 0.45 at the window, +0.10 per further window, +0.06 per
        # kelvin beyond the threshold. Capped lower with a single reference.
        conf = min(cap, 0.45 + 0.10 * (dur / self.sen_win - 1.0)
                   + 0.06 * min(st.sen_max - thresh, 5.0))
        sev = "high" if (st.sen_max >= 2 * thresh or dur >= 3 * self.sen_win) else \
              ("medium" if dur >= 2 * self.sen_win else "low")
        nlist = ", ".join(_name(n) for n in nbrs)
        ev = (f"{_name(z)} read {s['temp_c']:.1f} degC while its neighbours ({nlist}) "
              f"sat at a median {med:.1f} degC - a {mean_dev:+.1f} K offset "
              f"(peak {st.sen_max:.1f} K) sustained {dur:.0f} min at the same "
              f"commanded setpoint with no capacity shortfall")
        self._raise(key, "sensor", z, sev, conf, ev,
                    f"Verify the zone temperature sensor in {_name(z)} against a "
                    f"handheld reading; recalibrate or replace it if the offset is "
                    f"confirmed. Check for a sensor mounted in sun or over a heat source.",
                    now, changed)

    # ---- detector 3: actuator -------------------------------------------
    def _actuator(self, z, s, cmd, dew_out, now, changed) -> None:
        st = self._zs[z]
        key = ("actuator", z)
        vent = s["vent"] if (cmd is None or cmd.get("vent") is None) else int(cmd["vent"])
        sp = s["setpoint_c"] if (cmd is None or cmd.get("sp") is None) else cmd["sp"]
        prev = vent if st.act_prev is None else st.act_prev
        st.act_prev = vent

        ep = st.act_ep
        if ep is not None and (vent < ep["level"] or s["occ"] <= 0):
            ep = st.act_ep = None      # airflow backed off, or nobody is there
        if ep is None and vent > prev and vent >= 1:
            # Only worth watching when there was something to fix, cooling was
            # actually being asked for, and someone is in the room.
            hurting = s["temp_c"] > BAND[1] or s["rh_pct"] > RH_HUMID
            asked = (sp is not None and sp < s["temp_c"] - 0.3) or \
                    (sp is None and s["temp_c"] - 1.0 > dew_out and s["temp_c"] > BAND[1])
            if hurting and asked and s["occ"] > 0:
                st.act_ep = ep = {"level": vent, "t0": now, "temp0": s["temp_c"],
                                  "rh0": s["rh_pct"], "dew0": s["dew_point_c"],
                                  "cap": False, "cycles": 0}
        if ep is None:
            self._mark_clear(key, now)
            return
        if s["at_capacity"]:
            ep["cap"] = True           # capacity owns this symptom, not the fan
        if (now - ep["t0"]) / 60.0 < self.act_win:
            return

        d_temp = s["temp_c"] - ep["temp0"]
        d_dew = s["dew_point_c"] - ep["dew0"]
        improved = d_temp <= -TEMP_IMPROVE_K or d_dew <= -DEW_IMPROVE_K
        if improved or ep["cap"]:
            st.act_ep = None
            self._mark_clear(key, now)
            return
        ep["cycles"] += 1
        self._unclear(key)
        # Corroboration, not a gate: air that refuses to exchange moisture with
        # outdoors despite a real dew-point gradient is the "no airflow" tell.
        flat = abs(d_dew) < FLAT_DEW_K and abs(s["dew_point_c"] - dew_out) > DEW_GRADIENT_K
        conf = min(0.75, 0.40 + 0.10 * (ep["cycles"] - 1) + (0.15 if flat else 0.0))
        sev = "medium" if ep["cycles"] >= 2 else "low"
        ev = (f"Airflow commanded to level {ep['level']} in {_name(z)} for "
              f"{self.act_win:.0f} min: temperature {ep['temp0']:.1f} -> "
              f"{s['temp_c']:.1f} degC ({d_temp:+.1f} K), RH {ep['rh0']:.0f} -> "
              f"{s['rh_pct']:.0f}%, dew point {d_dew:+.2f} K - no measurable response")
        if flat:
            ev += (f"; moisture flat despite a {s['dew_point_c'] - dew_out:+.1f} K "
                   f"dew-point gradient to outdoor air")
        self._raise(key, "actuator", z, sev, conf, ev,
                    f"Check the supply damper, VFD and fan serving {_name(z)}: airflow "
                    f"is being commanded but the zone shows no thermal or moisture "
                    f"response. Confirm the damper strokes and the belt/impeller turns.",
                    now, changed)
        ep["t0"], ep["temp0"] = now, s["temp_c"]      # re-baseline for the next window
        ep["rh0"], ep["dew0"], ep["cap"] = s["rh_pct"], s["dew_point_c"], False

    # ---- detector 4: recurring complaints --------------------------------
    def _recurring(self, store, now, changed) -> None:
        items = list(getattr(store, "items", []) or []) if store is not None else []
        if len(items) < self._cursor:                 # store was replaced/cleared
            self._cursor, self._rec = 0, {}
        fresh = set()
        for c in items[self._cursor:]:
            if getattr(c, "author", "") == MEMORY_AUTHOR:
                continue                              # our own pre-applies are not evidence
            k = (c.zone, c.issue)
            r = self._rec.get(k)
            if r is None:
                r = self._rec[k] = {"days": set(), "ts": deque(maxlen=64), "n": 0,
                                    "last": -1e18, "burst": 0, "text": ""}
            r["days"].add(int(c.created_t // 86400.0))
            r["ts"].append(float(c.created_t))
            r["n"] += 1
            r["last"] = max(r["last"], float(c.created_t))
            r["text"] = getattr(c, "text", "") or r["text"]
            fresh.add(k)
        self._cursor = len(items)

        for k, r in self._rec.items():
            if k in fresh:                            # recompute only on new evidence
                ts = sorted(r["ts"])
                best, j = 0, 0
                for i in range(len(ts)):              # two-pointer, bounded by maxlen
                    while ts[i] - ts[j] > RECUR_BURST_S:
                        j += 1
                    best = max(best, i - j + 1)
                r["burst"] = best
            zone, issue = k
            key = ("recurring", zone, issue)
            n_days, burst = len(r["days"]), r["burst"]
            qualifies = n_days >= RECUR_DAYS or burst >= RECUR_BURST_N
            if not qualifies or now - r["last"] > RECUR_QUIET_S:
                self._mark_clear(key, now)
                continue
            self._unclear(key)
            # Confidence: 0.45 at the trigger, +0.10 per extra day, +0.05 per
            # extra report, +0.10 if they also cluster inside a single day.
            conf = min(0.95, 0.45 + 0.10 * max(0, n_days - RECUR_DAYS)
                       + 0.05 * max(0, r["n"] - RECUR_DAYS)
                       + (0.10 if burst >= RECUR_BURST_N else 0.0))
            sev = "high" if n_days >= 5 or r["n"] >= 8 else \
                  ("medium" if n_days >= 4 or burst >= RECUR_BURST_N else "low")
            days = ", ".join(DAYS[d % 7] for d in sorted(r["days"]))
            ev = (f"{r['n']} occupant report(s) of '{issue.replace('_', ' ')}' in "
                  f"{_name(zone)} across {n_days} distinct day(s) ({days}); "
                  f"peak {burst} within 24 h; most recent {_clock(r['last'])}")
            self._raise(key, "recurring", zone, sev, conf, ev,
                        f"Schedule a survey of {_name(zone)}: '{issue.replace('_', ' ')}' "
                        f"keeps coming back after the setpoint is corrected, which points "
                        f"at air balancing, zoning or envelope rather than a one-off.",
                        now, changed)

    # ---- alert bookkeeping ------------------------------------------------
    def _raise(self, key, kind, zone, severity, confidence, evidence,
               recommendation, now, changed) -> None:
        """Create or update the single alert for `key`; never duplicates it."""
        tr = self._alerts.get(key)
        if tr is None:
            a = MaintenanceAlert(id=new_id("mnt", self._ids), kind=kind, zone=zone,
                                 zone_name=_name(zone), severity=severity,
                                 confidence=round(confidence, 2), evidence=[evidence],
                                 recommendation=recommendation,
                                 first_seen_t=now, last_seen_t=now)
            self._alerts[key] = tr = _Tracked(a, None, now, {evidence})
            changed[key] = tr
            return
        a = tr.alert
        a.last_seen_t = now
        a.recommendation = recommendation
        bumped = severity != a.severity
        a.severity = severity
        a.confidence = round(max(a.confidence, confidence), 2)   # monotone: evidence only accrues
        tr.clear_since = None
        if evidence not in tr.seen and now - tr.last_ev_t >= EVIDENCE_INTERVAL_MIN * 60.0:
            a.evidence.append(evidence)
            tr.seen.add(evidence)
            tr.last_ev_t = now
            if len(a.evidence) > MAX_EVIDENCE:
                a.evidence = a.evidence[:1] + a.evidence[-(MAX_EVIDENCE - 1):]
            bumped = True
        if bumped:
            changed[key] = tr

    def _unclear(self, key) -> None:
        tr = self._alerts.get(key)
        if tr is not None:
            tr.clear_since = None

    def _mark_clear(self, key, now) -> None:
        tr = self._alerts.get(key)
        if tr is not None and tr.clear_since is None:
            tr.clear_since = now

    def _expire(self, now) -> None:
        for key, tr in list(self._alerts.items()):
            if tr.clear_since is not None and now - tr.clear_since >= self.cooldown_s:
                d = to_dict(self._alerts.pop(key).alert)
                d["resolved_t"] = now
                self._history.append(d)

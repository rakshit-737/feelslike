"""Rolling analytics for FeelsLike: the numbers behind the Overview / Analytics tabs.

Why this exists: the live sim already knows everything (twin state, A/B meters,
constraints, decisions), but it knows it only *right now*. The dashboard needs
shapes over TIME — a zone x hour comfort heatmap, an hourly energy series, a
complaint breakdown — and the app must not grow an unbounded list to get them.

So this module is a bounded sampler. The caller pushes a snapshot on a cadence
(default every 15 sim-minutes); every reader method is a pure fold over the ring
buffer. Nothing here mutates the twin, the store, or the feed.

Three deliberate choices worth naming:

1. RECTANGLE RULE. A sample is an instant, but comfort is measured in MINUTES.
   Each sample therefore stands for the span since the previous sample (a
   backward/right-endpoint rectangle). The first retained sample has span 0 and
   contributes no minutes. This makes viol_min here an ESTIMATE of the twin's
   exact per-step counter — expect ~1% disagreement at a 15-min cadence, and
   read twin.metrics()["viol_min"] when you need the exact figure.

2. COMFORT IS ONLY DEFINED WHEN OCCUPIED. Heatmap deviation averages occupied
   samples only. A cell with nobody in it reports deviation 0.0 and
   occ_samples 0 so a renderer can grey it out, rather than painting the night
   setback bright red and burying the real signal.

3. ROUND ONLY ON THE WIRE. Accumulators hold raw floats straight off the twin;
   every returned number is rounded at the boundary (2 dp, or 1 dp for percents
   that are already coarse). No numpy, no dataclasses cross this boundary.

Units, everywhere: temperatures degC, energy kWh, money Rs, carbon kgCO2,
durations minutes unless the key says _h, times sim-seconds since Monday 00:00.
"""
from __future__ import annotations

from collections import deque

from backend.contracts import ISSUES, canonical_reason, to_dict
from sim.twin import (
    BAND,
    GRID_CO2,
    TARIFF,
    ZONE_BY_ID,
    ZONE_IDS,
    ZONES,
    occupancy,
)

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIDPOINT = (BAND[0] + BAND[1]) / 2.0     # degC, 24.75 for the 23.0-26.5 band
HOURS = tuple(range(24))

# The controller's hard safety clamp (sim/controllers.py ConstraintAware.act).
# Duplicated as a constant, not imported, because it is a literal there.
SAFE_MIN, SAFE_MAX = 21.5, 29.0

# Weekday design headcount per zone, for occupancy as a percentage. Recomputed
# from the same pure occupancy() the twin uses, so the numbers cannot drift.
_OCC_PEAK = {
    z.id: max([occupancy(z.occ_profile, 0, q / 4.0) for q in range(96)] + [1])
    for z in ZONES
}
_ZONE_NAME = {z.id: z.name for z in ZONES}
_ISSUE_RANK = {k: i for i, k in enumerate(ISSUES)}   # canonical tiebreak order


# --------------------------------------------------------------------------
# wire helpers
# --------------------------------------------------------------------------

def _r(v, n: int = 2):
    """Round for the wire, never raising: None/junk/nan/inf all become None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, n)


def _clock(t: float) -> str:
    """Sim-seconds -> "Wed 14:35", the same clock string /api/state emits."""
    d = int(t // 86400)
    h = (t % 86400) / 3600.0
    return f"{DAYS[d % 7]} {int(h):02d}:{int((h % 1) * 60):02d}"


def _hour_of_clock(s) -> int | None:
    """Pull the hour out of a "Wed 14:35" clock string; None if unparseable."""
    try:
        return int(str(s).strip().split()[-1].split(":")[0]) % 24
    except (ValueError, IndexError, AttributeError):
        return None


def _num(v, default=0.0) -> float:
    """Coerce to float, falling back to default (feed/decision dicts are untrusted)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and f not in (float("inf"), float("-inf")) else default


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole > 1e-12 else 0.0


def _sorted_counts(counter: dict) -> list:
    """dict -> [{"key","count"}] sorted by count desc then key, for stable output."""
    return [{"key": str(k), "count": int(v)}
            for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))]


class AnalyticsStore:
    """Bounded rolling sampler over the live A/B simulation.

    INPUT: snapshots pushed by the sim loop via sample(); feed lists and decision
      lists passed straight into the reader methods (this module never reaches
      into backend.app).
    OUTPUT: every public reader returns json-safe primitives — dicts, lists,
      str/int/float/bool/None. No dataclasses, no numpy, no datetime.
    SIDE EFFECTS: appends to its own ring buffers only. Never touches the twin,
      the constraint store, or any caller-owned list.
    ERROR STATES: none by design. An empty store yields empty-but-valid shapes
      (never None, never a half-built dict), and malformed feed/decision entries
      are skipped rather than raised on.
    """

    def __init__(self, days: float = 8.0, sample_min: float = 15.0):
        """INPUT: days = sim-days of history to retain; sample_min = the cadence
        the caller intends to sample at, in sim-minutes (used to size the ring
        buffer and to clamp span estimates after a pause).
        OUTPUT: an empty store. SIDE EFFECTS: none. ERROR STATES: a sample_min
        of <= 0 is coerced to 15.0 rather than dividing by zero.
        """
        self.sample_min = float(sample_min) if sample_min and sample_min > 0 else 15.0
        self.days = float(days) if days and days > 0 else 8.0
        cap = max(8, int(self.days * 24 * 60 / self.sample_min))
        self.capacity = cap
        self.samples: deque = deque(maxlen=cap)
        self._last_t: float | None = None
        self._last_feed: list = []          # remembered for summary()
        self._last_alerts: list = []        # set via note_alerts()

    def __len__(self) -> int:
        return len(self.samples)

    def clear(self) -> None:
        """Drop all history and remembered feed/alerts. SIDE EFFECTS: local only."""
        self.samples.clear()
        self._last_t = None
        self._last_feed = []
        self._last_alerts = []

    # ---- ingest ---------------------------------------------------------
    def sample(self, twin, base_twin=None, store=None) -> dict:
        """Record one snapshot of the live sim. Call on a fixed sim cadence.

        INPUT: twin = the FeelsLike DigitalTwin (required); base_twin = the
          lock-step baseline twin (optional, None means "no A/B this run");
          store = the ConstraintStore (optional).
        OUTPUT: the raw sample dict that was stored (unrounded — do not put it
          on the wire directly; it is returned for tests and for callers that
          want to know the span that was credited).
        SIDE EFFECTS: appends to the ring buffer, evicting the oldest sample
          once capacity (days x 24 x 60 / sample_min) is reached.
        ERROR STATES: none. A clock that jumped backwards (twin restarted) is
          credited span 0.0; a gap longer than 4x the cadence is clamped to 4x
          so one paused demo cannot fabricate hours of violation minutes.
        """
        t = float(getattr(twin, "t", 0.0))
        if self._last_t is None or t < self._last_t:
            span = 0.0
        else:
            span = min((t - self._last_t) / 60.0, 4.0 * self.sample_min)
        self._last_t = t

        t_out = _num(twin.weather_fn(t), 0.0) + _num(getattr(twin, "outdoor_offset", 0.0))
        active_by_zone, active_total = {}, 0
        if store is not None:
            for zid in ZONE_IDS:
                n = len(store.active(t, zid))
                active_by_zone[zid] = n
                active_total += n

        zones = {}
        lo, hi = BAND
        for zid in ZONE_IDS:
            temp = _num(twin.T.get(zid), MIDPOINT)
            occ = int(twin.occupancy_now(zid))
            dev = temp - MIDPOINT
            viol = span if (occ > 0 and (temp > hi or temp < lo)) else 0.0
            zones[zid] = {
                "temp": temp,
                "rh": _num(twin.rh_now(zid)) if hasattr(twin, "rh_now") else 0.0,
                "occ": occ,
                "occ_pct": 100.0 * occ / _OCC_PEAK[zid],
                "setpoint": twin.last_setpoints.get(zid),
                "vent": int(twin.last_vents.get(zid, 0)),
                "kwh": _num(twin.kwh_by_zone.get(zid)),
                "base_temp": _num(base_twin.T.get(zid), temp) if base_twin is not None else None,
                "dev": dev,
                "viol_min": viol,
                "constraints": active_by_zone.get(zid, 0),
            }

        rec = {
            "t": t,
            "day": int(t // 86400),
            "hour": int((t % 86400) // 3600),
            "span_min": span,
            "t_out": t_out,
            "kwh_us": _num(twin.kwh),
            "kwh_base": _num(getattr(base_twin, "kwh", 0.0)) if base_twin is not None else 0.0,
            "viol_us": _num(twin.viol_min),
            "viol_base": _num(getattr(base_twin, "viol_min", 0.0)) if base_twin is not None else 0.0,
            "hot_us": _num(twin.hot_deg_min),
            "cold_us": _num(twin.cold_deg_min),
            "active_constraints": active_total,
            "zones": zones,
        }
        self.samples.append(rec)
        return rec

    def note_alerts(self, alerts) -> int:
        """Remember the current MaintenanceMonitor alerts so summary() can count them.

        INPUT: list of alert dicts/dataclasses (or None).
        OUTPUT: how many were remembered.
        SIDE EFFECTS: replaces the remembered list; the caller's list is copied,
          never held by reference.
        ERROR STATES: none; a non-list is treated as empty.
        """
        self._last_alerts = list(alerts) if isinstance(alerts, (list, tuple)) else []
        return len(self._last_alerts)

    # ---- 2. comfort heatmap ---------------------------------------------
    def comfort_heatmap(self) -> dict:
        """Zone x hour-of-day grid of comfort deviation, ready for a plain SVG grid.

        OUTPUT: {"zones":[{"id","name"}...], "hours":[0..23],
                 "cells":[{"zone","hour","deviation","viol_min","samples",
                           "occ_samples"}...],
                 "band":[lo,hi], "midpoint":float, "max_abs_deviation":float,
                 "window_h":float}
          deviation = mean(temp - 24.75) in degC over OCCUPIED samples in that
            cell; positive = too warm. 0.0 with occ_samples 0 means "nobody was
            ever in that zone at that hour" — grey the cell, do not colour it.
          viol_min = estimated occupied minutes outside the 23.0-26.5 band
            (rectangle rule, see the module docstring).
          samples = every sample in the cell, occupied or not.
          max_abs_deviation is provided so a renderer can pick a symmetric
            colour scale in one pass; it is 0.0 on an empty store.
        SIDE EFFECTS: none. ERROR STATES: none — an empty store returns the full
          5 x 24 grid of zeroed cells, so the SVG always has something to draw.
        """
        acc = {(z, h): [0.0, 0, 0.0, 0] for z in ZONE_IDS for h in HOURS}
        # value = [dev_sum(occupied), occ_samples, viol_min, samples]
        for s in self.samples:
            h = s["hour"]
            for zid, zs in s["zones"].items():
                cell = acc.get((zid, h))
                if cell is None:
                    continue
                cell[3] += 1
                cell[2] += zs["viol_min"]
                if zs["occ"] > 0:
                    cell[0] += zs["dev"]
                    cell[1] += 1

        cells, worst = [], 0.0
        for zid in ZONE_IDS:
            for h in HOURS:
                dev_sum, occ_n, viol, n = acc[(zid, h)]
                dev = dev_sum / occ_n if occ_n else 0.0
                worst = max(worst, abs(dev))
                cells.append({
                    "zone": zid, "hour": h,
                    "deviation": _r(dev), "viol_min": _r(viol, 1),
                    "samples": n, "occ_samples": occ_n,
                })
        return {
            "zones": [{"id": z, "name": _ZONE_NAME[z]} for z in ZONE_IDS],
            "hours": list(HOURS),
            "cells": cells,
            "band": [BAND[0], BAND[1]],
            "midpoint": MIDPOINT,
            "max_abs_deviation": _r(worst),
            "window_h": _r(self.window_h(), 2),
        }

    # ---- 3. energy -------------------------------------------------------
    def window_h(self) -> float:
        """Sim-hours currently retained (0.0 with fewer than two samples)."""
        if len(self.samples) < 2:
            return 0.0
        return (self.samples[-1]["t"] - self.samples[0]["t"]) / 3600.0

    def energy_series(self) -> dict:
        """Per-hour and per-day kWh for us vs baseline, per-zone split, savings.

        Deltas are differences of the twins' CUMULATIVE kWh between consecutive
        retained samples, credited to the bucket of the later sample. The oldest
        retained sample only seeds the difference and contributes no energy, so
        the totals here cover the window, not the whole run.

        OUTPUT: {"hourly":[{"t","day","hour","label","us","base","saved"}...],
                 "daily":[{"day","label","us","base","saved"}...],
                 "by_zone":[{"zone","name","kwh","pct"}...],
                 "totals":{"us","base","saved_kwh","saved_pct","saved_rs","saved_co2"},
                 "window_h":float,"samples":int,"cumulative":{"us","base"}}
          All energy in kWh, saved_rs in Rs at TARIFF, saved_co2 in kgCO2 at
          GRID_CO2, pct in percent. saved_* are floored at 0 — the honest signed
          difference is still available as totals.base - totals.us.
        SIDE EFFECTS: none. ERROR STATES: none; fewer than two samples yields
          empty series and zeroed totals.
        """
        hourly: dict = {}
        daily: dict = {}
        tot_us = tot_base = 0.0
        prev = None
        for s in self.samples:
            if prev is not None:
                d_us = max(0.0, s["kwh_us"] - prev["kwh_us"])
                d_base = max(0.0, s["kwh_base"] - prev["kwh_base"])
                key = int(s["t"] // 3600)
                hb = hourly.setdefault(key, [0.0, 0.0])
                hb[0] += d_us
                hb[1] += d_base
                db = daily.setdefault(s["day"], [0.0, 0.0])
                db[0] += d_us
                db[1] += d_base
                tot_us += d_us
                tot_base += d_base
            prev = s

        h_rows = []
        for key in sorted(hourly):
            us, base = hourly[key]
            t0 = key * 3600.0
            h_rows.append({
                "t": t0, "day": int(t0 // 86400), "hour": int((t0 % 86400) // 3600),
                "label": _clock(t0), "us": _r(us, 3), "base": _r(base, 3),
                "saved": _r(base - us, 3),
            })
        d_rows = []
        for day in sorted(daily):
            us, base = daily[day]
            d_rows.append({"day": day, "label": DAYS[day % 7],
                           "us": _r(us), "base": _r(base), "saved": _r(base - us)})

        by_zone = []
        if len(self.samples) >= 2:
            first, last = self.samples[0], self.samples[-1]
            raw = {z: max(0.0, last["zones"][z]["kwh"] - first["zones"][z]["kwh"])
                   for z in ZONE_IDS}
            zsum = sum(raw.values())
            for zid in ZONE_IDS:
                by_zone.append({"zone": zid, "name": _ZONE_NAME[zid],
                                "kwh": _r(raw[zid], 3), "pct": _r(_pct(raw[zid], zsum), 1)})

        saved = max(0.0, tot_base - tot_us)
        return {
            "hourly": h_rows,
            "daily": d_rows,
            "by_zone": by_zone,
            "totals": {
                "us": _r(tot_us), "base": _r(tot_base),
                "saved_kwh": _r(saved), "saved_pct": _r(_pct(saved, tot_base), 1),
                "saved_rs": _r(saved * TARIFF, 1), "saved_co2": _r(saved * GRID_CO2),
            },
            "cumulative": {
                "us": _r(self.samples[-1]["kwh_us"]) if self.samples else 0.0,
                "base": _r(self.samples[-1]["kwh_base"]) if self.samples else 0.0,
            },
            "window_h": _r(self.window_h(), 2),
            "samples": len(self.samples),
        }

    # ---- 4. complaints ---------------------------------------------------
    def complaint_stats(self, feed) -> dict:
        """Fold the occupant feed into frequency / severity / parser breakdowns.

        INPUT: the feed list from backend.app.LiveSim (list of dicts with keys
          author, text, source, latency_ms, parsed{...}, action, sim_clock).
          None or a non-list is treated as empty. The list is READ ONLY and is
          remembered (by reference to a shallow copy) for summary().
        OUTPUT: {"total":int,            # entries that parsed as comfort complaints
                 "entries":int,          # every feed entry, complaint or not
                 "by_zone":[{"zone","name","count"}...],   # multi-zone counts once per zone
                 "by_issue":[{"key","count"}...],       # most frequent first
                 "by_hour":[{"hour",0..23,"count"}...],    # always 24 rows
                 "severity":{"1":n,"2":n,"3":n},
                 "recurring":[{"zone","name","issue","count"}...],  # count >= 2, desc
                 "source":{"llm":n,"rules":n,...},"source_pct":{...},
                 "mean_latency_ms":float|None,"p95_latency_ms":float|None,
                 "by_action":[{"key","count"}],"by_language":[{"key","count"}],
                 "mean_confidence":float|None,"clarification_rate":float}
          Hour buckets come from the entry's sim_clock string; an entry without a
          parseable clock is counted in totals but in no hour bucket.
        SIDE EFFECTS: stores a shallow copy of the feed for summary().
        ERROR STATES: none; entries missing "parsed" or with junk values are
          skipped for the field they lack, never raised on.
        """
        feed = list(feed) if isinstance(feed, (list, tuple)) else []
        self._last_feed = feed

        by_zone: dict = {}
        by_issue: dict = {}
        by_hour = {h: 0 for h in HOURS}
        severity = {1: 0, 2: 0, 3: 0}
        recurring: dict = {}
        source: dict = {}
        action: dict = {}
        language: dict = {}
        latencies: list = []
        confidences: list = []
        total = clarify = 0

        for e in feed:
            if not isinstance(e, dict):
                continue
            src = str(e.get("source", "unknown"))
            source[src] = source.get(src, 0) + 1
            act = str(e.get("action", "")).split(" ")[0] or "none"
            action[act] = action.get(act, 0) + 1
            lat = e.get("latency_ms")
            if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                latencies.append(float(lat))

            p = e.get("parsed") or {}
            if not isinstance(p, dict) or not p.get("is_comfort_complaint"):
                continue
            total += 1
            lang = str(p.get("language", "en"))
            language[lang] = language.get(lang, 0) + 1
            if p.get("requires_clarification"):
                clarify += 1
            conf = p.get("confidence")
            if isinstance(conf, (int, float)) and not isinstance(conf, bool):
                confidences.append(float(conf))

            sev = int(_num(p.get("severity"), 1))
            severity[min(3, max(1, sev))] += 1
            issue = str(p.get("issue", "other"))
            by_issue[issue] = by_issue.get(issue, 0) + 1

            h = _hour_of_clock(e.get("sim_clock"))
            if h is not None:
                by_hour[h] += 1

            zids = p.get("zone_ids") or ([p["zone_id"]] if p.get("zone_id") else [])
            for zid in zids:
                if zid not in ZONE_BY_ID:
                    continue
                by_zone[zid] = by_zone.get(zid, 0) + 1
                k = (zid, issue)
                recurring[k] = recurring.get(k, 0) + 1

        lat_sorted = sorted(latencies)
        p95 = lat_sorted[min(len(lat_sorted) - 1, int(0.95 * len(lat_sorted)))] if lat_sorted else None
        n_src = sum(source.values())
        return {
            "total": total,
            "entries": len(feed),
            "by_zone": [{"zone": z, "name": _ZONE_NAME[z], "count": c}
                        for z, c in sorted(by_zone.items(), key=lambda kv: (-kv[1], kv[0]))],
            # most frequent first (summary().top_issue reads row 0); ties break on
            # the canonical ISSUES order, then alphabetically, so output is stable.
            "by_issue": [{"key": str(k), "count": v} for k, v in
                         sorted(by_issue.items(),
                                key=lambda kv: (-kv[1], _ISSUE_RANK.get(kv[0], 99),
                                                str(kv[0])))],
            "by_hour": [{"hour": h, "count": by_hour[h]} for h in HOURS],
            "severity": {str(k): v for k, v in severity.items()},
            "recurring": [{"zone": z, "name": _ZONE_NAME[z], "issue": i, "count": c}
                          for (z, i), c in sorted(recurring.items(),
                                                  key=lambda kv: (-kv[1], kv[0]))
                          if c >= 2],
            "source": {k: v for k, v in sorted(source.items())},
            "source_pct": {k: _r(_pct(v, n_src), 1) for k, v in sorted(source.items())},
            "mean_latency_ms": _r(sum(latencies) / len(latencies), 1) if latencies else None,
            "p95_latency_ms": _r(p95, 1),
            "by_action": _sorted_counts(action),
            "by_language": _sorted_counts(language),
            "mean_confidence": _r(sum(confidences) / len(confidences), 3) if confidences else None,
            "clarification_rate": _r(_pct(clarify, total), 1),
        }

    # ---- 5. controller ---------------------------------------------------
    def controller_stats(self, decisions) -> dict:
        """Fold a ControllerDecision log into intervention / arbitration stats.

        INPUT: a list of ControllerDecision dataclasses OR of the dicts
          DecisionLog.recent() returns — both are accepted (dataclasses go
          through backend.contracts.to_dict). None is treated as empty.
        OUTPUT: {"total":int,"applied":int,"applied_pct":float,
                 "interventions":int,        # applied AND offset_c != 0
                 "intervention_rate_pct":float,
                 "interventions_by_hour":[{"hour",0..23,"count"}],  # always 24 rows
                 "interventions_per_sim_hour":float,
                 "setpoint_changes_by_zone":[{"zone","name","changes","decisions"}],
                 "mean_offset_c":float,      # mean |offset| over ALL decisions
                 "mean_offset_when_moved_c":float|None,   # mean |offset| when nonzero
                 "max_offset_c":float,
                 "by_objective":[{"key","count"}],"by_reason":[{"key","count"}],
                 "by_arbitration":[{"key","count"}],
                 "conflicts":int,"conflict_pct":float,
                 "clamped":int,"clamped_pct":float,"at_envelope":int,
                 "safety_envelope":[21.5,29.0]}
          An INTERVENTION is an applied decision whose setpoint differs from the
          schedule's base setpoint — i.e. the occupant channel actually changed
          what the building did. clamped counts decisions whose reason_code
          resolves (via contracts.canonical_reason) to "clamped"; at_envelope
          independently counts decisions that landed exactly on the 21.5/29.0
          rails. These are NOT the same event: a setback of exactly 29.0 degC
          (the energy/cost/carbon objectives) sits on the rail without any
          clamp having bound, so at_envelope >= clamped is normal.
        SIDE EFFECTS: none; the caller's list is not mutated or retained.
        ERROR STATES: none; entries that are neither dict nor dataclass, and
          missing fields, are skipped or defaulted.
        """
        rows = []
        for d in (decisions if isinstance(decisions, (list, tuple)) else []):
            if not isinstance(d, dict):
                d = to_dict(d)
            if isinstance(d, dict):
                rows.append(d)

        by_obj: dict = {}
        by_reason: dict = {}
        by_arb: dict = {}
        by_hour = {h: 0 for h in HOURS}
        per_zone: dict = {z: [0, 0] for z in ZONE_IDS}   # [changes, decisions]
        applied = interventions = conflicts = clamped = at_env = 0
        off_sum = off_moved_sum = off_moved_n = 0
        off_max = 0.0
        t_lo = t_hi = None

        for d in rows:
            obj = str(d.get("objective", "balanced"))
            by_obj[obj] = by_obj.get(obj, 0) + 1
            rc = str(d.get("reason_code", "occupied_base"))
            by_reason[rc] = by_reason.get(rc, 0) + 1
            arb = str(d.get("arbitration", "none"))
            by_arb[arb] = by_arb.get(arb, 0) + 1
            if d.get("conflict"):
                conflicts += 1
            # canonical_reason() bridges the controller's "safety_clamp" spelling
            # onto the canonical "clamped" key; comparing rc directly made this
            # counter permanently 0 because nothing emits "clamped" verbatim.
            if canonical_reason(rc) == "clamped":
                clamped += 1

            t = _num(d.get("t"), -1.0)
            if t >= 0.0:
                t_lo = t if t_lo is None else min(t_lo, t)
                t_hi = t if t_hi is None else max(t_hi, t)

            off = abs(_num(d.get("offset_c")))
            off_sum += off
            off_max = max(off_max, off)
            if off > 1e-9:
                off_moved_sum += off
                off_moved_n += 1

            new_sp, prev_sp = d.get("new_setpoint"), d.get("prev_setpoint")
            if new_sp is not None and (abs(_num(new_sp) - SAFE_MIN) < 1e-6
                                       or abs(_num(new_sp) - SAFE_MAX) < 1e-6):
                at_env += 1

            zid = str(d.get("zone", ""))
            if zid in per_zone:
                per_zone[zid][1] += 1
                if new_sp != prev_sp:
                    per_zone[zid][0] += 1

            is_applied = bool(d.get("applied", True))
            if is_applied:
                applied += 1
                if off > 1e-9:
                    interventions += 1
                    h = _hour_of_clock(d.get("sim_clock"))
                    if h is None and t >= 0.0:
                        h = int((t % 86400) // 3600)
                    if h is not None:
                        by_hour[h] += 1

        n = len(rows)
        span_h = ((t_hi - t_lo) / 3600.0) if (t_lo is not None and t_hi is not None
                                              and t_hi > t_lo) else 0.0
        return {
            "total": n,
            "applied": applied,
            "applied_pct": _r(_pct(applied, n), 1),
            "interventions": interventions,
            "intervention_rate_pct": _r(_pct(interventions, n), 1),
            "interventions_by_hour": [{"hour": h, "count": by_hour[h]} for h in HOURS],
            "interventions_per_sim_hour": _r(interventions / span_h, 2) if span_h else 0.0,
            "setpoint_changes_by_zone": [
                {"zone": z, "name": _ZONE_NAME[z], "changes": per_zone[z][0],
                 "decisions": per_zone[z][1]} for z in ZONE_IDS],
            "mean_offset_c": _r(off_sum / n, 3) if n else 0.0,
            "mean_offset_when_moved_c": _r(off_moved_sum / off_moved_n, 3) if off_moved_n else None,
            "max_offset_c": _r(off_max, 2),
            "by_objective": _sorted_counts(by_obj),
            "by_reason": _sorted_counts(by_reason),
            "by_arbitration": _sorted_counts(by_arb),
            "conflicts": conflicts,
            "conflict_pct": _r(_pct(conflicts, n), 1),
            "clamped": clamped,
            "clamped_pct": _r(_pct(clamped, n), 1),
            "at_envelope": at_env,
            "safety_envelope": [SAFE_MIN, SAFE_MAX],
        }

    # ---- 6. setpoint timeline -------------------------------------------
    def setpoint_timeline(self, zone: str | None = None, hours: float = 24.0) -> dict:
        """Stepped setpoint / temperature series for a timeline chart.

        INPUT: zone = a zone_id, or None for every zone; hours = how many
          sim-hours back from the newest sample to include (<= 0 means all).
        OUTPUT: {"hours":float,"t_start":float,"t_end":float,"band":[lo,hi],
                 "series":[{"zone","name","points":[{"t","clock","setpoint",
                            "temp","occ","vent","rh"}...]}...]}
          STEPPED semantics: each point's setpoint/vent holds from its own t
          until the next point's t (draw with step-after, not a smooth line).
          setpoint is null when HVAC was off — do not interpolate across it,
          break the line. temp/rh are instantaneous readings at t, in degC / %.
        SIDE EFFECTS: none.
        ERROR STATES: none. An unknown zone id yields a series list with that
          zone and an empty points list, so the chart renders an empty lane
          rather than throwing.
        """
        zids = [zone] if zone else list(ZONE_IDS)
        if not self.samples:
            return {"hours": _r(hours, 2), "t_start": 0.0, "t_end": 0.0,
                    "band": [BAND[0], BAND[1]],
                    "series": [{"zone": z, "name": _ZONE_NAME.get(z, z), "points": []}
                               for z in zids]}

        t_end = self.samples[-1]["t"]
        t_start = self.samples[0]["t"] if hours is None or hours <= 0 else max(
            self.samples[0]["t"], t_end - float(hours) * 3600.0)
        series = []
        for zid in zids:
            pts = []
            for s in self.samples:
                if s["t"] < t_start:
                    continue
                zs = s["zones"].get(zid)
                if zs is None:
                    continue
                pts.append({
                    "t": s["t"], "clock": _clock(s["t"]),
                    "setpoint": _r(zs["setpoint"]),
                    "temp": _r(zs["temp"]), "occ": zs["occ"],
                    "vent": zs["vent"], "rh": _r(zs["rh"], 1),
                })
            series.append({"zone": zid, "name": _ZONE_NAME.get(zid, zid), "points": pts})
        return {"hours": _r((t_end - t_start) / 3600.0, 2),
                "t_start": t_start, "t_end": t_end,
                "band": [BAND[0], BAND[1]], "series": series}

    # ---- 7. overview -----------------------------------------------------
    def summary(self, feed=None, alerts=None) -> dict:
        """One-call payload for the Overview tab.

        INPUT: feed / alerts are optional. When omitted, the feed last passed to
          complaint_stats() and the alerts last passed to note_alerts() are used,
          so summary() with no arguments is the intended call.
        OUTPUT: {"clock":str,"window_h":float,"samples":int,
                 "savings":{"kwh","pct","rs","co2_kg"},
                 "comfort":{"viol_min_us","viol_min_base","viol_saved_min",
                            "mean_deviation_c","worst_zone":{"zone","name",
                            "deviation"}|None},
                 "complaints":{"total","applied","top_issue"},
                 "active_constraints":int,"alerts":int,
                 "top_zones":[{"zone","name","complaints"}...]}   # at most 3
          savings.kwh/pct/rs/co2_kg are window figures from energy_series();
          comfort violation minutes are the twins' EXACT cumulative counters as
          of the newest sample (not the rectangle-rule estimate), so they match
          /api/state. mean_deviation_c averages every occupied zone-sample in
          the window; worst_zone is the zone with the largest |mean deviation|
          and is None until an occupied sample exists.
        SIDE EFFECTS: none beyond what complaint_stats() remembers when a feed
          is passed in.
        ERROR STATES: none; an empty store yields zeros and empty lists.
        """
        cs = self.complaint_stats(feed if feed is not None else self._last_feed)
        if alerts is not None:
            self.note_alerts(alerts)

        e = self.energy_series()
        last = self.samples[-1] if self.samples else None

        dev_sum, dev_n = 0.0, 0
        per_zone = {z: [0.0, 0] for z in ZONE_IDS}
        for s in self.samples:
            for zid, zs in s["zones"].items():
                if zs["occ"] > 0 and zid in per_zone:
                    dev_sum += zs["dev"]
                    dev_n += 1
                    per_zone[zid][0] += zs["dev"]
                    per_zone[zid][1] += 1
        worst = None
        for zid in ZONE_IDS:
            tot, cnt = per_zone[zid]
            if not cnt:
                continue
            d = tot / cnt
            if worst is None or abs(d) > abs(worst[1]):
                worst = (zid, d)

        applied = next((r["count"] for r in cs["by_action"] if r["key"] == "applied"), 0)
        top_issue = cs["by_issue"][0]["key"] if cs["by_issue"] else None
        return {
            "clock": _clock(last["t"]) if last else "—",
            "window_h": e["window_h"],
            "samples": len(self.samples),
            "savings": {
                "kwh": e["totals"]["saved_kwh"], "pct": e["totals"]["saved_pct"],
                "rs": e["totals"]["saved_rs"], "co2_kg": e["totals"]["saved_co2"],
            },
            "comfort": {
                "viol_min_us": _r(last["viol_us"], 1) if last else 0.0,
                "viol_min_base": _r(last["viol_base"], 1) if last else 0.0,
                "viol_saved_min": _r(max(0.0, last["viol_base"] - last["viol_us"]), 1) if last else 0.0,
                "mean_deviation_c": _r(dev_sum / dev_n, 2) if dev_n else 0.0,
                "worst_zone": ({"zone": worst[0], "name": _ZONE_NAME[worst[0]],
                                "deviation": _r(worst[1])} if worst else None),
            },
            "complaints": {"total": cs["total"], "applied": applied,
                           "top_issue": top_issue},
            "active_constraints": last["active_constraints"] if last else 0,
            "alerts": len(self._last_alerts),
            "top_zones": [{"zone": r["zone"], "name": r["name"], "complaints": r["count"]}
                          for r in cs["by_zone"][:3]],
        }

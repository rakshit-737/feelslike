"""Controllers: the wasteful baseline, a reactive thermostat, and FeelsLike's
constraint-aware controller (the honest fallback if the RL agent isn't ready).

Interface: controller.act(twin, constraint_store) -> (setpoints, vents)
where setpoints: zone_id -> degC | None and vents: zone_id -> 0|1|2.

Why ConstraintAware is shaped the way it is
-------------------------------------------
It is a *schedule* (occupancy + pre-cool + setback) plus an *adaptive offset*
(occupant complaints, crowd overload, latent load), and finally a *safety
envelope* that nothing may escape. Those three layers are deliberately separate:

  written_setpoint = clamp( schedule(objective) + clamp(offset, +-1.8 K) )

- The schedule numbers are DERIVED from backend.contracts.OBJECTIVE_WEIGHTS, not
  looked up from a table of magic constants, so a new objective is one row of
  weights rather than a new branch (see control_profile()).
- The offset is the only channel humans (and the humidity/crowd terms) get, and
  it is bounded, so no amount of complaining can drive the building unsafe.
- The envelope is a single choke-point function, _enforce_envelope(), and every
  setpoint this module emits goes through it exactly once.

reason_code vocabulary: the nine codes in backend.decisions.REASON_CODES, which
is what build_decision() emits and what the explainability panel renders.
backend.contracts.REASON_CODES spells four of them differently; the mapping is
backend.decisions.CONTRACT_REASON_ALIAS. This module never invents a tenth code.

Calibration invariant: objective="balanced" must reproduce the published A/B
result (7 days, seed 7: 722.4 kWh static vs 530.3 kWh FeelsLike, 0 violation
minutes). control_profile("balanced") therefore lands on exactly 25.0 / 26.0 /
28.5 degC with a 30-minute pre-cool lead — the values this controller shipped
with. Any change to the derivation constants below must keep that true.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.contracts import OBJECTIVE_WEIGHTS, OBJECTIVES, SAFETY_MODES, objective_weights
from sim.twin import RH_HUMID, ZONE_BY_ID, ZONES

# ---------------------------------------------------------------------------
# HARD SAFETY ENVELOPE. The only absolute limits in the system. Enforced last,
# by _enforce_envelope(), on every path including emergency override.
# ---------------------------------------------------------------------------
SAFE_LO = 21.5          # degC, coldest setpoint anyone may command
SAFE_HI = 29.0          # degC, warmest setpoint anyone may command
MAX_OFFSET = 1.8        # K, largest magnitude the adaptive/complaint offset may reach
VENT_LO, VENT_HI = 0, 2  # fan levels the twin understands

# ---------------------------------------------------------------------------
# Schedule derivation constants. Everything the objective changes comes from
# these five numbers plus the comfort/energy weights; see control_profile().
# ---------------------------------------------------------------------------
# The occupied setpoint sweeps the UPPER HALF of the twin's occupied comfort
# band BAND = (23.0, 26.5). Both ends sit inside the band, so no objective can
# book a comfort violation by schedule alone - it can only lose margin.
SP_TIGHT = 24.4         # degC at w_energy = 0 (pure comfort)
SP_LOOSE = 26.4         # degC at w_energy = 1 (pure energy), still under BAND[1]
PRECOOL_LIFT = 1.0      # K above the occupied target while pre-cooling. Pre-cooling
                        # is about LEAD TIME, not depth; the depth is bounded by the
                        # occupied target it is converging on.
SETBACK_LIFT = 3.5      # K above the occupied target when nobody is expected. Deep
                        # enough to coast, shallow enough to recover within the lead.
LEAD_MAX_H = 0.75       # h of pre-cool a pure-comfort objective buys
LEAD_QUANTUM_H = 0.25   # h; occupancy profiles are piecewise-constant on quarter
                        # hours (sim.twin.occupancy), so a finer lead is not meaningful

# Crowd-aware ventilation. Outdoor-air demand is per PERSON (ASHRAE 62.1 is
# cfm/person), so the boost needs both an absolute headcount and a high fraction
# of the zone's design headcount before it is worth the fan power.
VENT_BOOST_HEADCOUNT = 20
VENT_BOOST_DENSITY_PCT = 80.0

# Crowd-aware setpoint. Only bites ABOVE design headcount - see _density_offset.
DENSITY_GAIN = 2.0      # K of depression per 100% overload, at w_comfort = 1
DENSITY_MAX = 1.0       # K, hard cap on the crowd term

# Latent (humidity) term - see _humidity_offset for the SHR reasoning.
HUMID_MAX = 0.6         # K, hard cap on the humidity depression
HUMID_SPAN = 15.0       # %RH above RH_HUMID at which the term saturates

# Emergency override target: mid-band, comfortable for everyone, cheap enough to
# hold with the installed capacity.
EMERGENCY_SP = 24.0

_BALANCED_CW = OBJECTIVE_WEIGHTS["balanced"]["comfort"]   # 0.70

# Sentinel for "derive this from the objective". None is NOT usable here because
# base_unoccupied=None already means "HVAC off when the zone is empty", and that
# meaning predates this file's objective support.
_DERIVE = object()


class StaticSchedule:
    """What real facilities teams do to pre-empt complaints: overcool the whole
    building on a fixed schedule. This is the baseline we race against."""

    name = "Static 22degC schedule (baseline)"

    def act(self, twin, store=None):
        h, day = twin.hour, twin.day
        on = (7.0 <= h < 21.0) if day % 7 < 5 else (8.0 <= h < 18.0)
        sp = 22.0 if on else None
        return ({z.id: sp for z in ZONES},
                {z.id: (1 if on else 0) for z in ZONES})


class ReactiveComfort:
    """Per-zone thermostat at a fixed occupied setpoint. Better, still dumb:
    it reacts only to current occupancy, never to people or forecasts."""

    name = "Reactive thermostat 24degC"

    def act(self, twin, store=None):
        sps, vents = {}, {}
        for z in ZONES:
            occ = twin.occupancy_now(z.id)
            sps[z.id] = 24.0 if occ > 0 else None
            vents[z.id] = 1 if occ > 0 else 0
        return sps, vents


# ---------------------------------------------------------------------------
# Objective -> schedule
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlProfile:
    """The concrete schedule an objective's weights imply.

    INPUT: produced by control_profile(objective); never built by hand.
    OUTPUT: value object; degC for the three setpoints, hours for lead_h.
    SIDE EFFECTS: none (frozen).
    ERROR STATES: none.
    """
    objective: str
    comfort_w: float
    energy_w: float
    occupied: float        # degC while people are in the zone
    precool: float         # degC while people are expected within lead_h
    unoccupied: float      # degC setback when nobody is expected
    lead_h: float          # hours of look-ahead used to trigger pre-cool
    flush: bool            # ventilate during pre-cool / allow the crowd vent boost


def control_profile(objective: str) -> ControlProfile:
    """Derive a full control schedule from an objective's comfort/energy weights.

    The formula - no lookup table, every number below is computed:
      occupied   = SP_TIGHT + (SP_LOOSE - SP_TIGHT) * w_energy, rounded to 0.01 K
                   (real BMS setpoint resolution; the rounding also removes binary
                   float noise so "balanced" is bit-exactly the historical 25.0).
      precool    = occupied + PRECOOL_LIFT
      unoccupied = min(occupied + SETBACK_LIFT, SAFE_HI)
      lead_h     = LEAD_MAX_H * w_comfort, quantised to LEAD_QUANTUM_H
      flush      = w_comfort >= the balanced objective's comfort weight. Flushing a
                   zone with outdoor air before arrival, and boosting the fan for a
                   crowd, are comfort investments paid in fan power and outdoor-air
                   load; objectives that value energy more than "balanced" does
                   decline them and run the code-minimum fan instead.

    Resulting schedules:
      comfort  24.50 / 25.50 / 28.00, lead 0.75 h, flush
      balanced 25.00 / 26.00 / 28.50, lead 0.50 h, flush   <- the shipped numbers
      cost     25.70 / 26.70 / 29.00, lead 0.25 h
      carbon   25.70 / 26.70 / 29.00, lead 0.25 h
      energy   25.90 / 26.90 / 29.00, lead 0.25 h

    INPUT: an Objective string; unknown strings fall back to "balanced" weights
      (objective_weights() tolerates them so a UI typo cannot take control down).
    OUTPUT: a ControlProfile.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    w = objective_weights(objective)
    cw, ew = float(w["comfort"]), float(w["energy"])
    occupied = round(SP_TIGHT + (SP_LOOSE - SP_TIGHT) * ew, 2)
    lead = round(LEAD_MAX_H * cw / LEAD_QUANTUM_H) * LEAD_QUANTUM_H
    return ControlProfile(
        objective=objective,
        comfort_w=cw,
        energy_w=ew,
        occupied=occupied,
        precool=round(occupied + PRECOOL_LIFT, 2),
        unoccupied=round(min(occupied + SETBACK_LIFT, SAFE_HI), 2),
        lead_h=lead,
        flush=cw >= _BALANCED_CW,
    )


# ---------------------------------------------------------------------------
# The one and only safety choke point
# ---------------------------------------------------------------------------

def _enforce_envelope(target, offset: float, vent) -> tuple:
    """Clamp one zone's command into the hard safety envelope. Enforced LAST.

    Two independent limits, applied in this order:
      1. the adaptive/complaint offset is clamped to +-MAX_OFFSET (1.8 K) - this
         is what stops a pile-on of severe complaints from running away;
      2. the resulting setpoint is clamped to [SAFE_LO, SAFE_HI] (21.5-29.0 degC)
         and the fan to [VENT_LO, VENT_HI] (0-2).

    INPUT: target degC or None (None = HVAC off), offset degC, vent int-ish.
    OUTPUT: (setpoint: float | None, vent: int, bound: bool) where bound is True
      iff any limit actually changed the requested value - the caller turns that
      into reason_code "safety_clamp".
    SIDE EFFECTS: none.
    ERROR STATES: none - non-numeric vent raises from int(), which is a caller bug.
    """
    bound = False
    try:
        v = int(vent)
    except (TypeError, ValueError):
        v, bound = VENT_LO, True
    if v < VENT_LO:
        v, bound = VENT_LO, True
    elif v > VENT_HI:
        v, bound = VENT_HI, True

    if target is None:
        return None, v, bound

    off = float(offset)
    if off > MAX_OFFSET:
        off, bound = MAX_OFFSET, True
    elif off < -MAX_OFFSET:
        off, bound = -MAX_OFFSET, True

    sp = float(target) + off
    if sp < SAFE_LO:
        sp, bound = SAFE_LO, True
    elif sp > SAFE_HI:
        sp, bound = SAFE_HI, True
    return round(sp, 2), v, bound


def _counts(c) -> bool:
    """True when a constraint is allowed to move a setpoint (approved, not rejected).

    Mirrors Constraint.counts(); the getattr fallback keeps this module working
    against a store that predates the approval flags.
    """
    fn = getattr(c, "counts", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            pass
    return bool(getattr(c, "approved", True)) and not bool(getattr(c, "rejected", False))


def _aggregate(cs, now_t: float):
    """Arbitrate an ARBITRARY set of constraints into an offset + vent delta.

    ConstraintStore.zone_adjustments() already does this for the constraints that
    count (approved, not rejected) — that is the number the controller writes, and
    it is never recomputed here. This function exists for the one thing the store
    cannot answer: "what WOULD the setpoint be if the pending complaints were
    approved too?", which human_approval mode has to show an operator before they
    can approve anything. Formula kept deliberately in step with the store's:
        weight = severity * confidence * decay
        offset = sum(raw_offset * weight) / sum(weight)
        vent   = sum(vent_delta of items with weight > 0.25), clipped to [-1, 1]

    INPUT: list of Constraint-like objects, current sim seconds.
    OUTPUT: {"setpoint_offset": float, "vent_delta": int, "n": int} or None when
      the set is empty or fully decayed.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    if not cs:
        return None
    wsum = sum(c.weight(now_t) for c in cs)
    if wsum <= 0:
        return None
    offset = sum(c.raw_offset * c.weight(now_t) for c in cs) / wsum
    vent = sum(c.vent_delta for c in cs if c.weight(now_t) > 0.25)
    return {"setpoint_offset": round(offset, 2),
            "vent_delta": max(-1, min(1, vent)), "n": len(cs)}


def _snapshot(twin, zone_id: str) -> dict:
    """twin.zone_snapshot() with a graceful fallback for older/stub twins.

    INPUT: a DigitalTwin-like object and a zone id.
    OUTPUT: dict with at least occ, occ_pct, rh_pct, temp_c, at_capacity.
    SIDE EFFECTS: none.
    ERROR STATES: none - a twin without zone_snapshot degrades to occupancy only,
      which disables the crowd and humidity terms rather than crashing control.
    """
    try:
        return twin.zone_snapshot(zone_id)
    except Exception:
        occ = twin.occupancy_now(zone_id)
        return {"occ": occ, "occ_pct": 0.0, "rh_pct": 0.0,
                "temp_c": getattr(twin, "T", {}).get(zone_id, 25.0),
                "at_capacity": False}


class ConstraintAware:
    """FeelsLike rules controller: occupancy-aware setbacks + pre-cool + live
    complaint constraints, under a multi-objective schedule and a hard safety
    envelope. Also the RL fallback.

    INPUT (constructor): base_* override the derived schedule (leave unset to
      derive from the objective; base_unoccupied=None still means "HVAC off when
      empty"); objective in OBJECTIVES; safety_mode in SAFETY_MODES; locked_zones
      an iterable of zone ids that maintenance_lockout freezes.
    OUTPUT: .act(twin, store) -> (setpoints, vents), plus .last_decisions and
      .pending_recommendations refreshed on every act().
    SIDE EFFECTS: none on the twin or the store - act() only reads them.
    ERROR STATES: ValueError for an unknown objective, safety mode or zone id.
    """

    name = "FeelsLike (constraint-aware)"

    def __init__(self, base_occupied=_DERIVE, base_precool=_DERIVE,
                 base_unoccupied=_DERIVE, objective: str = "balanced",
                 safety_mode: str = "automatic", locked_zones=None):
        if objective not in OBJECTIVES:
            raise ValueError(f"unknown objective {objective!r}; expected one of {OBJECTIVES}")
        if safety_mode not in SAFETY_MODES:
            raise ValueError(f"unknown safety mode {safety_mode!r}; "
                             f"expected one of {tuple(SAFETY_MODES)}")
        self.objective = objective
        self.safety_mode = safety_mode
        self.locked_zones = set(locked_zones or ())
        self._overrides = (base_occupied, base_precool, base_unoccupied)
        self.last_decisions: list = []
        self.pending_recommendations: list = []
        self._refresh()

    # ---- configuration ---------------------------------------------------
    def _refresh(self) -> None:
        """Recompute the schedule from the objective, honouring constructor overrides."""
        self.profile = control_profile(self.objective)
        o_occ, o_pre, o_un = self._overrides
        self.base_occupied = self.profile.occupied if o_occ is _DERIVE else o_occ
        self.base_precool = self.profile.precool if o_pre is _DERIVE else o_pre
        self.base_unoccupied = self.profile.unoccupied if o_un is _DERIVE else o_un

    def set_objective(self, o: str) -> str:
        """Switch the optimisation objective and re-derive the whole schedule.

        INPUT: an Objective literal. OUTPUT: the objective now in force.
        SIDE EFFECTS: rewrites base_occupied/base_precool/base_unoccupied unless
          the constructor pinned them. ERROR STATES: ValueError if unknown.
        """
        if o not in OBJECTIVES:
            raise ValueError(f"unknown objective {o!r}; expected one of {OBJECTIVES}")
        self.objective = o
        self._refresh()
        return o

    def set_safety_mode(self, m: str) -> str:
        """Switch the safety mode.

        INPUT: a SafetyMode literal. OUTPUT: the mode now in force.
        SIDE EFFECTS: changes what the next act() is allowed to write.
        ERROR STATES: ValueError if unknown.
        """
        if m not in SAFETY_MODES:
            raise ValueError(f"unknown safety mode {m!r}; expected one of {tuple(SAFETY_MODES)}")
        self.safety_mode = m
        return m

    def lock_zone(self, z: str) -> set:
        """Add a zone to the maintenance lockout list (takes effect in that mode).

        INPUT: a zone id. OUTPUT: the lockout set.
        SIDE EFFECTS: mutates self.locked_zones. ERROR STATES: ValueError on an
          unknown zone id - silently locking a typo would be invisible in the UI.
        """
        if z not in ZONE_BY_ID:
            raise ValueError(f"unknown zone {z!r}")
        self.locked_zones.add(z)
        return self.locked_zones

    def unlock_zone(self, z: str) -> set:
        """Remove a zone from the lockout list.

        INPUT: a zone id (unknown/absent ids are a no-op, unlocking must never fail).
        OUTPUT: the lockout set. SIDE EFFECTS: mutates self.locked_zones.
        ERROR STATES: none.
        """
        self.locked_zones.discard(z)
        return self.locked_zones

    # ---- adaptive offset terms ------------------------------------------
    def _density_offset(self, occ_pct: float) -> float:
        """Extra cooling for a zone carrying MORE people than it was sized for.

        occ_pct is 100% at the zone's weekday design headcount (sim.twin
        _OCC_PEAK, surfaced through zone_snapshot). Below that the coil was sized
        for the load and the schedule already covers it, so this term is exactly
        0.0 - which is why the nominal A/B numbers are untouched by it. Above it
        every extra body adds ~100 W sensible (sim.twin.step) that the schedule
        never budgeted for, and the zone drifts up before the thermostat can
        react; we pre-empt that in proportion to the overload, scaled by the
        objective's comfort weight and capped at DENSITY_MAX.

        Live whenever occupancy exceeds design: the what-if occupancy scenarios
        (twin.occ_scale) and the cafeteria lunch surge under any occ_scale > 1.

        INPUT: occ_pct 0..inf. OUTPUT: <= 0.0 K. SIDE EFFECTS: none. ERRORS: none.
        """
        over = max(0.0, float(occ_pct) / 100.0 - 1.0)
        if over <= 0.0:
            return 0.0
        return -round(min(DENSITY_MAX, DENSITY_GAIN * self.profile.comfort_w * over), 3)

    def _humidity_offset(self, rh_pct: float) -> float:
        """Extra sensible cooling to answer a confirmed latent (muggy) complaint.

        Physics: a DX coil only dehumidifies while it is removing sensible heat.
        With a sensible heat ratio SHR (sim.humidity.SHR = 0.75) the coil's latent
        capacity is q_sensible * (1 - SHR) / SHR - i.e. 1 W of moisture removal
        per 3 W of sensible cooling. So the ONLY control lever for "it's sticky in
        here" is to lower the setpoint: more coil runtime, more condensate. The
        complaint's own ISSUE_EFFECTS offset (-0.4..-0.6 K) is small precisely
        because it is not sensor-confirmed; this adds a bounded top-up once the
        zone's measured RH agrees with the occupant.

        GATING (deliberate, and the reason the A/B numbers are unchanged): this
        term needs an active humid/stuffy constraint, not just RH > 65. The twin
        documents that its apparatus-dew-point approximation pins indoor RH near
        88% for most occupied minutes (see the module docstring in sim/twin.py),
        so chasing the RH sensor unconditionally would spend real energy on a
        modelling artefact. An occupant signal is required to confirm it.

        INPUT: zone RH in %. OUTPUT: <= 0.0 K, magnitude <= HUMID_MAX.
        SIDE EFFECTS: none. ERROR STATES: none.
        """
        if rh_pct is None or rh_pct <= RH_HUMID:
            return 0.0
        frac = min(1.0, (float(rh_pct) - RH_HUMID) / HUMID_SPAN)
        return -round(HUMID_MAX * frac * self.profile.comfort_w, 3)

    # ---- constraint plumbing --------------------------------------------
    def _active_by_zone(self, store, now_t: float) -> dict:
        """Group the store's live constraints by zone. Never raises."""
        if store is None:
            return {}
        try:
            out: dict = {}
            for c in store.active(now_t):
                out.setdefault(c.zone, []).append(c)
            return out
        except Exception:
            return {}

    # ---- decision logging ------------------------------------------------
    def _decision(self, twin, store, zone_id, base_sp, new_sp, base_vent,
                  new_vent, applied, reason):
        """Build one ControllerDecision for the audit log. NEVER raises.

        backend.decisions is imported lazily inside this method on purpose: a
        half-written module must not be able to break `import sim.controllers`
        for demo_day, rl/* or sim/env. If it is missing we fall back to filling
        backend.contracts.ControllerDecision directly, and if even that fails the
        caller gets None and control carries on. Logging is never load-bearing.

        INPUT: twin, store (may be None), zone id, the schedule setpoint, the
          setpoint actually written, the two fan levels, applied flag, reason code.
        OUTPUT: a ControllerDecision, or None if nothing could be built.
        SIDE EFFECTS: none. ERROR STATES: none by construction.
        """
        d = None
        try:
            from backend.decisions import build_decision
        except Exception:
            build_decision = None
        try:
            if build_decision is not None:
                d = build_decision(twin, store, zone_id, base_sp, new_sp, base_vent,
                                   new_vent, self.objective, self.safety_mode, applied)
            else:
                d = self._fallback_decision(twin, zone_id, base_sp, new_sp,
                                            base_vent, new_vent, applied)
        except Exception:
            return None
        try:
            if d is not None and reason:
                # Precedence: build_decision knows things this module does not
                # (it detects an arbitrated conflict), so its finer-grained
                # "conflict_compromise" beats our generic "complaint_offset".
                # Everything else is zone- and mode-level knowledge that only the
                # controller has - notably that in maintenance_lockout mode the
                # UNLOCKED zones are still running normally - so ours wins.
                if not (reason == "complaint_offset"
                        and getattr(d, "reason_code", "") == "conflict_compromise"):
                    d.reason_code = reason
                d.applied = bool(applied)
        except Exception:
            pass
        return d

    def _fallback_decision(self, twin, zone_id, base_sp, new_sp, base_vent,
                           new_vent, applied):
        """Minimal ControllerDecision when backend.decisions is not on disk yet.

        Fills only what this module can know first-hand (zone, clock, setpoints,
        occupancy, temperatures). The richer fields build_decision owns -
        constraint views, conflict/arbitration, estimated deltas - stay at their
        dataclass defaults rather than being guessed at.

        INPUT/OUTPUT/ERRORS: as _decision; returns None if contracts is unavailable.
        """
        try:
            from backend.contracts import ControllerDecision
        except Exception:
            return None
        z = ZONE_BY_ID.get(zone_id)
        snap = _snapshot(twin, zone_id)
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        h = twin.hour
        clock = f"{days[twin.day % 7]} {int(h):02d}:{int((h % 1) * 60):02d}"
        off = 0.0
        if new_sp is not None and base_sp is not None:
            off = round(float(new_sp) - float(base_sp), 2)
        return ControllerDecision(
            t=twin.t, sim_clock=clock, zone=zone_id,
            zone_name=z.name if z else zone_id, applied=bool(applied),
            prev_setpoint=getattr(twin, "last_setpoints", {}).get(zone_id),
            new_setpoint=new_sp, base_setpoint=base_sp, offset_c=off,
            prev_vent=int(getattr(twin, "last_vents", {}).get(zone_id, 0)),
            new_vent=int(new_vent), occupancy=float(snap.get("occ", 0)),
            occupancy_pct=float(snap.get("occ_pct", 0.0)),
            outdoor_c=round(float(twin.weather_fn(twin.t)) + getattr(twin, "outdoor_offset", 0.0), 2),
            indoor_c=float(snap.get("temp_c", 0.0)), rh_pct=float(snap.get("rh_pct", 0.0)),
            objective=self.objective, safety_mode=self.safety_mode,
            summary=f"{z.name if z else zone_id}: setpoint {new_sp} degC "
                    f"(schedule {base_sp}), fan {int(new_vent)}.",
        )

    # ---- the control law -------------------------------------------------
    def act(self, twin, store=None):
        """One control step for all five zones. SIGNATURE FROZEN.

        INPUT: a DigitalTwin and an optional ConstraintStore.
        OUTPUT: (setpoints: zone_id -> degC|None, vents: zone_id -> 0|1|2).
        SIDE EFFECTS: refreshes self.last_decisions and self.pending_recommendations.
          Neither the twin nor the store is mutated.
        ERROR STATES: none from the control path; decision logging swallows its own.
        """
        p = self.profile
        now = twin.t
        groups = self._active_by_zone(store, now)
        try:
            canonical = store.zone_adjustments(now) if store is not None else {}
        except Exception:
            canonical = {}

        emergency = self.safety_mode == "emergency_override"
        recommend = self.safety_mode == "recommend_only"
        approval = self.safety_mode == "human_approval"
        lockout = self.safety_mode == "maintenance_lockout"

        sps, vents, decisions, pending = {}, {}, [], []
        for z in ZONES:
            snap = _snapshot(twin, z.id)
            occ = snap.get("occ", 0)
            occ_soon = twin.occupancy_now(z.id, ahead_h=p.lead_h)
            cs = groups.get(z.id, [])
            locked = lockout and z.id in self.locked_zones

            # --- layer 1: the base schedule -------------------------------
            if occ > 0:
                base_sp, reason = self.base_occupied, "no_change"
                # Crowd-aware fan: per-person outdoor air, but only worth the fan
                # power when the zone is both absolutely busy and near design load.
                boost = (p.flush and occ >= VENT_BOOST_HEADCOUNT
                         and snap.get("occ_pct", 0.0) >= VENT_BOOST_DENSITY_PCT)
                base_vent = 2 if boost else 1
            elif occ_soon > 0:
                base_sp, reason = self.base_precool, "precool"
                base_vent = 1 if p.flush else 0
            else:
                base_sp, base_vent, reason = self.base_unoccupied, 0, "occupancy_setback"

            # --- layer 2: the adaptive offset -----------------------------
            # The store's zone_adjustments() already weights ONLY the constraints
            # that count (approved and not rejected), in every mode - so that is
            # the offset we are allowed to write, and it is never recomputed here.
            counting = [c for c in cs if _counts(c)]
            pending_cs = [c for c in cs if not _counts(c)]
            suppress = locked or emergency

            adj_w = None if suppress else canonical.get(z.id)
            off_write = adj_w["setpoint_offset"] if adj_w else 0.0
            vent_write = base_vent + (adj_w["vent_delta"] if adj_w else 0)

            # What the setpoint WOULD be if the pending complaints were approved.
            # Identical to the written offset unless human_approval has queued
            # something, which is the only case worth spending _aggregate on.
            if approval and pending_cs and not suppress:
                adj_p = _aggregate(cs, now)
                off_want = adj_p["setpoint_offset"] if adj_p else 0.0
                vent_want = base_vent + (adj_p["vent_delta"] if adj_p else 0)
                want_cs = cs
            else:
                off_want, vent_want, want_cs = off_write, vent_write, counting

            if occ > 0 and not suppress:
                dens = self._density_offset(snap.get("occ_pct", 0.0))
                off_write += dens
                off_want += dens
                rh = snap.get("rh_pct", 0.0)
                if any(c.issue in ("humid", "stuffy") for c in counting):
                    off_write += self._humidity_offset(rh)
                if any(c.issue in ("humid", "stuffy") for c in want_cs):
                    off_want += self._humidity_offset(rh)

            # --- layer 3: what the mode allows -----------------------------
            if emergency:
                # Complaints are ignored as a control INPUT, but an active
                # complaint still proves somebody is in there, which the schedule
                # does not know - so it counts as presence for the safe band.
                present = occ > 0 or bool(cs)
                target = EMERGENCY_SP if present else None
                vent_write = vent_want = 1 if present else 0
                off_write = off_want = 0.0
                reason = "emergency_override"
            elif locked:
                target, vent_write, vent_want = base_sp, base_vent, base_vent
                off_write = off_want = 0.0
                reason = "locked_out"
            else:
                target = base_sp

            desired_sp, desired_vent, _ = _enforce_envelope(target, off_want, vent_want)

            # --- layer 4: what actually gets written ----------------------
            if recommend:
                write_sp, write_vent, bound = _enforce_envelope(base_sp, 0.0, base_vent)
                applied, reason = False, "recommend_only"
            else:
                write_sp, write_vent, bound = _enforce_envelope(target, off_write, vent_write)
                applied = True
                # Precedence: a bound safety clamp is always the headline reason
                # (requirement: "if a clamp bound, the reason_code is safety_clamp"),
                # then a withheld approval, then an applied complaint offset.
                if off_write != 0.0 and not suppress and write_sp is not None:
                    # write_sp is None when the schedule has the compressor off:
                    # there is no setpoint for an offset to move, so the honest
                    # reason stays "occupancy_setback", not "complaint_offset".
                    reason = "complaint_offset"
                if approval and pending_cs and desired_sp != write_sp and not suppress:
                    # The approved part (if any) was written, but an operator is
                    # still holding back a further move: same code build_decision
                    # uses for "the mode blocked this write".
                    reason = "recommend_only"
                if bound:
                    reason = "safety_clamp"

            sps[z.id] = write_sp
            vents[z.id] = write_vent

            # --- audit record: only for material events -------------------
            material = (write_sp != base_sp or write_vent != base_vent
                        or desired_sp != write_sp or bool(cs) or bound)
            if material:
                d = self._decision(twin, store, z.id, base_sp, write_sp,
                                   base_vent, write_vent, applied, reason)
                if d is not None:
                    decisions.append(d)
                if (not applied or pending_cs) and desired_sp != write_sp:
                    pending.append({
                        "zone": z.id, "zone_name": z.name, "t": now,
                        "reason_code": reason, "objective": self.objective,
                        "safety_mode": self.safety_mode,
                        "current_setpoint": write_sp,
                        "recommended_setpoint": desired_sp,
                        "recommended_vent": desired_vent,
                        "delta_c": (round(desired_sp - write_sp, 2)
                                    if (desired_sp is not None and write_sp is not None) else 0.0),
                        "active_constraints": len(cs),
                        "unapproved_constraints": len(pending_cs),
                        "summary": getattr(d, "summary", "") if d is not None else "",
                    })

        self.last_decisions = decisions
        # Snapshot of what an operator is being asked to approve RIGHT NOW.
        # Refreshed (not accumulated) each act(): at 1 Hz an accumulating queue
        # would grow without bound, and a stale recommendation is worse than none.
        self.pending_recommendations = pending
        return sps, vents


class RLPolicy:
    """Wraps a trained stable-baselines3 model. Falls back loudly if missing."""

    name = "FeelsLike (PPO agent)"

    def __init__(self, model_path: str = "rl/models/ppo_feelslike.zip"):
        from stable_baselines3 import PPO  # heavy import, only when used
        self.model = PPO.load(model_path)
        self.fallback = ConstraintAware()

    def act(self, twin, store=None):
        from sim.env import build_obs, apply_action
        import numpy as np
        obs = build_obs(twin, store)
        action, _ = self.model.predict(np.array(obs, dtype="float32"), deterministic=True)
        return apply_action(twin, store, action)

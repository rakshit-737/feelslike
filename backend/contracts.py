"""Canonical shared types for FeelsLike — the one module every other module
imports its vocabulary from, so parallel work cannot drift.

Why this exists: the twin, the parser, the constraint store, the controller, the
what-if engine, the maintenance monitor and the dashboard all talk about the same
five nouns (a zone, an issue, a constraint, a decision, a scenario). Before this
module each of them spelled those nouns slightly differently. Everything here is
plain stdlib dataclasses + Literal aliases: NO imports from sim/* or backend/*,
so this module is importable standalone and can never create a cycle.

Rules for anyone extending this file:
- Add fields, never rename or remove them (the dashboard reads some verbatim).
- Every field carries a unit in its comment; degC and W and kWh, never "value".
- Everything must survive to_dict() -> json.dumps() with no custom encoder.
- Ids come from new_id() with a deterministic counter, never uuid4, so a test
  that replays the same inputs gets the same ids.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Iterator, Literal

# --------------------------------------------------------------------------
# Literal vocabularies. The tuples exist for runtime validation (Literal is a
# typing-only construct and vanishes at runtime).
# --------------------------------------------------------------------------

Issue = Literal["too_hot", "too_cold", "stuffy", "humid", "drafty", "other"]
ISSUES: tuple = ("too_hot", "too_cold", "stuffy", "humid", "drafty", "other")

Objective = Literal["comfort", "energy", "cost", "carbon", "balanced"]
OBJECTIVES: tuple = ("comfort", "energy", "cost", "carbon", "balanced")

SafetyMode = Literal["automatic", "recommend_only", "human_approval",
                     "emergency_override", "maintenance_lockout"]

Language = Literal["en", "hinglish", "mixed"]      # ParsedComplaint.language
LANGUAGES: tuple = ("en", "hinglish", "mixed")

AlertKind = Literal["capacity", "sensor", "actuator", "recurring"]
ALERT_KINDS: tuple = ("capacity", "sensor", "actuator", "recurring")

AlertSeverity = Literal["low", "medium", "high"]
ALERT_SEVERITIES: tuple = ("low", "medium", "high")

ResultKind = Literal["measured", "predicted"]
RESULT_KINDS: tuple = ("measured", "predicted")

# How the multi-objective controller trades comfort against energy. Weights sum
# to 1.0 and are the ONLY place that trade-off is written down.
# cost and carbon share the comfort/energy split of an energy-leaning objective
# because the sim's tariff (TARIFF Rs/kWh) and grid factor (GRID_CO2 kg/kWh) are
# both flat scalars of kWh: minimising rupees and minimising CO2 are the same
# control problem here. They differ only in how the impact is REPORTED (rupees
# vs kilograms). If a time-of-use tariff or a carbon-intensity curve is ever
# added, these two rows are what must diverge — nothing else.
OBJECTIVE_WEIGHTS: dict = {
    "balanced": {"comfort": 0.70, "energy": 0.30},
    "comfort":  {"comfort": 0.95, "energy": 0.05},
    "energy":   {"comfort": 0.25, "energy": 0.75},
    "cost":     {"comfort": 0.35, "energy": 0.65},
    "carbon":   {"comfort": 0.35, "energy": 0.65},
}

# What each safety mode means for a controller that is about to write a setpoint.
SAFETY_MODES: dict = {
    "automatic": "Controller applies its decision to the twin immediately. Default.",
    "recommend_only": "Controller computes the decision and logs it with applied=False; "
                      "the twin keeps the previous setpoint. Nothing is written.",
    "human_approval": "Decision is queued in pending_recommendations and only applied "
                      "after an operator approves it; unapproved decisions log applied=False.",
    "emergency_override": "Occupant constraints are ignored; the controller drives every "
                          "zone to the safe band regardless of complaints.",
    "maintenance_lockout": "Named zones are frozen at their base schedule setpoint; "
                           "constraints for those zones are recorded but never applied.",
}

# Stable machine-readable reason vocabulary for ControllerDecision.reason_code.
# UI copy may change; these strings may not.
REASON_CODES: dict = {
    "occupied_base": "Zone occupied, no active constraint: base occupied setpoint.",
    "precool": "Occupancy expected within 30 min: pre-cool setpoint.",
    "setback": "Zone unoccupied and none expected soon: setback / HVAC off.",
    "constraint_applied": "Active complaint constraint shifted the setpoint.",
    "conflict_compromise": "Opposing constraints arbitrated to a weighted compromise.",
    "clamped": "Requested offset hit the 21.5-29.0 degC safety clamp.",
    "at_capacity": "Cooling demand exceeds the zone's max_cool; setpoint unreachable.",
    "locked": "Zone is under maintenance lockout; constraint recorded, not applied.",
    "recommend_only": "Safety mode blocked the write; decision logged as a recommendation.",
    "awaiting_approval": "Decision queued for human approval; not applied.",
    "emergency": "Emergency override: constraints ignored, safe band enforced.",
}

# The controller/decision engine (sim.controllers -> backend.decisions) emits a
# parallel nine-code spelling for the same states. This is the ONE bridge table:
# backend.decisions.CONTRACT_REASON_ALIAS is an alias of this object, so the two
# vocabularies cannot drift apart. Consumers must not compare reason_code to a
# REASON_CODES key directly -- run it through canonical_reason() first.
REASON_ALIASES: dict = {
    "no_change": "occupied_base",
    "occupancy_setback": "setback",
    "precool": "precool",
    "complaint_offset": "constraint_applied",
    "conflict_compromise": "conflict_compromise",
    "safety_clamp": "clamped",
    "recommend_only": "recommend_only",
    "locked_out": "locked",
    "emergency_override": "emergency",
}


def canonical_reason(code: str) -> str:
    """Resolve any emitted reason_code to its canonical REASON_CODES key.

    INPUT: code, a reason_code string from either vocabulary (canonical or the
      decisions/controllers spelling). Non-str input is coerced with str().
    OUTPUT: a key of REASON_CODES when the code is recognised, otherwise the
      input unchanged, so an unknown code stays visible instead of being hidden
      behind a wrong bucket.
    SIDE EFFECTS: none.
    ERROR STATES: none; never raises and never returns None.
    """
    c = code if isinstance(code, str) else str(code)
    if c in REASON_CODES:
        return c
    return REASON_ALIASES.get(c, c)


# How a zone's active constraints were combined (ControllerDecision.arbitration).
ARBITRATION_MODES: tuple = ("none", "single", "weighted_mean", "conflict_weighted_mean",
                            "ignored_emergency", "ignored_locked")


# --------------------------------------------------------------------------
# Dataclasses. Every field has a default so partial construction is legal and
# adding a field later cannot break a positional call site.
# --------------------------------------------------------------------------

@dataclass
class ConstraintView:
    """Read-only projection of one backend.constraints.Constraint at a moment in time.

    INPUT: built by whoever holds a Constraint plus the current sim time.
    OUTPUT: json-safe snapshot; the store's live decay maths is already applied.
    SIDE EFFECTS: none — this is a value object, mutating it changes no control.
    ERROR STATES: none; a caller that passes junk gets junk back verbatim.
    """
    id: int = 0                     # Constraint.id (monotonic counter)
    issue: str = "other"            # Issue literal
    severity: int = 1               # 1 mild .. 3 urgent
    confidence: float = 0.0         # 0..1, from the parser
    weight: float = 0.0             # severity * confidence * decay, at this instant
    age_min: float = 0.0            # sim-minutes since the complaint was filed
    expires_in_min: float = 0.0     # sim-minutes until decay hits EXPIRY_S (0 = expired)
    raw_offset: float = 0.0         # degC the issue asks the setpoint to move
    author: str = "anonymous"       # occupant handle, "comfort-memory", or anonymised token
    text: str = ""                  # original complaint text (may be PII-scrubbed)


@dataclass
class ControllerDecision:
    """One controller act() outcome for ONE zone: what it did, and why.

    This is the audit record behind the dashboard's explainability panel. One
    act() over 5 zones produces 5 of these.

    INPUT: assembled by backend.decisions.build_decision from twin + store state.
    OUTPUT: json-safe dict via to_dict(); fed to DecisionLog and /api/state.
    SIDE EFFECTS: none (the caller applies setpoints, this only describes them).
    ERROR STATES: none; unknown zones simply produce zone_name == zone.
    """
    t: float = 0.0                       # sim-seconds since Monday 00:00
    sim_clock: str = ""                  # "Wed 14:35" (human clock, same as /api/state)
    zone: str = ""                       # zone_id, e.g. "zone_b"
    zone_name: str = ""                  # "Conference Room B"
    applied: bool = True                 # False = recommendation only, twin untouched
    prev_setpoint: float | None = None   # degC before this act(); None = HVAC was off
    new_setpoint: float | None = None    # degC written (or proposed); None = off
    base_setpoint: float | None = None   # degC the schedule alone would have chosen
    offset_c: float = 0.0                # new_setpoint - base_setpoint, degC
    prev_vent: int = 0                   # 0|1|2 fan level before
    new_vent: int = 0                    # 0|1|2 fan level after
    occupancy: float = 0.0               # people in the zone right now
    occupancy_pct: float = 0.0           # 0..100, occupancy as % of that zone's peak
    outdoor_c: float = 0.0               # degC dry-bulb outdoors
    indoor_c: float = 0.0                # degC zone air temperature
    rh_pct: float = 0.0                  # % relative humidity in the zone
    constraints: list = field(default_factory=list)   # list[ConstraintView]
    conflict: bool = False               # True when opposing constraints were arbitrated
    arbitration: str = "none"            # one of ARBITRATION_MODES
    objective: str = "balanced"          # Objective literal in force
    safety_mode: str = "automatic"       # SafetyMode literal in force
    est_energy_delta_pct: float = 0.0    # % vs base schedule; + = more energy
    est_comfort_delta_pct: float = 0.0   # % vs base schedule; + = more comfortable
    summary: str = ""                    # one human sentence, safe to show a judge
    reason_code: str = "occupied_base"   # key into REASON_CODES
    id: str = ""                         # new_id("dec", n); empty until logged


@dataclass
class MaintenanceAlert:
    """A suspected equipment fault inferred from twin behaviour over time.

    INPUT: raised by backend.maintenance.MaintenanceMonitor.tick().
    OUTPUT: json-safe dict; surfaced in /api/state and the ops panel.
    SIDE EFFECTS: none; alerts never change control on their own.
    ERROR STATES: none; an alert with empty evidence is legal but useless.
    """
    id: str = ""                         # new_id("mnt", n)
    kind: str = "capacity"               # AlertKind literal
    zone: str = ""                       # zone_id
    zone_name: str = ""                  # display name
    severity: str = "low"                # AlertSeverity literal
    confidence: float = 0.0              # 0..1, how sure the heuristic is
    evidence: list = field(default_factory=list)   # list[str], observed facts w/ numbers
    recommendation: str = ""             # what a technician should do
    first_seen_t: float = 0.0            # sim-seconds when the symptom first appeared
    last_seen_t: float = 0.0             # sim-seconds of the most recent observation


@dataclass
class ScenarioSpec:
    """A what-if question: "what happens if X, for H hours, over these seeds?"

    INPUT: built from backend.whatif.SCENARIOS[key] plus user-chosen params.
    OUTPUT: consumed by run_scenario(); never mutated by it.
    SIDE EFFECTS: none.
    ERROR STATES: run_scenario raises KeyError for a params key the scenario
      does not know, and ValueError for horizon_h <= 0 or an empty seed list.
    """
    name: str = ""                       # human label, e.g. "Heat wave +4 degC"
    kind: str = ""                       # scenario family key, e.g. "heatwave"
    params: dict = field(default_factory=dict)     # knob -> value, scenario-specific
    horizon_h: float = 8.0               # simulated hours to run
    seeds: list = field(default_factory=list)      # list[int]; [] means "use the live seed"


@dataclass
class ScenarioResult:
    """The measured outcome of running a ScenarioSpec on cloned twins.

    kind distinguishes an honest simulation ("measured": we actually stepped the
    clone) from an analytic estimate ("predicted": closed-form, no stepping).
    Never label an estimate as measured — the whole pitch rests on that line.

    INPUT: produced by backend.whatif.run_scenario().
    OUTPUT: json-safe; mean/sd/ci95 share the key set of metrics.
    SIDE EFFECTS: none.
    ERROR STATES: none once constructed; per_seed == [] means a single-run scenario.
    """
    name: str = ""
    kind: str = "measured"               # ResultKind literal
    metrics: dict = field(default_factory=dict)    # metric -> value (twin.metrics() shape)
    per_seed: list = field(default_factory=list)   # list[dict], one metrics dict per seed
    mean: dict = field(default_factory=dict)       # metric -> mean across seeds
    sd: dict = field(default_factory=dict)         # metric -> sample stdev (0.0 if n<2)
    ci95: dict = field(default_factory=dict)       # metric -> half-width of 95% CI


@dataclass
class ZoneRuntime:
    """Everything the UI needs about one zone at one instant.

    The first nine fields are byte-identical in name to the zone dict that
    /api/state already returns (dashboard/index.html reads them verbatim), so
    to_dict(ZoneRuntime) is a drop-in superset of today's payload. Additive
    fields only, forever.

    INPUT: built from DigitalTwin state + ConstraintStore.zone_adjustments().
    OUTPUT: json-safe dict for the /api/state "zones" array.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    id: str = ""                         # zone_id
    name: str = ""                       # display name
    temp: float = 0.0                    # degC, FeelsLike twin
    base_temp: float = 0.0               # degC, baseline twin (the A/B partner)
    setpoint: float | None = None        # degC or None when HVAC is off
    vent: int = 0                        # 0|1|2 fan level
    occ: int = 0                         # people, rounded
    offset: float = 0.0                  # degC applied by active constraints
    active_constraints: int = 0          # count of non-decayed constraints
    # --- additive, safe to ignore by older clients -------------------------
    occ_pct: float = 0.0                 # 0..100 of the zone's peak occupancy
    rh_pct: float = 0.0                  # % relative humidity
    dew_point_c: float = 0.0             # degC dew point
    capacity_w: float = 0.0              # W thermal cooling capacity of the unit
    at_capacity: bool = False            # True when the unit is saturated this step
    conflict: bool = False               # opposing constraints active in this zone
    locked: bool = False                 # maintenance lockout in force


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def to_dict(obj: Any) -> Any:
    """Recursively convert any contract object into a json.dumps-safe structure.

    INPUT: any dataclass defined here, pydantic model, dict, list/tuple/set,
      scalar, or arbitrary object.
    OUTPUT: dict / list / str / int / float / bool / None only. Non-finite floats
      (nan, inf) become None, because json.dumps emits invalid JSON for them.
      Sets are sorted by repr so output is deterministic. Unknown objects fall
      back to str(obj) rather than raising.
    SIDE EFFECTS: none; the input is never mutated.
    ERROR STATES: none by design — this is the last line of defence before the
      wire, so it degrades instead of raising.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if hasattr(obj, "model_dump"):                 # pydantic v2 (ParsedComplaint)
        return to_dict(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [to_dict(v) for v in sorted(obj, key=repr)]
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return str(obj)


def new_id(prefix: str, counter: int | Iterator[int]) -> str:
    """Deterministic id: "<prefix>-0001". Never uuid4, so replays reproduce.

    INPUT: prefix (short tag, e.g. "dec", "mnt", "cmp") and either an int or an
      iterator of ints (itertools.count(1) is the intended use).
    OUTPUT: str "<prefix>-<n zero-padded to 4>"; longer n is not truncated.
    SIDE EFFECTS: advances the iterator when one is passed.
    ERROR STATES: TypeError if counter is neither an int nor an iterator;
      StopIteration if an exhausted iterator is passed.
    """
    if isinstance(counter, int):
        n = counter
    elif hasattr(counter, "__next__"):
        n = next(counter)
    else:
        raise TypeError(f"counter must be int or iterator, got {type(counter).__name__}")
    return f"{prefix}-{n:04d}"


def id_counter(start: int = 1) -> Iterator[int]:
    """Fresh deterministic counter for new_id().

    INPUT: start (first value handed out).
    OUTPUT: an itertools.count iterator.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    return itertools.count(start)


def objective_weights(objective: str) -> dict:
    """Comfort/energy weights for an objective, defaulting to balanced.

    INPUT: an Objective string (unknown strings are tolerated).
    OUTPUT: {"comfort": float, "energy": float} summing to 1.0. A copy, so the
      caller cannot corrupt OBJECTIVE_WEIGHTS.
    SIDE EFFECTS: none.
    ERROR STATES: none — an unrecognised objective silently yields "balanced",
      because a typo in a UI dropdown must never take the controller down.
    """
    return dict(OBJECTIVE_WEIGHTS.get(objective, OBJECTIVE_WEIGHTS["balanced"]))

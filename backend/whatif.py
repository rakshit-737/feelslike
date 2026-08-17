"""What-if engine: counterfactual scenarios run on THROWAWAY CLONES.

Why this exists: a judge (or an operator) asks "what would have happened if we
had ignored that complaint / if it were 3 degC hotter / if the chiller were at
70% capacity?". Answering honestly means re-running the same building forward
under one changed condition and measuring the difference — not guessing.

THE ONE NON-NEGOTIABLE: the live twin and the live constraint store are NEVER
mutated. Every run deep-copies both, perturbs the copies, steps the copies, and
throws them away. state_fingerprint() + verify_isolation() prove it rather than
asserting it in prose.

Honesty rules baked in:
- ScenarioResult.kind is "measured" only because we really did step the physics
  for the whole horizon. Nothing here extrapolates, so nothing is ever labelled
  "predicted". If someone adds a closed-form estimator later, it must set
  kind="predicted".
- Metrics are DELTAS accumulated over the horizon, not the clone's lifetime
  totals. A clone inherits the live twin's kWh counter; reporting that would
  smuggle history into the answer.
- The 95% CI is mean +/- 1.96*sd/sqrt(n): a NORMAL approximation, not a t
  distribution. With the seed counts a demo uses (n = 1..5) that is indicative
  only, and n < 3 gets no CI at all. See CI95_NOTE.

Performance: control runs every physics step (60 s). Measured on this repo,
3 seeds x 6 simulated hours (1080 steps) takes ~0.04 s, and a full compare()
(baseline + scenario) ~0.09 s, so no coarse control interval is needed. The
knob is CONTROL_INTERVAL_S if that ever changes.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import statistics

from backend.constraints import EXPIRY_S, ConstraintStore
from backend.contracts import ScenarioResult, ScenarioSpec, objective_weights, to_dict
from sim.controllers import ConstraintAware
from sim.humidity import rh_from_ratio
from sim.twin import DT, GRID_CO2, TARIFF, ZONES, DigitalTwin
from sim.weather import outdoor_temp

DEFAULT_SEEDS: list = [7]
DEFAULT_HORIZON_H: float = 6.0
CONTROL_INTERVAL_S: float = DT      # controller runs every physics step

CI95_NOTE = ("95% CI is mean +/- 1.96*sd/sqrt(n), a normal approximation. "
             "With n < 3 seeds it is indicative only and is reported as 0.0.")

# Setpoint shift per unit of extra energy weight, used ONLY by the fallback in
# _make_controller() when sim/controllers.py has no native objective support.
# 3.0 degC per unit weight puts "energy" (w_energy .75) at +1.35 degC and
# "comfort" (.05) at -0.75 degC relative to balanced (.30) — a real, defensible
# spread, not a placeholder.
OBJECTIVE_SETPOINT_K = 3.0
_BALANCED_ENERGY_W = objective_weights("balanced")["energy"]

# Every perturbation a ScenarioSpec.params may carry. Scale knobs are applied
# RELATIVE to the live twin (occ_scale 1.2 means "20% more people than we have
# now"); offset knobs are ADDITIVE. That way the unperturbed baseline is exactly
# the live building, whatever knobs it already has set.
PARAM_KEYS = frozenset({
    "occ_scale",          # multiplier on twin.occ_scale
    "capacity_scale",     # multiplier on twin.capacity_scale
    "solar_scale",        # multiplier on twin.solar_scale
    "outdoor_offset",     # degC added to twin.outdoor_offset
    "humidity_offset",    # %RH added to twin.humidity_offset
    "setpoint_delta_c",   # degC added to the controller's OCCUPIED setpoint
    "objective",          # Objective literal for the controller
    "drop_constraints",   # bool: run with an empty constraint store
    "age_constraints_s",  # seconds to back-date every constraint by
})

# key -> {label, kind, params, help}. kind is the scenario FAMILY (weather,
# occupancy, ...); ScenarioResult.kind is the separate measured/predicted flag.
SCENARIOS: dict = {
    "setpoint_up": {
        "label": "Setpoint +1 degC",
        "kind": "control",
        "params": {"setpoint_delta_c": 1.0},
        "help": "Let every occupied zone run one degree warmer than the schedule asks.",
    },
    "setpoint_down": {
        "label": "Setpoint -1 degC",
        "kind": "control",
        "params": {"setpoint_delta_c": -1.0},
        "help": "Cool every occupied zone one degree harder than the schedule asks.",
    },
    "occupancy_up": {
        "label": "Occupancy +20%",
        "kind": "occupancy",
        "params": {"occ_scale": 1.2},
        "help": "A busier day: 20% more people in every zone, so more body heat and moisture.",
    },
    "occupancy_down": {
        "label": "Occupancy -20%",
        "kind": "occupancy",
        "params": {"occ_scale": 0.8},
        "help": "A quieter day: 20% fewer people in every zone.",
    },
    "outdoor_hotter": {
        "label": "Heat wave +3 degC",
        "kind": "weather",
        "params": {"outdoor_offset": 3.0},
        "help": "Every outdoor temperature reading is three degrees hotter all day.",
    },
    "humidity_up": {
        "label": "Monsoon +15 %RH",
        "kind": "weather",
        "params": {"humidity_offset": 15.0},
        "help": "Outdoor air carries 15 percentage points more relative humidity.",
    },
    "capacity_loss": {
        "label": "Cooling capacity 70%",
        "kind": "equipment",
        "params": {"capacity_scale": 0.7},
        "help": "The plant can only deliver 70% of its rated cooling, as if filters were fouled.",
    },
    "ignore_complaint": {
        "label": "Ignore all complaints",
        "kind": "constraint",
        "params": {"drop_constraints": True},
        "help": "Run the same building with the complaint queue emptied, to price what listening costs.",
    },
    "complaint_expires": {
        "label": "Complaints already expired",
        "kind": "constraint",
        "params": {"age_constraints_s": EXPIRY_S + 60.0},
        "help": "Back-date every complaint past its two-hour expiry, so its influence has already faded.",
    },
    "objective_energy": {
        "label": "Objective: save energy",
        "kind": "objective",
        "params": {"objective": "energy"},
        "help": "Same building, but the controller is told to prioritise kilowatt-hours over comfort.",
    },
    "objective_comfort": {
        "label": "Objective: maximise comfort",
        "kind": "objective",
        "params": {"objective": "comfort"},
        "help": "Same building, but the controller is told comfort beats energy almost every time.",
    },
}

# metric -> (verb, unit, direction). direction: "lower" better, "neutral" = a
# reading, not a score. Used to write the plain-language verdict sentences.
METRIC_META: dict = {
    "kwh":             ("uses", "kWh", "lower"),
    "cost_rs":         ("costs", "Rs", "lower"),
    "co2_kg":          ("emits", "kgCO2", "lower"),
    "viol_min":        ("spends", "occupied-min out of band", "lower"),
    "hot_deg_min":     ("accumulates", "degC-min too hot", "lower"),
    "cold_deg_min":    ("accumulates", "degC-min too cold", "lower"),
    "humid_viol_min":  ("spends", "occupied-min above 65 %RH", "lower"),
    "at_capacity_min": ("spends", "occupied-min at full cooling capacity", "lower"),
    "mean_temp":       ("runs", "degC mean zone temperature", "neutral"),
    "mean_rh":         ("runs", "%RH mean zone humidity", "neutral"),
    "interventions":   ("makes", "setpoint changes", "neutral"),
}
METRIC_KEYS: tuple = tuple(METRIC_META)


# --------------------------------------------------------------------------
# Spec / clone plumbing
# --------------------------------------------------------------------------

def scenario_spec(key: str, horizon_h: float = DEFAULT_HORIZON_H,
                  seeds: list | None = None) -> ScenarioSpec:
    """Build a ScenarioSpec from a SCENARIOS key.

    INPUT: key (must be in SCENARIOS), horizon_h > 0, seeds (list[int] or None
      -> DEFAULT_SEEDS).
    OUTPUT: a fresh ScenarioSpec; params is a copy, so mutating it cannot
      corrupt the registry.
    SIDE EFFECTS: none.
    ERROR STATES: KeyError for an unknown scenario key.
    """
    if key not in SCENARIOS:
        raise KeyError(f"unknown scenario {key!r}; known: {sorted(SCENARIOS)}")
    e = SCENARIOS[key]
    return ScenarioSpec(name=e["label"], kind=e["kind"], params=dict(e["params"]),
                        horizon_h=float(horizon_h),
                        seeds=list(seeds if seeds is not None else DEFAULT_SEEDS))


def _as_spec(spec, seeds=None, horizon_h=None) -> ScenarioSpec:
    """Coerce a ScenarioSpec, a SCENARIOS entry dict, or a SCENARIOS key to a spec.

    Accepting the raw registry dict matters: callers naturally write
    compare(twin, store, SCENARIOS["occupancy_up"]).

    INPUT: ScenarioSpec | dict with a "params" key | str key; optional seeds and
      horizon_h overrides.
    OUTPUT: a NEW ScenarioSpec (never the caller's object) with validated fields.
    SIDE EFFECTS: none.
    ERROR STATES: KeyError for an unknown params key or scenario key;
      ValueError for horizon_h <= 0 or an empty seed list; TypeError for
      anything that is not a spec, dict or str.
    """
    if isinstance(spec, str):
        out = scenario_spec(spec)
    elif isinstance(spec, ScenarioSpec):
        out = ScenarioSpec(name=spec.name, kind=spec.kind, params=dict(spec.params),
                           horizon_h=float(spec.horizon_h), seeds=list(spec.seeds))
    elif isinstance(spec, dict):
        # A registry entry carries no horizon; an explicit 0 must still be an
        # error, so absence is tested with `is None`, not truthiness.
        hz = spec.get("horizon_h")
        out = ScenarioSpec(name=spec.get("label") or spec.get("name") or "scenario",
                           kind=spec.get("kind", ""),
                           params=dict(spec.get("params", {})),
                           horizon_h=DEFAULT_HORIZON_H if hz is None else float(hz),
                           seeds=list(spec.get("seeds") or []))
    else:
        raise TypeError(f"spec must be ScenarioSpec, dict or str, got {type(spec).__name__}")

    if horizon_h is not None:
        out.horizon_h = float(horizon_h)
    if seeds is not None:
        out.seeds = list(seeds)
    if not out.seeds:
        out.seeds = list(DEFAULT_SEEDS)
    if out.horizon_h <= 0:
        raise ValueError(f"horizon_h must be > 0, got {out.horizon_h}")
    unknown = set(out.params) - PARAM_KEYS
    if unknown:
        raise KeyError(f"unknown scenario params {sorted(unknown)}; known: {sorted(PARAM_KEYS)}")
    return out


def _clone_store(store) -> ConstraintStore:
    """Independent copy of a ConstraintStore (uses its own clone() when it has one).

    INPUT: a ConstraintStore or None.
    OUTPUT: a new ConstraintStore; None yields an empty one. Constraint objects
      are deep-copied, so back-dating created_t on the copy is safe.
    SIDE EFFECTS: none on the input.
    ERROR STATES: none.
    """
    if store is None:
        return ConstraintStore()
    clone = getattr(store, "clone", None)
    if callable(clone):
        return clone()
    c = ConstraintStore()
    c.items = [copy.deepcopy(i) for i in store.items]
    if hasattr(store, "objective"):          # forward-compat with the pinned API
        try:
            c.objective = store.objective
        except Exception:
            pass
    return c


def _seeded_clone(twin, seed) -> DigitalTwin:
    """Clone the twin and, when the seed differs, install that seed's weather.

    A seed equal to the live twin's is left alone, so a single-seed run keeps a
    custom weather_fn (e.g. a fetched Open-Meteo trace) instead of silently
    swapping in synthetic weather. A different seed re-seeds both dry-bulb
    (weather.outdoor_temp) and humidity (twin uses self.seed for outdoor_rh).

    INPUT: a DigitalTwin, an int seed (or None to keep the twin's).
    OUTPUT: an independent DigitalTwin at the same sim time and state.
    SIDE EFFECTS: none on the input twin.
    ERROR STATES: none.
    """
    c = twin.clone()
    if seed is not None and int(seed) != twin.seed:
        s = int(seed)
        c.seed = s
        c.weather_fn = lambda t, _s=s: outdoor_temp(t, _s)
    return c


def _objective_setpoint_delta(objective: str) -> float:
    """Fallback mapping from an objective to an occupied-setpoint shift, degC.

    Used only while sim/controllers.py has no native objective argument. It is a
    REAL perturbation (it moves setpoints, which moves kWh and comfort), derived
    from contracts.OBJECTIVE_WEIGHTS so the two stay consistent: leaning further
    towards energy than "balanced" raises the setpoint proportionally.

    INPUT: an Objective string (unknown strings fall back to balanced -> 0.0).
    OUTPUT: degC to add to base_occupied.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    w = objective_weights(objective)["energy"]
    return OBJECTIVE_SETPOINT_K * (w - _BALANCED_ENERGY_W)


def _default_occupied_setpoint() -> float:
    """The occupied setpoint an unperturbed ConstraintAware actually uses, degC.

    Read from a default-constructed instance, not the signature: ConstraintAware
    defaults its base_* arguments to a sentinel object and resolves the real
    number in __init__, so the signature carries no usable value.

    INPUT: none.
    OUTPUT: float degC; 25.0 if the attribute is missing or non-numeric, which
      is the value that class has shipped since the first commit.
    SIDE EFFECTS: none (constructs and discards one controller).
    ERROR STATES: none — any failure degrades to the 25.0 fallback.
    """
    try:
        v = getattr(ConstraintAware(), "base_occupied", None)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 25.0
    except Exception:
        return 25.0


def _make_controller(params: dict):
    """Build the ConstraintAware controller this scenario needs.

    setpoint_delta_c shifts base_occupied only (the spec's "occupied setpoint");
    pre-cool and setback stay on the schedule's own values. The reference value
    is read off a DEFAULT-CONSTRUCTED ConstraintAware rather than its signature,
    because that class uses sentinel objects for "unset" defaults — an instance
    is the only place the resolved number actually exists. So the perturbation
    keeps meaning "+1 degC relative to whatever the schedule currently is".

    When ConstraintAware exposes a native `objective=` argument it is used
    directly; the _objective_setpoint_delta fallback only runs if it does not.

    INPUT: a validated params dict.
    OUTPUT: (controller, note:str) where note describes what was actually done.
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    sig = inspect.signature(ConstraintAware.__init__).parameters
    delta = float(params.get("setpoint_delta_c", 0.0) or 0.0)
    objective = params.get("objective")
    kwargs: dict = {}
    notes = []
    if delta:
        notes.append(f"occupied setpoint {delta:+.2f} degC")
    if objective:
        if "objective" in sig:
            kwargs["objective"] = objective
            notes.append(f"controller objective={objective} (native)")
        else:
            d = _objective_setpoint_delta(objective)
            delta += d
            notes.append(f"objective={objective} -> occupied setpoint {d:+.2f} degC "
                         f"(fallback: controller has no native objective arg)")
    if delta:
        kwargs["base_occupied"] = _default_occupied_setpoint() + delta
    return ConstraintAware(**kwargs), "; ".join(notes) or "unperturbed controller"


def _apply_perturbation(twin, store, params: dict) -> str:
    """Apply the twin/store side of a perturbation to CLONES, in place.

    INPUT: a cloned twin, a cloned store, a validated params dict.
    OUTPUT: a human-readable note of what changed.
    SIDE EFFECTS: mutates the two clones it is handed — never pass live objects.
    ERROR STATES: TypeError if a knob value is not numeric.
    """
    notes = []
    cond: dict = {}
    for k, cur in (("occ_scale", twin.occ_scale),
                   ("capacity_scale", twin.capacity_scale),
                   ("solar_scale", twin.solar_scale)):
        if k in params:
            cond[k] = cur * float(params[k])
            notes.append(f"{k} x{float(params[k]):.2f} -> {cond[k]:.2f}")
    for k, cur in (("outdoor_offset", twin.outdoor_offset),
                   ("humidity_offset", twin.humidity_offset)):
        if k in params:
            cond[k] = cur + float(params[k])
            notes.append(f"{k} {float(params[k]):+.2f} -> {cond[k]:+.2f}")
    if cond:
        twin.set_conditions(**cond)          # clamps to its documented ranges

    if params.get("drop_constraints"):
        n = len(store.items)
        store.items = []
        notes.append(f"dropped {n} constraint(s)")
    aged = params.get("age_constraints_s")
    if aged:
        n = 0
        for c in store.items:
            if c.decay(twin.t) > 0.0:
                c.created_t -= float(aged)
                n += 1
        notes.append(f"back-dated {n} constraint(s) by {float(aged) / 60.0:.0f} min")
    return "; ".join(notes) or "no condition change"


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def _count_changes(sps: dict, prev: dict) -> int:
    """Interventions on one control tick: zones whose WRITTEN setpoint changed.

    Deliberately observation-based rather than read off ConstraintAware
    .last_decisions. Two reasons: (1) that log is filtered to "material events"
    (a zone with no active constraint and no deviation from base emits nothing),
    so it undercounts ordinary schedule transitions, and consulting it only on
    the ticks where it happens to be non-empty would silently switch definitions
    mid-run; (2) what lands on the twin is already the applied setpoint — in
    recommend_only mode the controller writes the base schedule value, so an
    unapplied recommendation correctly registers as no intervention. This gives
    one stable definition that holds for any controller with the frozen .act()
    signature.

    INPUT: this tick's setpoints, previous tick's setpoints (None = HVAC off is
      a real state and differs from any number).
    OUTPUT: int 0..len(ZONES).
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    return sum(1 for z in ZONES if sps.get(z.id) != prev.get(z.id))


def _run_horizon(twin, store, controller, horizon_h: float) -> dict:
    """Step one clone forward and return the metrics ACCUMULATED over the horizon.

    Energy/comfort counters are reported as deltas (after - before), because a
    clone inherits the live twin's lifetime totals. mean_temp / mean_rh are
    sampled across ALL five zones after every step, occupied or not — that is a
    different (and deliberately simpler) definition from twin.metrics()["mean_rh"],
    which averages only occupied zone-minutes.

    INPUT: cloned twin, cloned store, a controller, horizon in simulated hours.
    OUTPUT: dict over METRIC_KEYS, all floats except interventions (int).
    SIDE EFFECTS: steps the clone to the end of the horizon.
    ERROR STATES: none.
    """
    steps = max(1, int(round(horizon_h * 3600.0 / DT)))
    every = max(1, int(round(CONTROL_INTERVAL_S / DT)))
    b_kwh, b_viol = twin.kwh, twin.viol_min
    b_hot, b_cold = twin.hot_deg_min, twin.cold_deg_min
    b_humid, b_cap = twin.humid_viol_min, twin.at_capacity_min

    prev_sps = dict(twin.last_setpoints)
    # A fresh twin has never applied a setpoint; counting tick 1 against {} would
    # score five phantom interventions, so the first tick only seeds prev_sps.
    seeded = bool(prev_sps)
    interventions = 0
    t_sum = rh_sum = 0.0
    samples = 0
    sps: dict = {}
    vents: dict = {}

    for i in range(steps):
        if i % every == 0:
            sps, vents = controller.act(twin, store)
            if seeded:
                interventions += _count_changes(sps, prev_sps)
            seeded = True
            prev_sps = dict(sps)
        twin.step(sps, vents)
        for z in ZONES:
            t_sum += twin.T[z.id]
            rh_sum += rh_from_ratio(twin.T[z.id], twin.W[z.id])
            samples += 1

    d_kwh = twin.kwh - b_kwh
    return {
        "kwh": round(d_kwh, 4),
        "cost_rs": round(d_kwh * TARIFF, 3),
        "co2_kg": round(d_kwh * GRID_CO2, 4),
        "viol_min": round(twin.viol_min - b_viol, 3),
        "hot_deg_min": round(twin.hot_deg_min - b_hot, 3),
        "cold_deg_min": round(twin.cold_deg_min - b_cold, 3),
        "humid_viol_min": round(twin.humid_viol_min - b_humid, 3),
        "at_capacity_min": round(twin.at_capacity_min - b_cap, 3),
        "mean_temp": round(t_sum / samples, 3) if samples else 0.0,
        "mean_rh": round(rh_sum / samples, 3) if samples else 0.0,
        "interventions": interventions,
    }


def _aggregate(per_seed: list) -> tuple:
    """mean / sample-sd / 95% CI half-width across per-seed metric dicts.

    CI is the normal approximation 1.96*sd/sqrt(n) (see CI95_NOTE). With n < 3
    the CI is reported as 0.0 rather than a misleadingly precise number, and sd
    is 0.0 when n < 2 because a sample stdev of one point is undefined.

    INPUT: list of metric dicts sharing METRIC_KEYS.
    OUTPUT: (mean, sd, ci95), three dicts over METRIC_KEYS.
    SIDE EFFECTS: none.
    ERROR STATES: returns three empty dicts for an empty input.
    """
    if not per_seed:
        return {}, {}, {}
    n = len(per_seed)
    mean, sd, ci = {}, {}, {}
    for k in METRIC_KEYS:
        vals = [float(r[k]) for r in per_seed]
        mean[k] = round(statistics.fmean(vals), 4)
        s = statistics.stdev(vals) if n > 1 else 0.0
        sd[k] = round(s, 4)
        ci[k] = round(1.96 * s / math.sqrt(n), 4) if n >= 3 else 0.0
    return mean, sd, ci


def run_scenario(twin, store, spec, seeds=None) -> ScenarioResult:
    """Run one what-if scenario on clones and measure it.

    For each seed: clone the twin (installing that seed's weather when it differs
    from the live seed), clone the store, apply the perturbation to the CLONES,
    build the scenario's controller, and step the physics forward spec.horizon_h
    simulated hours at the twin's native 60 s step.

    INPUT: the LIVE twin and store (read only), a ScenarioSpec / SCENARIOS entry
      dict / SCENARIOS key, and an optional seed list overriding spec.seeds.
    OUTPUT: ScenarioResult with kind="measured" (the horizon really was
      simulated), metrics = the across-seed mean, plus per_seed / mean / sd /
      ci95. Each per_seed entry carries its "seed" and a "note" describing the
      perturbation that was actually applied.
    SIDE EFFECTS: NONE on the inputs. Clones only.
    ERROR STATES: KeyError (unknown scenario key or unknown params key),
      ValueError (horizon_h <= 0), TypeError (spec of the wrong type).
    """
    sp = _as_spec(spec, seeds=seeds)
    per_seed = []
    for seed in sp.seeds:
        tc = _seeded_clone(twin, seed)
        sc = _clone_store(store)
        note = _apply_perturbation(tc, sc, sp.params)
        ctrl, cnote = _make_controller(sp.params)
        if "objective" in sp.params and hasattr(sc, "set_objective"):
            sc.set_objective(sp.params["objective"])     # forward-compat, no-op today
        m = _run_horizon(tc, sc, ctrl, sp.horizon_h)
        m["seed"] = int(seed)
        m["note"] = f"{note} | {cnote}"
        per_seed.append(m)

    mean, sd, ci = _aggregate(per_seed)
    return ScenarioResult(name=sp.name, kind="measured", metrics=dict(mean),
                          per_seed=per_seed, mean=mean, sd=sd, ci95=ci)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def _verdict(metric: str, base: float, scen: float) -> tuple:
    """One plain-language sentence for a metric, plus its absolute/percent delta.

    INPUT: metric key (must be in METRIC_META), baseline and scenario values.
    OUTPUT: (abs_delta, pct_or_None, sentence). pct is None when the baseline is
      zero, and the sentence says so instead of printing an infinite percentage.
    SIDE EFFECTS: none.
    ERROR STATES: KeyError for an unknown metric key.
    """
    verb, unit, _ = METRIC_META[metric]
    d = scen - base
    pct = (100.0 * d / abs(base)) if base else None
    if abs(d) < 5e-4:
        return 0.0, (0.0 if base else None), f"{verb} the same {unit}"
    word = "more" if d > 0 else "less"
    tail = f"({pct:+.1f}%)" if pct is not None else "(baseline was zero, percent n/a)"
    # Keep sub-0.01 deltas readable instead of rendering them as "0.00 more".
    mag = f"{abs(d):.3f}" if abs(d) < 0.01 else f"{abs(d):.2f}"
    return round(d, 4), (round(pct, 2) if pct is not None else None), \
        f"{verb} {mag} {unit} {word} {tail}"


def compare(twin, store, spec) -> dict:
    """Baseline vs scenario over the SAME horizon, seeds and starting state.

    The baseline is the identical clone with an empty params dict, so the only
    difference between the two columns is the perturbation — apples to apples.

    INPUT: the LIVE twin and store (read only) and a ScenarioSpec / SCENARIOS
      entry dict / SCENARIOS key.
    OUTPUT: {"baseline": ScenarioResult, "scenario": ScenarioResult,
             "delta": {metric: {baseline, scenario, abs, pct, verdict}},
             "spec": dict, "headline": str, "ci95_note": str}.
      pct is None where the baseline metric was exactly zero.
    SIDE EFFECTS: NONE on the inputs.
    ERROR STATES: same as run_scenario.
    """
    sp = _as_spec(spec)
    base_spec = ScenarioSpec(name="Baseline (no change)", kind="baseline", params={},
                             horizon_h=sp.horizon_h, seeds=list(sp.seeds))
    base = run_scenario(twin, store, base_spec)
    scen = run_scenario(twin, store, sp)

    delta = {}
    for k in METRIC_KEYS:
        b, s = float(base.mean[k]), float(scen.mean[k])
        d, pct, sentence = _verdict(k, b, s)
        delta[k] = {"baseline": round(b, 4), "scenario": round(s, 4),
                    "abs": d, "pct": pct, "verdict": sentence}
    headline = (f"{sp.name} over {sp.horizon_h:g} h x {len(sp.seeds)} seed(s): "
                f"{delta['kwh']['verdict']}, {delta['viol_min']['verdict']}.")
    return {"baseline": base, "scenario": scen, "delta": delta,
            "spec": to_dict(sp), "headline": headline, "ci95_note": CI95_NOTE}


# --------------------------------------------------------------------------
# Isolation proof
# --------------------------------------------------------------------------

def state_fingerprint(twin, store=None) -> str:
    """SHA-256 of everything mutable in a twin (+ store), for isolation checks.

    Covers the clock, per-zone temperature and humidity ratio, every energy and
    comfort counter, the five condition knobs, the last applied setpoints/vents,
    the private RH accumulators and at-capacity flags, and each constraint's
    identity/timing fields. Floats are rounded to 9 dp so the hash is stable
    against nothing at all — any real step changes it.

    INPUT: a DigitalTwin and an optional ConstraintStore.
    OUTPUT: 64-char hex digest.
    SIDE EFFECTS: none.
    ERROR STATES: none; missing attributes are simply absent from the digest.
    """
    def r(v):
        return round(float(v), 9)

    state = {
        "seed": twin.seed, "t": r(twin.t),
        "T": {k: r(v) for k, v in sorted(twin.T.items())},
        "W": {k: r(v) for k, v in sorted(getattr(twin, "W", {}).items())},
        "kwh": r(twin.kwh),
        "kwh_by_zone": {k: r(v) for k, v in sorted(twin.kwh_by_zone.items())},
        "viol_min": r(twin.viol_min), "hot": r(twin.hot_deg_min),
        "cold": r(twin.cold_deg_min), "power": r(twin.last_power_w),
        "knobs": [r(getattr(twin, k, 0.0)) for k in
                  ("occ_scale", "capacity_scale", "solar_scale",
                   "outdoor_offset", "humidity_offset")],
        "humid_viol_min": r(getattr(twin, "humid_viol_min", 0.0)),
        "at_capacity_min": r(getattr(twin, "at_capacity_min", 0.0)),
        "rh_sum": r(getattr(twin, "_rh_sum", 0.0)),
        "rh_min": r(getattr(twin, "_rh_min", 0.0)),
        "at_cap": sorted((k, bool(v)) for k, v in getattr(twin, "_at_cap", {}).items()),
        "last_sps": sorted((k, None if v is None else r(v))
                           for k, v in getattr(twin, "last_setpoints", {}).items()),
        "last_vents": sorted((k, int(v)) for k, v in getattr(twin, "last_vents", {}).items()),
    }
    if store is not None:
        state["store"] = [
            [c.id, c.zone, c.issue, c.severity, r(c.confidence), r(c.created_t),
             r(c.raw_offset), c.vent_delta, c.author, c.text]
            for c in store.items
        ]
        if hasattr(store, "objective"):
            state["objective"] = str(store.objective)
    blob = json.dumps(state, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def verify_isolation(twin, store=None) -> bool:
    """Prove that running scenarios leaves the live twin and store untouched.

    Fingerprints the live state, then runs the two most dangerous scenario
    families against it — the constraint mutators (drop / back-date, which touch
    the store) and a condition knob plus an extra seed (which swaps weather_fn)
    — through the full compare() path, then re-fingerprints. Short horizon
    (0.5 h), so it is cheap enough to call from a test or a health check.

    INPUT: the LIVE twin and store.
    OUTPUT: True when the fingerprint is byte-identical afterwards.
    SIDE EFFECTS: none if the engine is correct; a False return means some code
      path leaked a reference and the caller must treat results as suspect.
    ERROR STATES: none — exceptions from a scenario propagate to the caller.
    """
    before = state_fingerprint(twin, store)
    seeds = [twin.seed, twin.seed + 1]
    for key in ("ignore_complaint", "complaint_expires", "outdoor_hotter"):
        compare(twin, store, scenario_spec(key, horizon_h=0.5, seeds=seeds))
    return state_fingerprint(twin, store) == before

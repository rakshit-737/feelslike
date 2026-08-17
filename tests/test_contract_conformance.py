"""Contract conformance: the cross-module boundary FeelsLike is assembled on.

These tests assert the *pinned* API surface — the names, signatures, shapes and
invariants that separate modules agreed on — rather than any module's internal
behavior. Their job is to fail loudly when one subsystem drifts away from the
contract the others were written against. Behavioral depth lives in the
per-subsystem test modules; this file is the seam.

Run: .venv/Scripts/python -m pytest tests/test_contract_conformance.py -q
"""
from __future__ import annotations

import inspect
import json

import pytest

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def has(obj, *names):
    """Every name must exist on obj; returns the list of missing ones."""
    return [n for n in names if not hasattr(obj, n)]


def params(fn):
    return list(inspect.signature(fn).parameters)


def jsonable(x):
    """True when x survives a round-trip through JSON (wire-safety check)."""
    try:
        json.dumps(x)
        return True
    except (TypeError, ValueError):
        return False


@pytest.fixture
def twin():
    from sim.twin import DigitalTwin
    t = DigitalTwin(seed=7)
    t.t = 10 * 3600.0          # mid-morning: zones occupied, HVAC active
    return t


@pytest.fixture
def store():
    from backend.constraints import ConstraintStore
    return ConstraintStore()


# --------------------------------------------------------------------------
# 1 · canonical types
# --------------------------------------------------------------------------

def test_contracts_module_exports_canonical_types():
    import backend.contracts as c
    missing = has(c, "ControllerDecision", "ConstraintView", "MaintenanceAlert",
                  "ScenarioSpec", "ScenarioResult", "OBJECTIVE_WEIGHTS",
                  "SAFETY_MODES", "to_dict")
    assert not missing, f"backend.contracts is missing pinned exports: {missing}"


def test_objective_weights_cover_every_objective_and_sum_to_one():
    from backend.contracts import OBJECTIVE_WEIGHTS
    for name in ("comfort", "energy", "cost", "carbon", "balanced"):
        assert name in OBJECTIVE_WEIGHTS, f"objective {name!r} has no weights"
        w = OBJECTIVE_WEIGHTS[name]
        total = w["comfort"] + w["energy"]
        assert abs(total - 1.0) < 1e-6, f"{name} weights sum to {total}, not 1.0"
    assert OBJECTIVE_WEIGHTS["balanced"]["comfort"] == pytest.approx(0.70, abs=0.01)


def test_safety_modes_are_the_five_pinned_modes():
    from backend.contracts import SAFETY_MODES
    expected = {"automatic", "recommend_only", "human_approval",
                "emergency_override", "maintenance_lockout"}
    assert expected.issubset(set(SAFETY_MODES)), \
        f"missing safety modes: {expected - set(SAFETY_MODES)}"


def test_canonical_types_are_wire_safe():
    """Anything crossing the HTTP boundary must serialize without custom encoders."""
    import backend.contracts as c
    alert = c.MaintenanceAlert(
        id="alert-1", kind="capacity", zone="zone_e", zone_name="Cafeteria E",
        severity="high", confidence=0.8, evidence=["held 27.8 C for 52 min"],
        recommendation="Inspect the AHU", first_seen_t=0.0, last_seen_t=60.0)
    assert jsonable(c.to_dict(alert)), "MaintenanceAlert is not JSON-serializable via to_dict"


# --------------------------------------------------------------------------
# 2 · psychrometrics
# --------------------------------------------------------------------------

def test_humidity_module_exports_pinned_functions():
    import sim.humidity as h
    missing = has(h, "sat_pressure", "humidity_ratio", "rh_from_ratio",
                  "dew_point", "outdoor_rh")
    assert not missing, f"sim.humidity missing: {missing}"


def test_humidity_ratio_round_trips_through_rh():
    """W = f(T, RH) and RH = f_inv(T, W) must be mutual inverses."""
    from sim.humidity import humidity_ratio, rh_from_ratio
    for temp in (18.0, 24.0, 31.0):
        for rh in (25.0, 55.0, 85.0):
            w = humidity_ratio(temp, rh)
            assert rh_from_ratio(temp, w) == pytest.approx(rh, abs=0.5), \
                f"round-trip failed at {temp} C / {rh}% RH"


def test_dew_point_is_bounded_by_dry_bulb_and_rises_with_humidity():
    from sim.humidity import dew_point
    assert dew_point(30.0, 50.0) < 30.0, "dew point cannot exceed dry-bulb temperature"
    assert dew_point(30.0, 90.0) > dew_point(30.0, 40.0), "dew point must rise with RH"
    assert dew_point(30.0, 100.0) == pytest.approx(30.0, abs=0.3), \
        "at 100% RH dew point equals dry-bulb"


def test_outdoor_rh_is_seeded_and_in_range():
    from sim.humidity import outdoor_rh
    a = [outdoor_rh(t * 3600.0, seed=4) for t in range(48)]
    b = [outdoor_rh(t * 3600.0, seed=4) for t in range(48)]
    assert a == b, "outdoor_rh is not deterministic for a fixed seed"
    assert all(0.0 <= v <= 100.0 for v in a), "outdoor RH left the 0-100% range"


# --------------------------------------------------------------------------
# 3 · digital twin
# --------------------------------------------------------------------------

def test_twin_exposes_pinned_additions(twin):
    missing = has(twin, "clone", "rh_now", "dew_point_now", "zone_snapshot",
                  "set_conditions", "occ_scale", "capacity_scale", "solar_scale",
                  "outdoor_offset", "last_setpoints", "last_vents")
    assert not missing, f"DigitalTwin missing pinned additions: {missing}"


def test_twin_legacy_surface_survived(twin):
    """Every attribute existing callers (app, demo_day, rl, env) rely on."""
    missing = has(twin, "t", "T", "kwh", "kwh_by_zone", "viol_min", "hot_deg_min",
                  "cold_deg_min", "last_power_w", "day", "hour",
                  "occupancy_now", "step", "metrics")
    assert not missing, f"DigitalTwin lost legacy surface: {missing}"
    assert params(twin.step)[:3] == ["setpoints", "vents", "dt"], \
        "DigitalTwin.step signature changed; rl/env/app call it positionally"


def test_metrics_keeps_legacy_keys_and_adds_humidity_keys(twin):
    for _ in range(120):
        twin.step({z: 24.0 for z in twin.T}, {z: 1 for z in twin.T})
    m = twin.metrics()
    legacy = {"kwh", "cost_rs", "co2_kg", "viol_min", "hot_deg_min", "cold_deg_min"}
    assert legacy.issubset(m), f"metrics() dropped legacy keys: {legacy - set(m)}"
    added = {"humid_viol_min", "mean_rh", "at_capacity_min"}
    assert added.issubset(m), f"metrics() missing new keys: {added - set(m)}"
    assert jsonable(m), "metrics() is not JSON-serializable"


def test_clone_is_fully_independent(twin):
    """What-if safety rests entirely on this: mutating a clone cannot touch the original."""
    for _ in range(60):
        twin.step({z: 25.0 for z in twin.T}, {z: 1 for z in twin.T})
    before_kwh, before_t = twin.kwh, twin.t
    before_temps = dict(twin.T)

    clone = twin.clone()
    for _ in range(240):
        clone.step({z: 21.5 for z in clone.T}, {z: 2 for z in clone.T})

    assert twin.kwh == before_kwh, "clone leaked energy into the original"
    assert twin.t == before_t, "clone advanced the original's clock"
    assert twin.T == before_temps, "clone mutated the original's temperatures"
    assert clone.kwh > before_kwh, "clone did not actually run"


def test_same_seed_is_bit_identical(twin):
    from sim.twin import DigitalTwin
    a, b = DigitalTwin(seed=11), DigitalTwin(seed=11)
    for _ in range(300):
        sp = {z: 24.0 for z in a.T}
        a.step(sp, {z: 1 for z in a.T})
        b.step(sp, {z: 1 for z in b.T})
    assert a.kwh == b.kwh, "identical seeds diverged on energy"
    assert a.T == b.T, "identical seeds diverged on temperature"
    assert [a.rh_now(z) for z in a.T] == [b.rh_now(z) for z in b.T], \
        "identical seeds diverged on humidity"


def test_condition_knobs_actually_change_the_simulation(twin):
    """A knob that does not move the physics is fake functionality."""
    from sim.twin import DigitalTwin

    def run(**conditions):
        t = DigitalTwin(seed=7)
        t.t = 10 * 3600.0
        if conditions:
            t.set_conditions(**conditions)
        for _ in range(360):
            t.step({z: 24.0 for z in t.T}, {z: 1 for z in t.T})
        return t.kwh

    base = run()
    assert run(outdoor_offset=6.0) > base, "outdoor_offset did not raise cooling load"
    assert run(occ_scale=2.0) > base, "occ_scale did not raise internal gains"
    assert run(capacity_scale=0.3) < base, "capacity_scale did not limit cooling output"


def test_set_conditions_clamps_out_of_range_input(twin):
    out = twin.set_conditions(occ_scale=99.0, capacity_scale=-5.0, outdoor_offset=500.0)
    assert isinstance(out, dict), "set_conditions must return the resulting knob dict"
    assert 0.0 <= twin.occ_scale <= 3.0, "occ_scale escaped its documented range"
    assert 0.1 <= twin.capacity_scale <= 1.5, "capacity_scale escaped its documented range"
    assert -10.0 <= twin.outdoor_offset <= 10.0, "outdoor_offset escaped its documented range"


def test_zone_snapshot_shape(twin):
    snap = twin.zone_snapshot("zone_b")
    expected = {"temp_c", "setpoint_c", "vent", "occ", "occ_pct", "rh_pct",
                "dew_point_c", "capacity_w", "at_capacity"}
    assert expected.issubset(snap), f"zone_snapshot missing: {expected - set(snap)}"
    assert jsonable(snap), "zone_snapshot is not JSON-serializable"


# --------------------------------------------------------------------------
# 4 · parser
# --------------------------------------------------------------------------

def test_parsed_complaint_carries_both_zone_shapes():
    from backend.parser import parse
    p, _src, _ms = parse("The lobby and cafeteria are too hot", force_rules=True)
    assert hasattr(p, "zone_ids"), "ParsedComplaint lost the multi-zone field"
    assert hasattr(p, "zone_id"), "ParsedComplaint dropped the back-compat scalar zone_id"
    assert p.zone_id == (p.zone_ids[0] if p.zone_ids else None), \
        "zone_id and zone_ids[0] disagree; downstream callers will split"


def test_multi_zone_complaint_resolves_both_zones():
    from backend.parser import parse
    p, _s, _m = parse("The lobby and cafeteria are too hot", force_rules=True)
    assert set(p.zone_ids) == {"zone_d", "zone_e"}, \
        f"expected lobby+cafeteria, got {p.zone_ids}"
    assert p.issue == "too_hot"
    assert p.is_comfort_complaint is True


def test_parser_never_invents_a_zone():
    """The anti-hallucination guardrail: unknown zones are nulled, never guessed."""
    from backend.parser import parse
    from sim.twin import ZONE_IDS
    for text in ("the server room in basement 3 is boiling",
                 "zone_q is freezing", "it's hot in narnia"):
        p, _s, _m = parse(text, force_rules=True)
        assert all(z in ZONE_IDS for z in p.zone_ids), \
            f"parser emitted an unknown zone for {text!r}: {p.zone_ids}"


def test_non_thermal_message_is_ignored():
    from backend.parser import parse
    p, _s, _m = parse("the projector in room b is broken", force_rules=True)
    assert p.is_comfort_complaint is False, "equipment fault misread as a comfort complaint"


def test_retraction_is_not_a_complaint():
    from backend.parser import detect_retraction, parse
    assert detect_retraction("the heat issue in the lobby is fixed now") is True
    p, _s, _m = parse("room b is fine now, thanks", force_rules=True)
    assert p.is_comfort_complaint is False, "retraction filed as a fresh complaint"


def test_confidence_is_computed_not_constant():
    from backend.parser import parse
    scores = set()
    for text in ("It's really stuffy in Conference Room B",
                 "maybe kind of warm somewhere?",
                 "CONFERENCE ROOM B IS UNBEARABLE, I'm dying"):
        p, _s, _m = parse(text, force_rules=True)
        assert 0.0 <= p.confidence <= 1.0, "confidence left the 0-1 range"
        scores.add(round(p.confidence, 3))
    assert len(scores) > 1, "confidence is a constant; it carries no information"


# --------------------------------------------------------------------------
# 5 · constraints + explainability
# --------------------------------------------------------------------------

def test_store_exposes_pinned_additions(store):
    missing = has(store, "clone", "add_many", "set_objective", "constraint_views",
                  "pending_approvals", "approve", "reject", "by_complaint", "stats")
    assert not missing, f"ConstraintStore missing pinned additions: {missing}"


def test_store_legacy_surface_survived(store):
    missing = has(store, "items", "add", "active", "zone_adjustments", "explain",
                  "clear_zone", "unmet_pressure")
    assert not missing, f"ConstraintStore lost legacy surface: {missing}"


def test_add_many_creates_one_constraint_per_zone_sharing_a_complaint_id(twin, store):
    made = store.add_many(["zone_d", "zone_e"], "too_hot", 2, 0.9, twin.t,
                          "lobby and cafeteria are hot", "judge", "c-1")
    assert len(made) == 2, "add_many did not fan out across zones"
    assert {c.zone for c in made} == {"zone_d", "zone_e"}
    assert len({c.complaint_id for c in made}) == 1, \
        "zones of one complaint must share a complaint_id"
    assert len(store.by_complaint("c-1")) == 2


def test_opposing_constraints_produce_a_conflict_compromise(twin, store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "boiling", "a", "c-hot")
    store.add_many(["zone_b"], "too_cold", 2, 0.9, twin.t, "freezing", "b", "c-cold")
    ex = store.explain("zone_b", twin.t)
    assert ex["conflict"] is True, "opposing constraints did not register as a conflict"
    adj = store.zone_adjustments(twin.t)["zone_b"]["setpoint_offset"]
    assert abs(adj) < 1.3, f"compromise {adj} is not between the two demands"


def test_unapproved_constraints_do_not_steer_control(twin, store):
    """human_approval mode is meaningless if pending constraints still move the setpoint."""
    made = store.add_many(["zone_b"], "too_hot", 3, 0.95, twin.t, "boiling", "a", "c-x")
    for c in made:
        c.approved = False
    adj = store.zone_adjustments(twin.t).get("zone_b")
    offset = 0.0 if adj is None else adj["setpoint_offset"]
    assert offset == pytest.approx(0.0, abs=1e-9), \
        "an unapproved constraint influenced the arbitrated setpoint"


def test_store_clone_is_independent(twin, store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "a", "c-1")
    clone = store.clone()
    clone.clear_zone("zone_b", twin.t)
    assert len(store.active(twin.t, "zone_b")) == 1, "clearing the clone cleared the original"
    assert len(clone.active(twin.t, "zone_b")) == 0


def test_decay_reduces_influence_over_time(twin, store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "a", "c-1")
    now = store.active(twin.t, "zone_b")[0]
    assert now.decay(twin.t) == pytest.approx(1.0, abs=1e-6)
    assert now.decay(twin.t + 2700) == pytest.approx(0.5, abs=0.01), "45-min half-life broke"
    assert now.decay(twin.t + 7201) == 0.0, "constraint outlived its 2 h expiry"


def test_build_decision_returns_a_populated_explainable_record(twin, store):
    from backend.contracts import to_dict
    from backend.decisions import build_decision
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "stuffy in B", "judge", "c-1")
    d = build_decision(twin, store, "zone_b", 25.0, 24.2, 1, 2, "balanced", "automatic", True)
    data = d if isinstance(d, dict) else to_dict(d)
    required = {"zone", "prev_setpoint", "new_setpoint", "occupancy", "outdoor_c",
                "rh_pct", "constraints", "conflict", "objective", "safety_mode",
                "est_energy_delta_pct", "est_comfort_delta_pct", "summary", "reason_code"}
    assert required.issubset(data), f"decision record missing fields: {required - set(data)}"
    assert data["summary"], "decision summary is empty; the explainability panel has nothing to show"
    assert jsonable(data), "decision record is not JSON-serializable"


def test_decision_log_is_bounded_and_queryable(twin, store):
    from backend.decisions import DecisionLog, build_decision
    log = DecisionLog(maxlen=10)
    for i in range(25):
        log.record(build_decision(twin, store, "zone_b", 25.0, 25.0 - i * 0.01,
                                  1, 1, "balanced", "automatic", True))
    assert len(log.recent(100)) <= 10, "DecisionLog ignored its maxlen and will grow forever"
    assert all(d["zone"] == "zone_b" for d in log.for_zone("zone_b", 5))


# --------------------------------------------------------------------------
# 6 · controllers
# --------------------------------------------------------------------------

def test_act_signature_is_frozen_for_every_controller():
    """demo_day, rl/evaluate, sim/env and app all call act(twin, store) positionally."""
    from sim.controllers import ConstraintAware, ReactiveComfort, StaticSchedule
    for cls in (StaticSchedule, ReactiveComfort, ConstraintAware):
        p = params(cls().act)
        assert p[:2] == ["twin", "store"], f"{cls.__name__}.act signature drifted: {p}"


def test_constraint_aware_exposes_pinned_controls():
    from sim.controllers import ConstraintAware
    c = ConstraintAware(objective="balanced", safety_mode="automatic", locked_zones=[])
    missing = has(c, "last_decisions", "set_objective", "set_safety_mode",
                  "lock_zone", "unlock_zone")
    assert not missing, f"ConstraintAware missing pinned controls: {missing}"


def test_safety_envelope_cannot_be_escaped(twin, store):
    """No complaint, however severe, may drive the setpoint outside [21.5, 29.0]."""
    from sim.controllers import ConstraintAware
    for _ in range(8):
        store.add_many(["zone_b"], "too_hot", 3, 1.0, twin.t, "UNBEARABLE", "a", "c")
    sps, vents = ConstraintAware().act(twin, store)
    for zone, sp in sps.items():
        if sp is not None:
            assert 21.5 - 1e-9 <= sp <= 29.0 + 1e-9, f"{zone} escaped the safety envelope at {sp}"
    for zone, v in vents.items():
        assert 0 <= int(v) <= 2, f"{zone} vent level {v} outside 0-2"


def test_recommend_only_mode_does_not_move_the_setpoint(twin, store):
    from sim.controllers import ConstraintAware
    store.add_many(["zone_b"], "too_hot", 3, 0.95, twin.t, "boiling", "a", "c")
    auto = ConstraintAware(safety_mode="automatic").act(twin, store)[0]["zone_b"]
    rec = ConstraintAware(safety_mode="recommend_only").act(twin, store)[0]["zone_b"]
    assert auto < rec, "recommend_only applied the change it was only supposed to recommend"


def test_emergency_override_ignores_complaints(twin, store):
    from sim.controllers import ConstraintAware
    store.add_many(["zone_b"], "too_hot", 3, 0.95, twin.t, "boiling", "a", "c")
    sp = ConstraintAware(safety_mode="emergency_override").act(twin, store)[0]["zone_b"]
    assert sp == pytest.approx(24.0, abs=0.01), f"emergency override did not pin 24.0 (got {sp})"


def test_maintenance_lockout_excludes_the_locked_zone(twin, store):
    from sim.controllers import ConstraintAware
    store.add_many(["zone_b"], "too_hot", 3, 0.95, twin.t, "boiling", "a", "c")
    locked = ConstraintAware(locked_zones=["zone_b"], safety_mode="maintenance_lockout")
    free = ConstraintAware(safety_mode="automatic")
    assert locked.act(twin, store)[0]["zone_b"] > free.act(twin, store)[0]["zone_b"], \
        "a locked-out zone still accepted a complaint offset"


def test_objectives_trade_energy_against_comfort():
    """If every objective produces the same numbers, the parameter is decorative."""
    from scripts.demo_day import run
    from sim.controllers import ConstraintAware
    energy = run(ConstraintAware(objective="energy"), 2, 7)
    comfort = run(ConstraintAware(objective="comfort"), 2, 7)
    assert energy["kwh"] < comfort["kwh"], \
        f"energy objective ({energy['kwh']}) did not undercut comfort ({comfort['kwh']})"


# --------------------------------------------------------------------------
# 7 · what-if isolation
# --------------------------------------------------------------------------

def test_whatif_exposes_pinned_surface():
    from backend import whatif
    missing = has(whatif, "SCENARIOS", "run_scenario", "compare", "verify_isolation")
    assert not missing, f"backend.whatif missing: {missing}"
    assert len(whatif.SCENARIOS) >= 8, "scenario registry is too thin to be useful"


def test_scenario_never_mutates_live_state(twin, store):
    """The single non-negotiable of the what-if engine."""
    from backend import whatif
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "a", "c-1")
    before = (twin.kwh, twin.t, dict(twin.T), twin.viol_min, len(store.items))
    whatif.compare(twin, store, whatif.SCENARIOS["occupancy_up"])
    after = (twin.kwh, twin.t, dict(twin.T), twin.viol_min, len(store.items))
    assert before == after, "a what-if run mutated the live twin or store"
    assert whatif.verify_isolation(twin, store) is True


def test_compare_reports_baseline_scenario_and_delta(twin, store):
    from backend import whatif
    out = whatif.compare(twin, store, whatif.SCENARIOS["outdoor_hotter"])
    assert {"baseline", "scenario", "delta"}.issubset(out)
    # The pinned contract types baseline/scenario as ScenarioResult DATACLASSES,
    # so raw json.dumps is expected to fail; contracts.to_dict is the documented
    # boundary converter and is what the wire must go through.
    from backend.contracts import to_dict
    assert jsonable(to_dict(out)), "compare() output is not wire-safe via to_dict"


def test_measured_results_are_labelled_measured(twin, store):
    """Judges are told which numbers were simulated and which were extrapolated."""
    from backend import whatif
    r = whatif.run_scenario(twin, store, whatif.SCENARIOS["setpoint_up"])
    kind = r["kind"] if isinstance(r, dict) else r.kind
    assert kind in ("measured", "predicted")


# --------------------------------------------------------------------------
# 8 · maintenance, analytics, adapters, privacy
# --------------------------------------------------------------------------

def test_maintenance_monitor_surface(twin, store):
    from backend.maintenance import MaintenanceMonitor
    m = MaintenanceMonitor()
    assert not has(m, "tick", "alerts", "clear")
    assert m.alerts() == [], "a fresh monitor invented alerts with no evidence"
    m.tick(twin, store)
    assert jsonable(m.alerts()), "alerts are not JSON-serializable"


def test_analytics_store_surface_tolerates_empty_state(twin, store):
    from backend.analytics import AnalyticsStore
    a = AnalyticsStore()
    missing = has(a, "sample", "comfort_heatmap", "energy_series",
                  "complaint_stats", "controller_stats", "summary")
    assert not missing, f"AnalyticsStore missing: {missing}"
    for out in (a.comfort_heatmap(), a.energy_series(), a.summary()):
        assert out is not None, "analytics returned None instead of an empty-but-valid shape"
        assert jsonable(out)


def test_adapters_expose_sim_implementations(twin):
    from backend.adapters import get_adapters
    ad = get_adapters(twin)
    for name in ("hvac", "occupancy", "weather", "sensor"):
        assert name in ad, f"adapter registry missing {name!r}"
    state = ad["hvac"].read_state("zone_b")
    assert jsonable(state) and state, "SimHVACAdapter.read_state returned nothing useful"


def test_privacy_scrubs_pii_but_keeps_the_complaint():
    from backend.privacy import scrub_pii
    clean, found = scrub_pii("Room B is boiling, ping me at rakshit@example.com or 9876543210")
    assert "rakshit@example.com" not in clean, "email survived PII scrubbing"
    assert "9876543210" not in clean, "phone number survived PII scrubbing"
    assert "boiling" in clean.lower(), "scrubbing destroyed the actual complaint"
    assert found, "scrub_pii did not report what it removed"


def test_anonymize_is_stable_per_author_and_distinct_across_authors():
    from backend.privacy import anonymize_author
    assert anonymize_author("alice") == anonymize_author("alice"), "pseudonym is unstable"
    assert anonymize_author("alice") != anonymize_author("bob"), "pseudonyms collide"
    assert "alice" not in anonymize_author("alice").lower(), "pseudonym leaks the real name"


def test_external_ai_use_is_disclosed():
    from backend.privacy import AIDisclosure
    assert AIDisclosure.is_external("llm") is True
    assert AIDisclosure.is_external("rules") is False
    assert AIDisclosure.describe("rules"), "offline path has no disclosure text"


# --------------------------------------------------------------------------
# 9 · frozen headline results (the report and the pitch depend on these)
# --------------------------------------------------------------------------

@pytest.mark.slow
def test_seven_day_ab_numbers_have_not_drifted():
    from scripts.demo_day import run
    from sim.controllers import ConstraintAware, ReactiveComfort, StaticSchedule
    static = run(StaticSchedule(), 7, 7)
    reactive = run(ReactiveComfort(), 7, 7)
    ours = run(ConstraintAware(), 7, 7)

    assert static["kwh"] == pytest.approx(722.4, abs=0.5), "baseline energy drifted"
    assert static["viol_min"] == pytest.approx(16328, abs=5), "baseline violations drifted"
    assert reactive["kwh"] == pytest.approx(493.4, abs=0.5), "reactive energy drifted"
    assert reactive["viol_min"] == pytest.approx(429, abs=5), "reactive violations drifted"
    assert ours["kwh"] == pytest.approx(530.3, abs=0.5), "FeelsLike energy drifted"
    assert ours["viol_min"] == 0, "FeelsLike lost its zero-violation result"

    saved = 100.0 * (static["kwh"] - ours["kwh"]) / static["kwh"]
    assert saved == pytest.approx(26.6, abs=0.2), f"headline saving is now {saved:.1f}%"

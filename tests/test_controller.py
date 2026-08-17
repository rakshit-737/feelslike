"""Behavioural depth on the controllers: the safety envelope, the five safety
modes, the five objectives, and the three adaptive offset terms.

The organising idea is the one in the module docstring of sim/controllers.py:

    written_setpoint = clamp( schedule(objective) + clamp(offset, +-1.8 K) )

so the tests are grouped the same way — schedule, offset, mode, envelope — and the
envelope group is deliberately adversarial, because "no amount of complaining can
drive the building unsafe" is a safety claim and a safety claim needs an attack,
not an example.

Where a documented design decision looks like a missing distinction it is asserted
AS the decision: contracts.OBJECTIVE_WEIGHTS gives "cost" and "carbon" identical
comfort/energy splits on purpose (a flat tariff and a flat grid factor make them
the same control problem), so five objectives produce four distinct schedules.
"""
from __future__ import annotations

import pytest

from backend.contracts import OBJECTIVES, SAFETY_MODES, canonical_reason
from backend.contracts import REASON_CODES as CONTRACT_REASON_CODES
from backend.decisions import REASON_CODES as EMITTED_REASON_CODES
from sim.controllers import (DENSITY_MAX, EMERGENCY_SP, HUMID_MAX, MAX_OFFSET,
                             SAFE_HI, SAFE_LO, ConstraintAware, ReactiveComfort,
                             StaticSchedule, control_profile)
from sim.twin import RH_HUMID, ZONE_IDS, ZONES, DigitalTwin

ALL_MODES = ("automatic", "recommend_only", "human_approval",
             "emergency_override", "maintenance_lockout")


def settled(hour=10.0, minutes=30, **conditions):
    """A twin that has actually been running, so RH, capacity and last_setpoints
    are populated. The controller reads all three."""
    twin = DigitalTwin(seed=7)
    twin.t = hour * 3600.0
    if conditions:
        twin.set_conditions(**conditions)
    for _ in range(minutes):
        twin.step({z.id: 25.0 for z in ZONES}, {z.id: 1 for z in ZONES})
    return twin


def stack(store, zone, issue, n, twin, severity=3, confidence=1.0):
    for i in range(n):
        store.add_many([zone], issue, severity, confidence, twin.t, "x", f"a{i}", f"c{zone}{i}")
    return store


# --------------------------------------------------------------------------
# 1 · the frozen interface
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [StaticSchedule, ReactiveComfort, ConstraintAware])
def test_every_controller_returns_a_complete_legal_command(cls, store):
    twin = settled()
    setpoints, vents = cls().act(twin, store)
    assert set(setpoints) == set(ZONE_IDS), f"{cls.__name__} skipped a zone"
    assert set(vents) == set(ZONE_IDS)
    for zone, sp in setpoints.items():
        assert sp is None or isinstance(sp, float), f"{zone} setpoint {sp!r} is not degC-or-None"
    for zone, v in vents.items():
        assert int(v) in (0, 1, 2), f"{zone} vent {v!r} is not a fan level"


def test_act_never_mutates_the_twin_or_the_store(store):
    """The controller reads; the caller writes. If act() stepped the physics the
    what-if engine's isolation proof would be meaningless."""
    from backend.whatif import state_fingerprint
    twin = settled()
    stack(store, "zone_a", "too_hot", 2, twin, severity=2, confidence=0.9)
    before = state_fingerprint(twin, store)
    for mode in ALL_MODES:
        ConstraintAware(safety_mode=mode, locked_zones=["zone_a"]).act(twin, store)
    assert state_fingerprint(twin, store) == before, "act() mutated live state"


def test_the_baselines_ignore_the_store_entirely():
    """StaticSchedule and ReactiveComfort must stay dumb, or the A/B race is rigged."""
    twin = settled()
    from backend.constraints import ConstraintStore
    loud = stack(ConstraintStore(), "zone_a", "too_hot", 5, twin)
    for cls in (StaticSchedule, ReactiveComfort):
        assert cls().act(twin, loud)[0] == cls().act(twin, None)[0], \
            f"{cls.__name__} started listening to complaints"


# --------------------------------------------------------------------------
# 2 · the safety envelope, attacked
# --------------------------------------------------------------------------

@pytest.mark.parametrize("issue", ["too_hot", "too_cold", "stuffy", "humid", "drafty"])
def test_forty_maximal_complaints_cannot_escape_the_envelope(store, issue):
    twin = settled()
    stack(store, "zone_a", issue, 40, twin)
    setpoints, vents = ConstraintAware().act(twin, store)
    for zone, sp in setpoints.items():
        if sp is not None:
            assert SAFE_LO - 1e-9 <= sp <= SAFE_HI + 1e-9, f"{zone} escaped to {sp}"
        assert 0 <= int(vents[zone]) <= 2


def test_the_offset_is_clamped_at_exactly_the_documented_maximum(store):
    """A pile-on saturates at MAX_OFFSET and goes no further, in both directions."""
    twin = settled()
    base = ConstraintAware().base_occupied
    stack(store, "zone_a", "too_hot", 40, twin)
    assert ConstraintAware().act(twin, store)[0]["zone_a"] == \
        pytest.approx(base - MAX_OFFSET, abs=1e-9)

    from backend.constraints import ConstraintStore
    cold = stack(ConstraintStore(), "zone_a", "too_cold", 40, twin)
    assert ConstraintAware().act(twin, cold)[0]["zone_a"] == \
        pytest.approx(base + MAX_OFFSET, abs=1e-9)


def test_the_envelope_holds_under_every_objective_and_a_broken_building(store):
    """Worst case in one shot: a 10% coil, +10 degC outdoors, triple occupancy, a
    monsoon and five severe complaints in every zone."""
    twin = settled(outdoor_offset=10.0, occ_scale=3.0, capacity_scale=0.1,
                   humidity_offset=30.0)
    for zone in ZONE_IDS:
        stack(store, zone, "too_hot", 5, twin)
    for objective in OBJECTIVES:
        setpoints, vents = ConstraintAware(objective=objective).act(twin, store)
        for zone, sp in setpoints.items():
            if sp is not None:
                assert SAFE_LO - 1e-9 <= sp <= SAFE_HI + 1e-9, \
                    f"{objective}/{zone} escaped to {sp}"
            assert 0 <= int(vents[zone]) <= 2


def test_conflicting_pile_ons_still_land_inside_the_envelope(store):
    twin = settled()
    stack(store, "zone_a", "too_hot", 20, twin)
    stack(store, "zone_a", "too_cold", 20, twin)
    sp = ConstraintAware().act(twin, store)[0]["zone_a"]
    base = ConstraintAware().base_occupied
    assert SAFE_LO <= sp <= SAFE_HI
    assert abs(sp - base) <= MAX_OFFSET + 1e-9


def test_stacking_one_issue_saturates_at_the_cap_without_tripping_the_clamp(store):
    """Worth pinning: complaints ALONE can never bind the clamp. Every too_hot
    constraint carries the same raw_offset, so their weighted mean is exactly
    -1.8 K no matter how many are filed — equal to MAX_OFFSET, and _enforce_envelope
    only reports `bound` when the limit is strictly exceeded. So the honest reason
    for a pile-on is "complaint_offset", not "safety_clamp"."""
    twin = settled()
    stack(store, "zone_a", "too_hot", 40, twin)
    controller = ConstraintAware()
    controller.act(twin, store)
    decision = next(d for d in controller.last_decisions if d.zone == "zone_a")
    assert decision.reason_code == "complaint_offset"
    assert decision.new_setpoint == pytest.approx(controller.base_occupied - MAX_OFFSET)


def test_a_clamped_write_is_reported_as_a_safety_clamp(store):
    """The clamp binds when the ADAPTIVE terms stack on top of a saturated
    complaint: a triple-booked cafeteria contributes the crowd term (-1.0 K) on top
    of a severity-3 heat complaint (-1.8 K), asking for -2.8 K. The write is held
    at -1.8 K and the reason code says so, which is the precedence rule
    sim/controllers.py documents (a bound clamp is always the headline)."""
    twin = settled(hour=13.0, occ_scale=3.0)
    assert twin.zone_snapshot("zone_e")["occ_pct"] == pytest.approx(300.0)
    store.add_many(["zone_e"], "too_hot", 3, 1.0, twin.t, "boiling", "amy", "c1")
    controller = ConstraintAware()
    setpoints, _vents = controller.act(twin, store)
    decision = next(d for d in controller.last_decisions if d.zone == "zone_e")
    assert decision.reason_code == "safety_clamp", \
        f"a bound clamp was not the headline reason: {decision.reason_code}"
    assert setpoints["zone_e"] == pytest.approx(controller.base_occupied - MAX_OFFSET), \
        "the clamp reported itself but let the offset through anyway"


def test_the_documented_envelope_constants_have_not_moved():
    assert (SAFE_LO, SAFE_HI, MAX_OFFSET) == (21.5, 29.0, 1.8)


# --------------------------------------------------------------------------
# 3 · the five safety modes
# --------------------------------------------------------------------------

def mode_signature(mode, twin, store):
    """What an operator can observe about one zone under one mode."""
    controller = ConstraintAware(safety_mode=mode, locked_zones=["zone_a"])
    setpoints, vents = controller.act(twin, store)
    decision = next(d for d in controller.last_decisions if d.zone == "zone_a")
    return (setpoints["zone_a"], int(vents["zone_a"]), decision.reason_code,
            bool(decision.applied), len(controller.pending_recommendations))


@pytest.fixture
def approval_case(store):
    """zone_a with one approved complaint and one still awaiting approval — the
    only state in which all five modes are actually distinguishable."""
    twin = settled()
    store.add_many(["zone_a"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
    held = store.add_many(["zone_a"], "too_hot", 3, 0.95, twin.t, "boiling", "bob", "c2")
    for c in held:
        c.approved = False
    return twin, store


def test_all_five_safety_modes_behave_distinctly(approval_case):
    twin, store = approval_case
    signatures = {mode: mode_signature(mode, twin, store) for mode in ALL_MODES}
    assert len(set(signatures.values())) == len(ALL_MODES), \
        "two safety modes are observationally identical:\n" + \
        "\n".join(f"  {m}: {s}" for m, s in signatures.items())
    for mode in ALL_MODES:
        assert mode in SAFETY_MODES, f"{mode} is not in the pinned vocabulary"


def test_automatic_applies_the_approved_offset_and_nothing_else(approval_case):
    twin, store = approval_case
    base = ConstraintAware().base_occupied
    sp, _vent, reason, applied, pending = mode_signature("automatic", twin, store)
    assert applied is True and reason == "complaint_offset"
    assert sp < base, "automatic mode did not act on an approved complaint"
    assert sp > base - MAX_OFFSET, "the pending severity-3 complaint leaked through"
    assert pending == 0


def test_recommend_only_writes_the_schedule_and_flags_the_recommendation(approval_case):
    twin, store = approval_case
    base = ConstraintAware().base_occupied
    sp, _vent, reason, applied, pending = mode_signature("recommend_only", twin, store)
    assert sp == pytest.approx(base), "recommend_only applied the change it only advises"
    assert applied is False and reason == "recommend_only"
    assert pending == 1, "nothing was surfaced for the operator to act on"


def test_human_approval_applies_only_what_was_approved_and_queues_the_rest(approval_case):
    twin, store = approval_case
    auto = mode_signature("automatic", twin, store)
    held = mode_signature("human_approval", twin, store)
    assert held[0] == auto[0], "the approved part of the queue was withheld too"
    assert held[4] == 1, "the unapproved complaint was not queued for an operator"
    recommendation = ConstraintAware(safety_mode="human_approval").act(twin, store) and None
    controller = ConstraintAware(safety_mode="human_approval")
    controller.act(twin, store)
    row = controller.pending_recommendations[0]
    assert row["recommended_setpoint"] < row["current_setpoint"], \
        "the queued recommendation does not differ from what was written"
    assert row["unapproved_constraints"] == 1
    assert recommendation is None


def test_emergency_override_pins_the_safe_band_and_ignores_complaints(store):
    twin = settled()
    stack(store, "zone_a", "too_hot", 10, twin)
    setpoints, vents = ConstraintAware(safety_mode="emergency_override").act(twin, store)
    assert setpoints["zone_a"] == pytest.approx(EMERGENCY_SP, abs=1e-9)
    assert int(vents["zone_a"]) == 1
    assert SAFE_LO <= EMERGENCY_SP <= SAFE_HI


def test_emergency_override_treats_a_complaint_as_proof_of_occupancy(store):
    """A complaint from an "empty" zone means the schedule is wrong about who is in
    it, so the safe band is enforced there rather than the HVAC left off."""
    twin = settled(hour=3.0)                        # nobody is scheduled anywhere
    assert twin.occupancy_now("zone_b") == 0
    empty = ConstraintAware(safety_mode="emergency_override").act(twin, store)[0]
    assert empty["zone_b"] is None, "the compressor ran for an empty, silent zone"
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
    heard = ConstraintAware(safety_mode="emergency_override").act(twin, store)[0]
    assert heard["zone_b"] == pytest.approx(EMERGENCY_SP), \
        "an occupant in a nominally empty zone was ignored in an emergency"


def test_maintenance_lockout_freezes_only_the_locked_zone(store):
    twin = settled()
    store.add_many(["zone_a"], "too_hot", 3, 0.95, twin.t, "hot", "amy", "c1")
    store.add_many(["zone_d"], "too_hot", 3, 0.95, twin.t, "hot", "bob", "c2")
    locked = ConstraintAware(safety_mode="maintenance_lockout", locked_zones=["zone_a"])
    free = ConstraintAware(safety_mode="automatic")
    locked_sps = locked.act(twin, store)[0]
    free_sps = free.act(twin, store)[0]
    assert locked_sps["zone_a"] > free_sps["zone_a"], "the locked zone still took the offset"
    assert locked_sps["zone_a"] == pytest.approx(locked.base_occupied), \
        "a locked zone must sit on its base schedule setpoint"
    assert locked_sps["zone_d"] == pytest.approx(free_sps["zone_d"]), \
        "locking one zone changed an unrelated zone"


def test_a_lockout_list_does_nothing_outside_lockout_mode(store):
    """SAFETY_MODES documents the lockout as mode-scoped; a lock that silently bit
    in automatic mode would be an invisible control."""
    twin = settled()
    store.add_many(["zone_a"], "too_hot", 3, 0.95, twin.t, "hot", "amy", "c1")
    with_list = ConstraintAware(safety_mode="automatic", locked_zones=["zone_a"])
    without = ConstraintAware(safety_mode="automatic")
    assert with_list.act(twin, store)[0]["zone_a"] == \
        pytest.approx(without.act(twin, store)[0]["zone_a"])


def test_a_locked_zone_still_records_the_complaint(store):
    """"Constraints for those zones are recorded but never applied" — the recording
    half is what lets an operator see why the room is unhappy."""
    twin = settled()
    store.add_many(["zone_a"], "too_hot", 3, 0.95, twin.t, "hot", "amy", "c1")
    controller = ConstraintAware(safety_mode="maintenance_lockout", locked_zones=["zone_a"])
    controller.act(twin, store)
    decision = next(d for d in controller.last_decisions if d.zone == "zone_a")
    assert decision.reason_code == "locked_out"
    assert decision.constraints, "the complaint vanished from the audit record"
    assert len(store.active(twin.t, "zone_a")) == 1


# --------------------------------------------------------------------------
# 4 · the five objectives
# --------------------------------------------------------------------------

def test_the_objective_schedules_are_ordered_by_energy_weight():
    occupied = {o: control_profile(o).occupied for o in OBJECTIVES}
    assert occupied["comfort"] < occupied["balanced"] < occupied["cost"] < occupied["energy"], \
        f"objective schedules are not ordered: {occupied}"
    leads = {o: control_profile(o).lead_h for o in OBJECTIVES}
    assert leads["comfort"] > leads["balanced"] > leads["energy"], \
        f"pre-cool lead is not ordered by comfort weight: {leads}"
    assert control_profile("comfort").flush and not control_profile("energy").flush


def test_cost_and_carbon_are_identical_by_design():
    """NOT a missing distinction. contracts.OBJECTIVE_WEIGHTS gives both the same
    comfort/energy split because the sim's tariff and grid factor are flat scalars
    of kWh: minimising rupees and minimising CO2 are the same control problem here.
    They differ only in how the impact is reported. If a time-of-use tariff or a
    carbon-intensity curve ever lands, these two rows are what must diverge."""
    assert control_profile("cost") == control_profile("carbon")._replace(objective="cost") \
        if hasattr(control_profile("cost"), "_replace") else True
    cost, carbon = control_profile("cost"), control_profile("carbon")
    assert (cost.occupied, cost.precool, cost.unoccupied, cost.lead_h) == \
           (carbon.occupied, carbon.precool, carbon.unoccupied, carbon.lead_h)


def test_balanced_still_lands_on_the_published_calibration():
    """The A/B headline (722.4 -> 530.3 kWh, 0 violation-minutes) was measured with
    these exact numbers. Deriving them from weights is fine; changing them is not."""
    p = control_profile("balanced")
    assert (p.occupied, p.precool, p.unoccupied, p.lead_h) == (25.0, 26.0, 28.5, 0.5)


def test_every_objective_schedule_sits_inside_the_comfort_band_or_the_envelope():
    from sim.twin import BAND
    for objective in OBJECTIVES:
        p = control_profile(objective)
        assert p.occupied <= BAND[1], \
            f"{objective} books a comfort violation by schedule alone: {p.occupied}"
        assert SAFE_LO <= p.occupied <= SAFE_HI and p.unoccupied <= SAFE_HI


def test_the_objectives_produce_a_real_energy_comfort_trade_off():
    """One simulated day per objective (a full weekday with every occupancy
    regime). The point is that the parameter is not decorative: leaning towards
    energy must buy kWh and cost comfort."""
    from scripts.demo_day import run
    results = {o: run(ConstraintAware(objective=o), 1, 7) for o in OBJECTIVES}
    kwh = {o: r["kwh"] for o, r in results.items()}
    viol = {o: r["viol_min"] for o, r in results.items()}
    assert kwh["energy"] < kwh["cost"] < kwh["balanced"] < kwh["comfort"], \
        f"energy ranking collapsed: {kwh}"
    assert viol["energy"] > viol["balanced"], \
        f"the cheaper objective cost nothing in comfort, which cannot be true: {viol}"
    assert viol["comfort"] == 0.0 and viol["balanced"] == 0.0
    assert len({round(v, 2) for v in kwh.values()}) == 4, \
        f"expected 4 distinct energy results (cost == carbon by design): {kwh}"


def test_switching_objective_re_derives_the_whole_schedule():
    controller = ConstraintAware(objective="balanced")
    assert controller.base_occupied == 25.0
    controller.set_objective("energy")
    assert controller.base_occupied == control_profile("energy").occupied
    assert controller.base_precool == control_profile("energy").precool
    assert controller.profile.lead_h == 0.25


def test_a_constructor_override_survives_an_objective_switch():
    controller = ConstraintAware(base_occupied=23.0, objective="balanced")
    controller.set_objective("energy")
    assert controller.base_occupied == 23.0, "an explicit override was overwritten"


# --------------------------------------------------------------------------
# 5 · the schedule: setback and pre-cool timing
# --------------------------------------------------------------------------

def test_occupancy_setback_precool_and_occupied_fire_in_that_order():
    """Conference room B is booked 10:00-11:00 and the balanced lead is 0.5 h, so
    the three regimes are observable at 09:00, 09:45 and 10:00 on the same day."""
    controller = ConstraintAware()
    profile = controller.profile
    seen = {}
    for hour in (9.0, 9.45, 9.75, 10.0, 10.5, 11.5):
        twin = settled(hour=hour, minutes=0)
        sps, vents = controller.act(twin, None)
        seen[hour] = (sps["zone_b"], int(vents["zone_b"]))

    assert seen[9.0] == (profile.unoccupied, 0), f"09:00 was not a setback: {seen[9.0]}"
    assert seen[9.45] == (profile.unoccupied, 0), \
        f"pre-cool started earlier than the {profile.lead_h} h lead: {seen[9.45]}"
    assert seen[9.75] == (profile.precool, 1), f"09:45 did not pre-cool: {seen[9.75]}"
    assert seen[10.0] == (profile.occupied, 1), f"10:00 was not occupied: {seen[10.0]}"
    assert seen[10.5] == (profile.occupied, 1), "the room emptied while still booked"
    assert seen[11.5] == (profile.unoccupied, 0), "the setback never came back"


def test_a_longer_lead_starts_pre_cooling_earlier():
    twin = settled(hour=9.45, minutes=0)
    comfort = ConstraintAware(objective="comfort")          # lead 0.75 h
    energy = ConstraintAware(objective="energy")            # lead 0.25 h
    assert comfort.act(twin, None)[0]["zone_b"] == comfort.profile.precool, \
        "the comfort objective did not use its longer look-ahead"
    assert energy.act(twin, None)[0]["zone_b"] == energy.profile.unoccupied


def test_the_setback_is_a_setpoint_not_a_shutdown_when_someone_is_expected():
    twin = settled(hour=9.75, minutes=0)
    sp = ConstraintAware().act(twin, None)[0]["zone_b"]
    assert sp is not None, "pre-cool switched the compressor off"


def test_the_reactive_baseline_only_reacts_to_current_occupancy():
    early = settled(hour=9.75, minutes=0)
    assert ReactiveComfort().act(early, None)[0]["zone_b"] is None, \
        "the reactive baseline learned to pre-cool"
    late = settled(hour=10.0, minutes=0)
    assert ReactiveComfort().act(late, None)[0]["zone_b"] == 24.0


# --------------------------------------------------------------------------
# 6 · the adaptive offset terms
# --------------------------------------------------------------------------

def test_the_humidity_term_only_fires_for_a_confirmed_latent_complaint(store):
    """Deliberate gating: the twin's apparatus-dew-point approximation pins RH near
    88% most occupied minutes, so chasing the RH sensor alone would spend real
    energy on a modelling artefact. An occupant has to confirm it."""
    twin = settled(minutes=60)
    assert twin.zone_snapshot("zone_a")["rh_pct"] > RH_HUMID, "fixture is not muggy"

    def extra_depression(issue):
        from backend.constraints import ConstraintStore
        s = ConstraintStore()
        s.add_many(["zone_a"], issue, 2, 0.9, twin.t, "x", "amy", "c1")
        controller = ConstraintAware()
        written = controller.act(twin, s)[0]["zone_a"]
        asked = controller.base_occupied + s.zone_adjustments(twin.t)["zone_a"]["setpoint_offset"]
        return round(written - asked, 3)

    for issue in ("humid", "stuffy"):
        extra = extra_depression(issue)
        assert extra < 0.0, f"a confirmed {issue} complaint got no latent top-up"
        assert abs(extra) <= HUMID_MAX + 1e-9, f"{issue} top-up {extra} exceeded HUMID_MAX"
    assert extra_depression("too_hot") == 0.0, \
        "the humidity term fired for a complaint that never mentioned moisture"


def test_the_humidity_term_is_silent_in_dry_air(store):
    twin = settled(minutes=60)
    twin.W = {z.id: 0.004 for z in ZONES}           # ~20% RH at 25 degC
    assert twin.zone_snapshot("zone_a")["rh_pct"] < RH_HUMID
    store.add_many(["zone_a"], "humid", 2, 0.9, twin.t, "sticky", "amy", "c1")
    controller = ConstraintAware()
    written = controller.act(twin, store)[0]["zone_a"]
    asked = controller.base_occupied + store.zone_adjustments(twin.t)["zone_a"]["setpoint_offset"]
    assert written == pytest.approx(asked), \
        "the latent top-up fired although the sensor disagreed with the occupant"


def test_the_crowd_term_only_bites_above_design_headcount():
    """Below 100% of design the coil was sized for the load, so the term is exactly
    zero — which is why the published A/B numbers are untouched by it."""
    at_design = settled(hour=13.0, minutes=5)
    assert at_design.zone_snapshot("zone_e")["occ_pct"] == pytest.approx(100.0)
    controller = ConstraintAware()
    assert controller.act(at_design, None)[0]["zone_e"] == \
        pytest.approx(controller.base_occupied), "the crowd term fired at design load"

    overloaded = settled(hour=13.0, minutes=5, occ_scale=1.5)
    half_over = controller.act(overloaded, None)[0]["zone_e"]
    assert half_over < controller.base_occupied, "50% overload bought no extra cooling"

    swamped = settled(hour=13.0, minutes=5, occ_scale=3.0)
    full_over = controller.act(swamped, None)[0]["zone_e"]
    assert full_over < half_over, "the crowd term is not proportional to the overload"
    assert full_over >= controller.base_occupied - DENSITY_MAX - 1e-9, \
        f"the crowd term escaped its {DENSITY_MAX} K cap: {full_over}"


def test_the_crowd_vent_boost_needs_both_a_headcount_and_a_density():
    """ASHRAE outdoor air is per person, so the boost needs an absolute crowd; but
    fan power is real, so it also needs the zone to be near its design load."""
    busy = settled(hour=13.0, minutes=5)
    assert int(ConstraintAware().act(busy, None)[1]["zone_e"]) == 2, \
        "30 people at design load did not earn the outdoor-air boost"
    quiet = settled(hour=10.0, minutes=5)
    assert busy.zone_snapshot("zone_c")["occ"] < 20
    assert int(ConstraintAware().act(quiet, None)[1]["zone_c"]) == 1, \
        "a three-person cabin ran the fan flat out"


def test_an_energy_objective_declines_the_comfort_investments():
    busy = settled(hour=13.0, minutes=5)
    assert int(ConstraintAware(objective="energy").act(busy, None)[1]["zone_e"]) == 1, \
        "the energy objective still paid for the outdoor-air boost"


# --------------------------------------------------------------------------
# 7 · the audit trail
# --------------------------------------------------------------------------

def test_last_decisions_is_populated_for_material_events_only(store):
    twin = settled()
    quiet = ConstraintAware()
    quiet.act(twin, None)
    quiet_zones = {d.zone for d in quiet.last_decisions}

    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
    loud = ConstraintAware()
    loud.act(twin, store)
    assert "zone_b" in {d.zone for d in loud.last_decisions}, \
        "a complaint produced no audit record"
    assert "zone_b" not in quiet_zones, "an uneventful zone was logged anyway"

    decision = next(d for d in loud.last_decisions if d.zone == "zone_b")
    assert decision.summary, "the explainability panel has nothing to render"
    assert decision.constraints, "the decision does not carry what drove it"
    assert decision.objective == "balanced" and decision.safety_mode == "automatic"


def test_last_decisions_is_refreshed_not_accumulated(store):
    twin = settled()
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
    controller = ConstraintAware()
    sizes = []
    for _ in range(3):
        controller.act(twin, store)
        sizes.append(len(controller.last_decisions))
    assert len(set(sizes)) == 1, f"the decision list grew without bound: {sizes}"


def test_pending_recommendations_is_refreshed_not_accumulated(store):
    twin = settled()
    held = store.add_many(["zone_a"], "too_hot", 3, 0.95, twin.t, "boiling", "amy", "c1")
    for c in held:
        c.approved = False
    controller = ConstraintAware(safety_mode="human_approval")
    for _ in range(3):
        controller.act(twin, store)
        assert len(controller.pending_recommendations) == 1, \
            "a stale recommendation queue is worse than none"


@pytest.mark.parametrize("code", sorted(EMITTED_REASON_CODES))
def test_every_emitted_reason_code_resolves_to_the_pinned_vocabulary(code):
    assert canonical_reason(code) in CONTRACT_REASON_CODES, \
        f"{code!r} has no canonical spelling; the explainability panel cannot label it"


def test_the_controller_never_invents_a_reason_code(store):
    """Sweep every mode x objective x lock combination and check the vocabulary."""
    twin = settled()
    store.add_many(["zone_a"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
    store.add_many(["zone_b"], "too_cold", 2, 0.9, twin.t, "cold", "bob", "c2")
    seen = set()
    for mode in ALL_MODES:
        for objective in OBJECTIVES:
            controller = ConstraintAware(objective=objective, safety_mode=mode,
                                         locked_zones=["zone_a"])
            controller.act(twin, store)
            seen.update(d.reason_code for d in controller.last_decisions)
    assert seen <= set(EMITTED_REASON_CODES), f"unknown reason codes: {seen - set(EMITTED_REASON_CODES)}"
    assert len(seen) >= 4, f"the sweep only exercised {seen}"


# --------------------------------------------------------------------------
# 8 · configuration errors
# --------------------------------------------------------------------------

def test_an_unknown_objective_or_mode_is_rejected_loudly():
    with pytest.raises(ValueError):
        ConstraintAware(objective="cheapest")
    with pytest.raises(ValueError):
        ConstraintAware(safety_mode="yolo")
    with pytest.raises(ValueError):
        ConstraintAware().set_objective("cheapest")
    with pytest.raises(ValueError):
        ConstraintAware().set_safety_mode("yolo")


def test_locking_a_typo_fails_but_unlocking_anything_never_does():
    controller = ConstraintAware()
    with pytest.raises(ValueError):
        controller.lock_zone("zone_z")
    assert controller.unlock_zone("zone_z") == set(), "unlocking must never fail"
    assert controller.lock_zone("zone_b") == {"zone_b"}
    assert controller.unlock_zone("zone_b") == set()


def test_a_missing_store_is_not_an_error():
    twin = settled()
    setpoints, vents = ConstraintAware().act(twin, None)
    assert set(setpoints) == set(ZONE_IDS) and set(vents) == set(ZONE_IDS)


def test_a_broken_store_degrades_instead_of_stopping_control():
    """Control is load-bearing; the constraint store is not. A store that raises
    must cost the building its complaint handling, never its cooling."""
    class Exploding:
        def active(self, *a, **k):
            raise RuntimeError("boom")

        def zone_adjustments(self, *a, **k):
            raise RuntimeError("boom")

    twin = settled()
    setpoints, _vents = ConstraintAware().act(twin, Exploding())
    assert setpoints["zone_a"] == pytest.approx(ConstraintAware().base_occupied)

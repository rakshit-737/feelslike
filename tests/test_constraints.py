"""Behavioural depth on the constraint store: the decay maths, the arbitration
that turns disagreeing occupants into one number, and the approval gate.

This is where "recency-weighted democracy, not a ticket queue" either is or is not
true, so the numbers are asserted exactly rather than approximately wherever the
formula is closed-form (weight = severity x confidence x decay, half-life 45 min,
expiry 2 h).

Two tests are xfail(strict) and tagged `defect`: both are the SAME root cause —
expiring a constraint by back-dating created_t, when created_t is also the only
timestamp ComfortMemory.patterns() has to cluster on. They are kept as failing
tests so the corruption stays visible in every run.
"""
from __future__ import annotations

import pytest

from backend.constraints import (EXPIRY_S, HALF_LIFE_S, ISSUE_EFFECTS, Constraint,
                                 ConstraintStore)

T0 = 10 * 3600.0        # Monday 10:00, in sim-seconds


# --------------------------------------------------------------------------
# 1 · creation
# --------------------------------------------------------------------------

def test_from_issue_reads_its_offset_out_of_the_effects_table(store):
    for issue, (offsets, vent) in ISSUE_EFFECTS.items():
        for sev in (1, 2, 3):
            c = Constraint.from_issue("zone_b", issue, sev, 0.9, T0)
            assert c.raw_offset == offsets[sev], f"{issue} sev {sev} -> {c.raw_offset}"
            assert c.vent_delta == vent
            assert c.severity == sev


def test_severity_is_clamped_and_never_escapes_one_to_three():
    for asked, expected in ((-5, 1), (0, 1), (1, 1), (3, 3), (9, 3)):
        c = Constraint.from_issue("zone_b", "too_hot", asked, 0.9, T0)
        assert c.severity == expected, f"severity {asked} -> {c.severity}"


def test_add_returns_an_explanation_and_files_the_constraint(store):
    ex = store.add(Constraint.from_issue("zone_b", "too_hot", 2, 0.9, T0, "hot", "amy"))
    assert len(store.items) == 1
    assert ex["zone"] == "zone_b"
    assert ex["summary"], "add() produced no human-readable explanation"
    assert ex["adjustment"]["setpoint_offset"] == pytest.approx(-1.3)


def test_an_unknown_issue_is_recorded_but_moves_nothing(store):
    """The store must not guess. An issue outside ISSUE_EFFECTS gets a zero offset
    and no fan change rather than being dropped, so the complaint stays auditable."""
    store.add(Constraint.from_issue("zone_b", "other", 3, 1.0, T0))
    assert len(store.items) == 1
    adj = store.zone_adjustments(T0)["zone_b"]
    assert adj["setpoint_offset"] == 0.0, "an unmapped issue moved the setpoint"
    assert adj["vent_delta"] == 0, "an unmapped issue moved the fan"


def test_ids_are_monotonic_and_unique(store):
    made = store.add_many(["zone_a", "zone_b", "zone_c"], "too_hot", 2, 0.9, T0,
                          "hot", "amy", "cmp-x")
    ids = [c.id for c in made]
    assert ids == sorted(ids) and len(set(ids)) == 3, f"ids not monotonic/unique: {ids}"


# --------------------------------------------------------------------------
# 2 · decay, expiry and weighting
# --------------------------------------------------------------------------

@pytest.mark.parametrize("minutes,expected", [(0, 1.0), (45, 0.5), (90, 0.25)])
def test_decay_halves_every_forty_five_minutes(store, minutes, expected):
    """0 / 45 / 90 min is the whole ladder that fits inside the 2 h expiry — the
    fourth half-life (135 min) is past the cliff and is asserted separately."""
    c = store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")[0]
    assert c.decay(T0 + minutes * 60.0) == pytest.approx(expected, abs=1e-9)
    assert HALF_LIFE_S == 45 * 60.0, "the documented 45-min half-life moved"


def test_expiry_is_a_hard_cliff_at_two_hours(store):
    c = store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")[0]
    assert EXPIRY_S == 2 * 3600.0
    assert c.decay(T0 + EXPIRY_S) > 0.0, "the boundary itself must still count"
    assert c.decay(T0 + EXPIRY_S + 1.0) == 0.0, "constraint outlived its 2 h expiry"
    assert c.expires_in_min(T0) == pytest.approx(120.0)
    assert c.expires_in_min(T0 + 3600.0) == pytest.approx(60.0)
    assert c.expires_in_min(T0 + EXPIRY_S + 999.0) == 0.0


def test_age_and_expiry_never_go_negative_for_a_clock_that_moved_backwards(store):
    """A what-if clone can be fingerprinted at an earlier t than a constraint's
    created_t. That must read as "brand new", not as negative age."""
    c = store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")[0]
    assert c.age_min(T0 - 5000.0) == 0.0
    assert c.decay(T0 - 5000.0) == pytest.approx(1.0)
    assert c.expires_in_min(T0 - 5000.0) == pytest.approx(120.0)


def test_weight_is_exactly_severity_times_confidence_times_decay(store):
    c = store.add_many(["zone_b"], "too_hot", 3, 0.8, T0, "hot", "amy", "c1")[0]
    for minutes in (0, 45, 90):
        expected = 3 * 0.8 * c.decay(T0 + minutes * 60.0)
        assert c.weight(T0 + minutes * 60.0) == pytest.approx(expected, abs=1e-12)


def test_a_severe_confident_complaint_outweighs_a_mild_unsure_one(store):
    heavy = Constraint.from_issue("zone_b", "too_hot", 3, 1.0, T0)
    light = Constraint.from_issue("zone_b", "too_hot", 1, 0.5, T0)
    assert heavy.weight(T0) == 3.0
    assert light.weight(T0) == 0.5
    # ...and a fresh mild report eventually outweighs a stale severe one.
    stale = heavy.weight(T0 + 3 * HALF_LIFE_S)
    assert light.weight(T0) > stale, f"recency never wins: {light.weight(T0)} vs {stale}"


def test_active_drops_a_constraint_once_it_expires(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    assert len(store.active(T0 + 119 * 60.0, "zone_b")) == 1
    assert store.active(T0 + EXPIRY_S + 1.0, "zone_b") == []
    assert len(store.items) == 1, "an expired constraint must stay in history"


def test_decay_is_what_fades_the_offset_not_a_step_function(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    store.add_many(["zone_b"], "too_cold", 2, 0.9, T0 + 2700.0, "cold", "bob", "c2")
    # 45 min after the second report the first has decayed twice as far, so the
    # compromise must sit on the cold side of the midpoint.
    adj = store.zone_adjustments(T0 + 2700.0)["zone_b"]
    assert -1.3 < adj["setpoint_offset"] < 1.3
    assert adj["setpoint_offset"] > -0.5, \
        f"the older complaint still dominated: {adj['setpoint_offset']}"


# --------------------------------------------------------------------------
# 3 · arbitration
# --------------------------------------------------------------------------

def test_opposing_constraints_land_strictly_between_the_two_demands(store):
    store.add_many(["zone_b"], "too_hot", 3, 0.9, T0, "boiling", "amy", "c-hot")
    store.add_many(["zone_b"], "too_cold", 1, 0.5, T0, "freezing", "bob", "c-cold")
    hot_demand = ISSUE_EFFECTS["too_hot"][0][3]      # -1.8
    cold_demand = ISSUE_EFFECTS["too_cold"][0][1]    # +0.8
    adj = store.zone_adjustments(T0)["zone_b"]
    off = adj["setpoint_offset"]
    assert hot_demand < off < cold_demand, \
        f"compromise {off} is not between {hot_demand} and {cold_demand}"
    assert adj["conflict"] is True
    assert adj["n"] == 2


def test_the_compromise_is_the_weighted_mean_not_the_midpoint(store):
    """Three loud "too hot" against one quiet "too cold" must resolve towards hot."""
    store.add_many(["zone_b"], "too_hot", 3, 0.9, T0, "boiling", "amy", "c1")
    store.add_many(["zone_b"], "too_hot", 3, 0.9, T0, "boiling", "ben", "c2")
    store.add_many(["zone_b"], "too_hot", 3, 0.9, T0, "boiling", "cal", "c3")
    store.add_many(["zone_b"], "too_cold", 1, 0.4, T0, "chilly", "dee", "c4")
    off = store.zone_adjustments(T0)["zone_b"]["setpoint_offset"]
    midpoint = (-1.8 + 0.8) / 2.0
    assert off < midpoint, f"the majority lost the arbitration: {off} vs midpoint {midpoint}"
    assert off > -1.8, "the compromise ignored the dissenting occupant entirely"


def test_agreeing_constraints_do_not_read_as_a_conflict(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    store.add_many(["zone_b"], "stuffy", 2, 0.9, T0, "stuffy", "bob", "c2")
    ex = store.explain("zone_b", T0)
    assert ex["conflict"] is False, "two complaints pulling the same way is not a conflict"
    assert ex["arbitration"] == "weighted_mean"


def test_arbitration_mode_is_reported_for_every_shape(store):
    assert store.explain("zone_b", T0)["arbitration"] == "none"
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    assert store.explain("zone_b", T0)["arbitration"] == "single"
    store.add_many(["zone_b"], "stuffy", 2, 0.9, T0, "stuffy", "bob", "c2")
    assert store.explain("zone_b", T0)["arbitration"] == "weighted_mean"
    store.add_many(["zone_b"], "too_cold", 2, 0.9, T0, "cold", "cal", "c3")
    assert store.explain("zone_b", T0)["arbitration"] == "conflict_weighted_mean"


def test_stacked_stuffy_reports_cannot_drive_the_fan_past_one_step(store):
    for author in ("amy", "bob", "cal"):
        store.add_many(["zone_b"], "stuffy", 2, 0.9, T0, "s", author, f"c-{author}")
    assert store.zone_adjustments(T0)["zone_b"]["vent_delta"] == 1, "vent delta not clipped"


def test_a_faded_complaint_stops_moving_the_fan_before_it_expires(store):
    """The fan only answers a complaint whose weight is still above 0.25. A mild,
    unsure report (sev 1 x conf 0.5) crosses that line at ~45 min, so the airflow
    change is withdrawn well before the setpoint offset expires."""
    store.add_many(["zone_b"], "stuffy", 1, 0.5, T0, "a bit stuffy", "amy", "c1")
    assert store.zone_adjustments(T0)["zone_b"]["vent_delta"] == 1
    faded = store.zone_adjustments(T0 + 50 * 60.0)["zone_b"]
    assert faded["vent_delta"] == 0, f"a faded complaint still moved the fan: {faded}"
    assert faded["setpoint_offset"] != 0.0, "the setpoint offset should still be live"


def test_zone_adjustments_only_reports_zones_that_were_complained_about(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    adj = store.zone_adjustments(T0)
    assert set(adj) == {"zone_b"}, f"invented adjustments for {set(adj) - {'zone_b'}}"


def test_explain_text_names_the_issue_the_age_and_the_number(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    summary = store.explain("zone_b", T0 + 600.0)["summary"]
    assert "too hot" in summary and "10m ago" in summary and "-1.3" in summary, summary


# --------------------------------------------------------------------------
# 4 · multi-zone grouping
# --------------------------------------------------------------------------

def test_add_many_files_one_constraint_per_zone_under_one_complaint_id(store):
    made = store.add_many(["zone_d", "zone_e"], "too_hot", 2, 0.9, T0,
                          "lobby and cafeteria are hot", "judge", "")
    assert [c.zone for c in made] == ["zone_d", "zone_e"], "zone order was not preserved"
    cid = made[0].complaint_id
    assert cid.startswith("cmp-"), f"no deterministic complaint id: {cid!r}"
    assert len({c.complaint_id for c in made}) == 1
    assert store.by_complaint(cid) == made


def test_add_many_collapses_duplicate_zones_and_keeps_first_mention_order(store):
    made = store.add_many(["zone_e", "zone_d", "zone_e", None, ""], "stuffy", 1, 0.6,
                          T0, "x", "amy", "c1")
    assert [c.zone for c in made] == ["zone_e", "zone_d"], [c.zone for c in made]


def test_add_many_with_no_zones_files_nothing(store):
    assert store.add_many([], "too_hot", 2, 0.9, T0, "x", "amy", "c1") == []
    assert store.add_many(None, "too_hot", 2, 0.9, T0, "x", "amy", "c1") == []
    assert store.items == []


def test_by_complaint_never_matches_the_blank_id(store):
    """add() (the single-zone path) legitimately leaves complaint_id empty, so a
    blank lookup must not return every such constraint."""
    store.add(Constraint.from_issue("zone_b", "too_hot", 2, 0.9, T0))
    assert store.by_complaint("") == []
    assert store.by_complaint("cmp-9999") == []


def test_siblings_of_one_complaint_share_severity_confidence_and_text(store):
    made = store.add_many(["zone_d", "zone_e"], "too_hot", 3, 0.87, T0, "both hot",
                          "amy", "c1")
    assert {(c.severity, c.confidence, c.text, c.author) for c in made} == \
        {(3, 0.87, "both hot", "amy")}


# --------------------------------------------------------------------------
# 5 · the human-approval gate
# --------------------------------------------------------------------------

def test_a_pending_constraint_is_visible_but_moves_nothing(store):
    made = store.add_many(["zone_b"], "too_hot", 3, 0.95, T0, "boiling", "amy", "c1")
    for c in made:
        c.approved = False
    assert store.zone_adjustments(T0) == {}, "an unapproved constraint steered control"
    assert len(store.active(T0, "zone_b")) == 1, "it must still be visible in the UI"
    assert store.active(T0, "zone_b", counting_only=True) == []
    assert store.pending_approvals() == made
    assert store.explain(T0 and "zone_b", T0)["pending"] == 1


def test_approving_lets_the_constraint_through_without_resetting_its_clock(store):
    made = store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    made[0].approved = False
    assert store.approve(made[0].id) is True
    adj = store.zone_adjustments(T0 + HALF_LIFE_S)["zone_b"]
    # Approved 45 min late, so half the complaint is left: -1.3 * 0.5 weighting.
    assert adj["setpoint_offset"] == pytest.approx(-1.3), "the offset itself is undecayed"
    assert adj["weight"] == pytest.approx(2 * 0.9 * 0.5, abs=1e-3), \
        "approval wrongly reset the decay clock"
    assert store.pending_approvals() == []


def test_rejecting_keeps_the_history_but_never_moves_a_setpoint(store):
    made = store.add_many(["zone_b"], "too_hot", 3, 0.95, T0, "boiling", "amy", "c1")
    filed_at = made[0].created_t
    assert store.reject(made[0].id) is True
    assert store.zone_adjustments(T0) == {}
    assert store.pending_approvals() == [], "a rejected item is not still waiting"
    assert made[0].created_t == filed_at, \
        "reject() moved created_t and would corrupt the pattern miner"
    assert store.stats(T0)["rejected"] == 1
    assert made[0].counts() is False


def test_approve_and_reject_report_an_unknown_id(store):
    assert store.approve(999999) is False
    assert store.reject(999999) is False


def test_a_zone_whose_only_constraints_are_pending_is_omitted_entirely(store):
    made = store.add_many(["zone_b", "zone_c"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    made[0].approved = False                       # zone_b pending, zone_c approved
    adj = store.zone_adjustments(T0)
    assert set(adj) == {"zone_c"}, f"pending zone leaked into control: {set(adj)}"
    assert adj["zone_c"]["pending"] == 0


def test_partially_approved_zone_reports_the_pending_count(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    held = store.add_many(["zone_b"], "too_hot", 3, 0.95, T0, "boiling", "bob", "c2")
    held[0].approved = False
    adj = store.zone_adjustments(T0)["zone_b"]
    assert adj["n"] == 1, "the pending item was weighted"
    assert adj["pending"] == 1
    assert adj["setpoint_offset"] == pytest.approx(-1.3), "sev-3 leaked into the mean"


def test_unmet_pressure_ignores_constraints_the_controller_was_never_allowed_to_act_on(
        store, warm_twin):
    made = store.add_many(["zone_b"], "too_hot", 3, 1.0, warm_twin.t, "hot", "amy", "c1")
    warm_twin.T["zone_b"] = 27.0                   # still too hot: pressure is real
    assert store.unmet_pressure(warm_twin, 60.0) > 0.0
    made[0].approved = False
    assert store.unmet_pressure(warm_twin, 60.0) == 0.0, \
        "an unapproved complaint counted as unmet by a controller that could not act"


# --------------------------------------------------------------------------
# 6 · clear_zone
# --------------------------------------------------------------------------

def test_clear_zone_stops_the_influence_and_reports_the_count(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    store.add_many(["zone_b"], "stuffy", 1, 0.6, T0, "stuffy", "bob", "c2")
    store.add_many(["zone_c"], "too_cold", 2, 0.9, T0, "cold", "cal", "c3")
    now = T0 + 600.0
    assert store.clear_zone("zone_b", now) == 2
    assert store.active(now, "zone_b") == []
    assert len(store.active(now, "zone_c")) == 1, "an unrelated zone was cleared"
    assert len(store.items) == 3, "cleared constraints must stay in history"


def test_clear_zone_on_an_empty_or_unknown_zone_is_a_no_op(store):
    assert store.clear_zone("zone_b", T0) == 0
    assert store.clear_zone("zone_nope", T0) == 0


def test_clear_zone_does_not_resurrect_already_expired_constraints(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    late = T0 + EXPIRY_S + 600.0
    assert store.clear_zone("zone_b", late) == 0, \
        "an all-clear counted a complaint that had already expired on its own"


# --------------------------------------------------------------------------
# 7 · clone independence
# --------------------------------------------------------------------------

def test_clone_is_independent_in_both_directions(store):
    store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    clone = store.clone()
    assert clone.items[0] is not store.items[0], "clone shares Constraint objects"
    assert clone.items[0].id == store.items[0].id, "the clone describes the same complaint"

    clone.clear_zone("zone_b", T0)
    assert len(store.active(T0, "zone_b")) == 1, "clearing the clone cleared the original"

    store.add_many(["zone_c"], "too_cold", 2, 0.9, T0, "cold", "bob", "c2")
    assert len(clone.items) == 1, "the original leaked a new constraint into the clone"

    clone.items[0].approved = False
    assert store.items[0].approved is True, "approval flags are shared"


def test_clone_carries_the_objective(store):
    store.set_objective("energy")
    assert store.clone().objective == "energy"


def test_set_objective_falls_back_instead_of_raising(store):
    assert store.set_objective("energy") == "energy"
    assert store.set_objective("nonsense") == "balanced", \
        "a UI typo must not take the store down, but it must not stick either"


# --------------------------------------------------------------------------
# 8 · stats and views
# --------------------------------------------------------------------------

def test_stats_counts_by_zone_issue_and_lifecycle(store):
    store.add_many(["zone_b", "zone_c"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    held = store.add_many(["zone_b"], "too_cold", 1, 0.5, T0, "cold", "bob", "c2")
    held[0].approved = False
    st = store.stats(T0)
    assert (st["total"], st["active"], st["expired"], st["pending"], st["rejected"]) \
        == (3, 3, 0, 1, 0)
    assert st["by_zone"]["zone_b"] == {"total": 2, "active": 2}
    assert st["by_issue"]["too_hot"] == {"total": 2, "active": 2}

    later = store.stats(T0 + EXPIRY_S + 1.0)
    assert (later["active"], later["expired"]) == (0, 3)
    assert later["pending"] == 1, "a decayed request must still be dismissable"


def test_stats_on_an_empty_store_is_zeroed_not_absent(store):
    st = store.stats()
    assert st["total"] == 0 and st["active"] == 0 and st["by_zone"] == {}


def test_constraint_views_are_ordered_heaviest_first(store):
    store.add_many(["zone_b"], "too_hot", 1, 0.5, T0, "mild", "amy", "c1")
    store.add_many(["zone_b"], "too_hot", 3, 0.95, T0, "severe", "bob", "c2")
    views = store.constraint_views("zone_b", T0)
    assert [v["text"] for v in views] == ["severe", "mild"], \
        "row 0 must be the complaint actually driving the setpoint"
    assert views[0]["weight"] > views[1]["weight"]
    assert store.constraint_views("zone_nope", T0) == []


def test_a_view_never_leaks_an_anonymised_handle(store):
    made = store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "occupant-0daa", "c1")
    made[0].anonymous = True
    made[0].author = "rakshit"
    view = made[0].view(T0)
    assert view["author"] == "anonymous", f"pseudonym leaked: {view['author']}"
    assert "rakshit" not in str(view)


def test_view_shape_matches_the_pinned_constraint_view(store):
    from dataclasses import fields

    from backend.contracts import ConstraintView
    made = store.add_many(["zone_b"], "too_hot", 2, 0.9, T0, "hot", "amy", "c1")
    assert set(made[0].view(T0)) == {f.name for f in fields(ConstraintView)}


# --------------------------------------------------------------------------
# 9 · KNOWN DEFECTS — the back-dating family
# --------------------------------------------------------------------------

@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT D2: ConstraintStore.clear_zone() expires a constraint by MOVING ITS "
    "CLOCK — `c.created_t = now_t - EXPIRY_S - 1.0` — and created_t is the only "
    "timestamp ComfortMemory.patterns() has to cluster on (it derives both the day "
    "index and the hour-of-day from it). So every all-clear silently rewrites "
    "history: a complaint filed at 14:00 on Tuesday and retracted at 14:30 is "
    "recorded as having happened at 12:30, and one filed just after midnight moves "
    "to the PREVIOUS DAY. This test files the same complaint at 14:00 on two "
    "distinct days, so patterns() legitimately learns a 2-day 14:00 habit, then "
    "issues one all-clear: the surviving event moves to 12:30, falls outside "
    "TOLERANCE_H (1.0 h) of the cluster, splits into two 1-day clusters and the "
    "learned pattern DISAPPEARS. Fix: a separate `cleared` flag that decay() "
    "honours, so expiry never touches the clock. A multi-zone all-clear back-dates "
    "several constraints at once, so the damage per retraction is now wider."))
def test_clear_zone_must_not_corrupt_the_comfort_memory_timeline(store):
    from backend.memory import ComfortMemory
    day0_1400 = 14 * 3600.0
    day1_1400 = 86400.0 + 14 * 3600.0
    store.add_many(["zone_b"], "too_hot", 2, 0.9, day0_1400, "hot", "amy", "c1")
    store.add_many(["zone_b"], "too_hot", 2, 0.9, day1_1400, "hot", "amy", "c2")

    memory = ComfortMemory()
    learned = memory.patterns(store)
    assert learned and learned[0]["n_days"] == 2, "the fixture did not build a pattern"
    assert learned[0]["hour"] == pytest.approx(14.0)

    store.clear_zone("zone_b", day1_1400 + 1800.0)      # "it's fine now", 14:30

    kept = memory.patterns(store)
    assert kept and kept[0]["hour"] == pytest.approx(14.0), (
        "one all-clear destroyed a learned 2-day pattern; timestamps are now "
        f"{[(int(c.created_t // 86400), round((c.created_t % 86400) / 3600, 2)) for c in store.items]}")


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT D2b, same root cause in a second owner's file: "
    "backend.privacy.redact_record() also expires constraints by back-dating "
    "created_t (`c.created_t = now_t - expiry - 1.0`), and it does so for EVERY "
    "constraint matching the deleted record's text+author regardless of age — so a "
    "single forget-me request can rewrite the timestamps of a whole week of that "
    "occupant's history and collapse the pattern miner's clusters. The deletion "
    "itself is correct and must keep working; only the mechanism for expiring the "
    "derived constraints needs to become a flag rather than a clock move."))
def test_redact_record_must_not_corrupt_the_comfort_memory_timeline(store):
    from backend.memory import ComfortMemory
    from backend.privacy import record_id, redact_record
    day0_1400 = 14 * 3600.0
    day1_1400 = 86400.0 + 14 * 3600.0
    store.add_many(["zone_b"], "too_hot", 2, 0.9, day0_1400, "hot in b", "amy", "c1")
    store.add_many(["zone_b"], "too_hot", 2, 0.9, day1_1400, "hot in b", "amy", "c2")
    memory = ComfortMemory()
    assert memory.patterns(store)[0]["n_days"] == 2

    feed = [{"author": "amy", "text": "hot in b", "t": day1_1400}]
    assert redact_record(feed, record_id(feed[0]), store, day1_1400 + 1800.0) is True
    assert feed == [], "the record itself was not deleted"

    kept = memory.patterns(store)
    assert kept and kept[0]["hour"] == pytest.approx(14.0), (
        "redaction rewrote the constraint timeline; timestamps are now "
        f"{[(int(c.created_t // 86400), round((c.created_t % 86400) / 3600, 2)) for c in store.items]}")

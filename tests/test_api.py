"""Behavioural depth on the HTTP surface, driven through fastapi.testclient — no
port, no network, no uvicorn.

Two jobs, in this order of importance:

1. THE LEGACY /api/state CONTRACT, KEY BY KEY. dashboard/index.html reads 39 keys
   verbatim and nothing in backend/app.py may rename, retype or remove one. The
   spec is written out below as data so a rename fails with the key's own name in
   the message, and so a reviewer can diff the spec against the contract text
   instead of reading assertions.
2. EVERY ENDPOINT'S THREE PATHS: happy, bad input -> 4xx, unknown id -> 404. An
   endpoint with no tested error path is an endpoint whose error path does not
   exist.

Time is driven explicitly. conftest drops LiveSim.speed to its floor so the daemon
thread cannot move the building underneath an assertion, and anything that needs
the clock to advance calls sim.advance(minutes), which runs the identical
_step_once() the loop does.
"""
from __future__ import annotations

import json
import threading

import pytest

from sim.twin import ZONE_IDS

# --------------------------------------------------------------------------
# The pinned legacy contract, as data. Every entry is (key -> python type), and
# `None` in a type tuple means the key is legitimately nullable.
# --------------------------------------------------------------------------
LEGACY_SIM = {"clock": str, "hour": float, "t_out": float, "speed": float}
LEGACY_ZONE = {"id": str, "name": str, "temp": float, "base_temp": float,
               "setpoint": (float, type(None)), "vent": int, "occ": int,
               "offset": float, "active_constraints": int}
LEGACY_METER = {"kwh": float, "cost_rs": float, "co2_kg": float, "viol_min": float,
                "hot_deg_min": float, "cold_deg_min": float}
LEGACY_METERS_TOP = {"saved_kwh": float, "saved_pct": float, "saved_rs": float,
                     "saved_co2": float}
LEGACY_HISTORY = {"t": float, "us": float, "base": float}
LEGACY_FEED = {"author": str, "text": str, "source": str, "latency_ms": int,
               "parsed": dict, "action": str, "sim_clock": str}
LEGACY_KEY_COUNT = (len(LEGACY_SIM) + len(LEGACY_ZONE) + 2 * len(LEGACY_METER)
                    + len(LEGACY_METERS_TOP) + len(LEGACY_HISTORY) + len(LEGACY_FEED))


def check_shape(row, spec, where):
    for key, kind in spec.items():
        assert key in row, f"{where} lost the legacy key {key!r}"
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if float in kinds and int not in kinds:
            kinds = kinds + (int,)          # JSON collapses 25.0 to 25 sometimes
        assert isinstance(row[key], kinds), \
            f"{where}.{key} is {type(row[key]).__name__}, expected {kind}"


# ==========================================================================
# 1 · the legacy /api/state contract
# ==========================================================================

def test_state_returns_every_legacy_top_level_section(fresh_client):
    state = fresh_client.get("/api/state").json()
    for section in ("sim", "zones", "meters", "history", "feed"):
        assert section in state, f"/api/state lost the {section!r} section"
    assert isinstance(state["zones"], list) and isinstance(state["feed"], list)


def test_state_sim_block_matches_the_pinned_shape(fresh_client):
    check_shape(fresh_client.get("/api/state").json()["sim"], LEGACY_SIM, "sim")


def test_state_zone_rows_match_the_pinned_shape(fresh_client):
    zones = fresh_client.get("/api/state").json()["zones"]
    assert [z["id"] for z in zones] == ZONE_IDS, "the zone list or its order changed"
    for row in zones:
        check_shape(row, LEGACY_ZONE, f"zones[{row['id']}]")
        assert 0 <= row["vent"] <= 2
        assert row["occ"] >= 0 and row["active_constraints"] >= 0


def test_state_meters_match_the_pinned_shape(fresh_client):
    meters = fresh_client.get("/api/state").json()["meters"]
    check_shape(meters, LEGACY_METERS_TOP, "meters")
    check_shape(meters["us"], LEGACY_METER, "meters.us")
    check_shape(meters["base"], LEGACY_METER, "meters.base")


def test_state_history_rows_match_the_pinned_shape(fresh_client, live):
    live.advance(20)                                 # one 15-min analytics sample
    history = fresh_client.get("/api/state").json()["history"]
    assert history, "no energy history was sampled after 20 sim-minutes"
    for row in history:
        check_shape(row, LEGACY_HISTORY, "history[]")
    assert [r["t"] for r in history] == sorted(r["t"] for r in history), \
        "history is not in chronological order"


def test_state_feed_rows_match_the_pinned_shape(fresh_client):
    fresh_client.post("/api/complaint",
                      json={"text": "It's way too hot in Conference Room B",
                            "author": "judge"})
    feed = fresh_client.get("/api/state").json()["feed"]
    assert feed, "an applied complaint did not reach the occupant channel"
    check_shape(feed[0], LEGACY_FEED, "feed[0]")
    assert feed[0]["explanation"]["summary"], \
        "the dashboard reads feed[].explanation.summary for an applied complaint"


def test_the_whole_legacy_key_set_is_present_and_counted(fresh_client, live):
    """Belt and braces on the additive-only rule: 39 named legacy keys."""
    live.advance(20)
    fresh_client.post("/api/complaint", json={"text": "freezing in cabin c"})
    state = fresh_client.get("/api/state").json()
    missing = []
    for spec, row, where in (
            (LEGACY_SIM, state["sim"], "sim"),
            (LEGACY_ZONE, state["zones"][0], "zones[]"),
            (LEGACY_METER, state["meters"]["us"], "meters.us"),
            (LEGACY_METER, state["meters"]["base"], "meters.base"),
            (LEGACY_METERS_TOP, state["meters"], "meters"),
            (LEGACY_HISTORY, state["history"][0], "history[]"),
            (LEGACY_FEED, state["feed"][0], "feed[]")):
        missing += [f"{where}.{k}" for k in spec if k not in row]
    assert not missing, f"legacy keys missing: {missing}"
    assert LEGACY_KEY_COUNT == 39, "the pinned legacy contract itself changed size"


def test_state_is_json_serialisable_with_no_custom_encoder(fresh_client):
    state = fresh_client.get("/api/state").json()
    assert json.loads(json.dumps(state))["sim"]["clock"]


def test_the_additive_sections_are_all_present(fresh_client):
    state = fresh_client.get("/api/state").json()
    for section in ("controller", "alerts", "decisions", "analytics", "privacy",
                    "constraint_stats", "health"):
        assert section in state, f"/api/state is missing the additive {section!r} section"
    assert set(state["sim"]["conditions"]) == {"occ_scale", "capacity_scale",
                                              "solar_scale", "outdoor_offset",
                                              "humidity_offset"}
    for row in state["zones"]:
        for key in ("rh", "dew_point_c", "occ_pct", "capacity_pct", "locked_out",
                    "at_capacity", "conflict", "pending_constraints", "alerts"):
            assert key in row, f"zones[] is missing the additive key {key!r}"
        assert 0.0 <= row["capacity_pct"] <= 100.0
        assert row["dew_point_c"] <= row["temp"] + 0.5


def test_the_baseline_twin_really_is_running_alongside(fresh_client, live):
    """The A/B race is the dashboard's headline. Both twins must advance, and the
    baseline must be the more wasteful one."""
    live.advance(120)
    meters = fresh_client.get("/api/state").json()["meters"]
    assert meters["us"]["kwh"] > 0.0 and meters["base"]["kwh"] > 0.0
    assert meters["base"]["kwh"] > meters["us"]["kwh"], \
        f"the baseline stopped being wasteful: {meters['base']['kwh']} vs {meters['us']['kwh']}"
    assert meters["saved_kwh"] == pytest.approx(meters["base"]["kwh"] - meters["us"]["kwh"],
                                                abs=0.02)
    assert 0.0 < meters["saved_pct"] < 100.0


def test_health_reports_no_subsystem_failures_on_a_clean_run(fresh_client, live):
    live.advance(60)
    health = fresh_client.get("/api/state").json()["health"]
    assert health["errors"] == [], f"a subsystem failed during a clean run: {health['errors']}"
    assert health["steps"] >= 60


# ==========================================================================
# 2 · the complaint channel
# ==========================================================================

def test_a_multi_zone_complaint_creates_one_constraint_per_zone(fresh_client):
    body = fresh_client.post("/api/complaint",
                             json={"text": "The lobby and cafeteria are too hot",
                                   "author": "judge"}).json()
    assert body["action"] == "applied"
    assert body["zones"] == ["zone_d", "zone_e"]
    assert len(body["constraints"]) == 2
    assert body["complaint_id"].startswith("cmp-")
    assert len(body["explanations"]) == 2
    assert body["explanation"] == body["explanations"][0], "the legacy shape drifted"

    constraints = fresh_client.get("/api/constraints").json()
    assert len(constraints["active"]) == 2
    assert {c["zone"] for c in constraints["active"]} == {"zone_d", "zone_e"}
    assert {c["zone_name"] for c in constraints["active"]} == {"Lobby D", "Cafeteria E"}

    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    for zone in ("zone_d", "zone_e"):
        assert zones[zone]["active_constraints"] == 1
        assert zones[zone]["offset"] < 0.0, "a heat complaint did not lower the setpoint"


def test_an_all_clear_clears_exactly_the_zones_it_names(fresh_client):
    fresh_client.post("/api/complaint", json={"text": "The lobby and cafeteria are too hot"})
    fresh_client.post("/api/complaint", json={"text": "conference room b is freezing"})
    assert len(fresh_client.get("/api/constraints").json()["active"]) == 3

    body = fresh_client.post("/api/complaint",
                             json={"text": "all good in the lobby and cafeteria now"}).json()
    assert body["action"].startswith("all-clear")     # see the xfail on the action vocabulary
    assert body["cleared"] == 2
    assert body["cleared_by_zone"] == {"zone_d": 1, "zone_e": 1}

    left = fresh_client.get("/api/constraints").json()["active"]
    assert [c["zone"] for c in left] == ["zone_b"], \
        "the all-clear touched a zone the occupant did not name"


def test_a_negated_all_clear_also_clears(fresh_client):
    """Regression for defect D3 at the HTTP boundary: "no longer X" must clear, not
    file the opposite complaint."""
    fresh_client.post("/api/complaint", json={"text": "reception is really stuffy"})
    assert len(fresh_client.get("/api/constraints").json()["active"]) == 1
    body = fresh_client.post("/api/complaint", json={"text": "no longer stuffy in reception"}).json()
    assert body["action"].startswith("all-clear") and body["cleared"] == 1
    assert fresh_client.get("/api/constraints").json()["active"] == []


def test_a_non_comfort_message_is_ignored_and_changes_nothing(fresh_client):
    body = fresh_client.post("/api/complaint",
                             json={"text": "the projector in room b is broken"}).json()
    assert body["action"].startswith("ignored")
    assert fresh_client.get("/api/constraints").json()["active"] == []
    assert fresh_client.get("/api/state").json()["feed"][0]["action"].startswith("ignored")


def test_an_unaddressable_complaint_asks_which_zone(fresh_client):
    body = fresh_client.post("/api/complaint", json={"text": "it's hot in narnia"}).json()
    assert body["action"].startswith("clarify")
    assert "Conference Room B" in body["action"], "the clarify prompt must list the zones"
    assert fresh_client.get("/api/constraints").json()["active"] == [], \
        "a complaint with no zone still created a constraint somewhere"


def test_an_outdoor_statement_is_ignored(fresh_client):
    body = fresh_client.post("/api/complaint", json={"text": "it's boiling outside today"}).json()
    assert body["action"].startswith("ignored")


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: POST /api/complaint's docstring pins the output as \"action is one of "
    "applied / cleared / ignored / clarify / noted\", but four of those five never "
    "reach the client. Every return in LiveSim.handle_complaint is built as "
    "`{\"ok\": True, \"action\": \"<code>\", **entry}` and `entry` already carries "
    "its own long-form `action` (\"all-clear - 2 constraint(s) cleared in Lobby D, "
    "Cafeteria E\"), so the ** expansion SHADOWS the short code that comes before "
    "it. Only \"applied\" survives, because there the two spellings happen to be "
    "identical. Consequence: any client written against the documented vocabulary "
    "silently never matches - and one already works around it, "
    "app.slack_command does `action.startswith(\"all-clear\")` with the comment "
    "'# long form'. Fix: either put \"action\" AFTER the ** expansion, or give the "
    "feed entry a separate key (e.g. entry[\"action_text\"]) so the machine-readable "
    "code and the human sentence stop competing for one name."))
@pytest.mark.parametrize("text,expected", [
    ("the projector in room b is broken", "ignored"),
    ("it's hot in narnia", "clarify"),
    ("everything is fine now, thanks", "noted"),
    ("all good in the lobby now", "cleared"),
])
def test_the_complaint_action_vocabulary_matches_its_docstring(fresh_client, text, expected):
    body = fresh_client.post("/api/complaint", json={"text": text}).json()
    assert body["action"] == expected, f"{text!r} -> {body['action']!r}"


def test_the_one_action_code_that_does_survive_is_applied(fresh_client):
    """"applied" is the single member of the documented vocabulary that reaches the
    client, because there the short code and the feed's human sentence are the same
    string. It is also the one the live dashboard branches on, which is why the
    shadowing bug above has gone unnoticed."""
    body = fresh_client.post("/api/complaint",
                             json={"text": "It's way too hot in Conference Room B"}).json()
    assert body["action"] == "applied"


def test_empty_and_missing_text_are_rejected(fresh_client):
    assert fresh_client.post("/api/complaint", json={"text": "   "}).status_code == 400
    assert fresh_client.post("/api/complaint", json={"text": ""}).status_code == 400
    assert fresh_client.post("/api/complaint", json={}).status_code == 422
    assert fresh_client.post("/api/complaint", json={"text": 17}).status_code == 422


def test_pii_is_scrubbed_before_the_text_is_stored_or_parsed(fresh_client):
    body = fresh_client.post("/api/complaint", json={
        "text": "room b is too hot, call me on 9876543210 or rakshit@example.com",
        "author": "rakshit", "anonymous": True}).json()
    assert "9876543210" not in body["text"] and "rakshit@example.com" not in body["text"]
    assert "hot" in body["text"], "scrubbing destroyed the complaint itself"
    assert body["redacted"], "the scrubber did not report what it removed"
    assert body["author"] != "rakshit", "the handle was not anonymised"
    assert body["action"] == "applied", "scrubbing broke the parse"
    stored = json.dumps(fresh_client.get("/api/state").json())
    assert "9876543210" not in stored and "rakshit@example.com" not in stored


def test_the_offline_parser_is_what_answered(fresh_client):
    """conftest strips the provider keys, so every request must report source
    "rules" and disclose that nothing left the building."""
    body = fresh_client.post("/api/complaint", json={"text": "cabin c is freezing"}).json()
    assert body["source"] == "rules"
    assert body["external_ai"] is False
    disclosure = fresh_client.get("/api/state").json()["privacy"]["ai_disclosure"]
    assert disclosure["external"] is False and disclosure["text"]


def test_the_slack_webhook_answers_in_the_shape_slack_expects(fresh_client):
    body = fresh_client.post("/api/slack",
                             data={"text": "room b is too hot", "user_name": "amy"}).json()
    assert body["response_type"] == "in_channel"
    assert "Conference Room B" in body["text"] and "too hot" in body["text"]
    empty = fresh_client.post("/api/slack", data={"text": "", "user_name": "amy"}).json()
    assert empty["response_type"] == "ephemeral"


# ==========================================================================
# 3 · conditions
# ==========================================================================

def test_conditions_apply_to_both_twins_so_the_race_stays_fair(fresh_client, live):
    body = fresh_client.post("/api/conditions", json={"outdoor_offset": 6.0}).json()
    assert body["ok"] is True
    assert body["conditions"]["outdoor_offset"] == 6.0
    assert body["clamped"] == []
    with live.lock:
        assert live.us.outdoor_offset == 6.0 and live.base.outdoor_offset == 6.0, \
            "a heat wave was applied to one twin only, rigging the A/B race"
    state = fresh_client.get("/api/state").json()
    assert state["sim"]["conditions"]["outdoor_offset"] == 6.0
    fresh_client.post("/api/conditions", json={"outdoor_offset": 0.0})


def test_out_of_range_knobs_are_clamped_and_the_clamp_is_reported(fresh_client):
    body = fresh_client.post("/api/conditions",
                             json={"occ_scale": 99.0, "capacity_scale": -5.0}).json()
    assert body["conditions"]["occ_scale"] == 3.0
    assert body["conditions"]["capacity_scale"] == 0.1
    assert set(body["clamped"]) == {"occ_scale", "capacity_scale"}
    fresh_client.post("/api/conditions", json={"occ_scale": 1.0, "capacity_scale": 1.0})


def test_conditions_rejects_an_empty_body_and_an_unknown_knob(fresh_client):
    assert fresh_client.post("/api/conditions", json={}).status_code == 400
    assert fresh_client.post("/api/conditions", json={"bogus": 1}).status_code == 422
    assert fresh_client.post("/api/conditions", json={"occ_scale": "hot"}).status_code == 422


# ==========================================================================
# 4 · controller
# ==========================================================================

def test_switching_objective_re_derives_the_schedule_and_records_it(fresh_client, live):
    body = fresh_client.post("/api/controller", json={"objective": "energy"}).json()
    assert body["objective"] == "energy"
    assert body["schedule"] == {"occupied": 25.9, "precool": 26.9,
                                "unoccupied": 29.0, "lead_h": 0.25}
    with live.lock:
        assert live.store.objective == "energy", "the store was not told the objective"
    back = fresh_client.post("/api/controller", json={"objective": "balanced"}).json()
    assert back["schedule"]["occupied"] == 25.0


def test_locking_a_zone_promotes_the_effective_mode_and_unlocking_restores_it(fresh_client):
    body = fresh_client.post("/api/controller", json={"lock_zone": "zone_b"}).json()
    assert body["locked_zones"] == ["zone_b"]
    assert body["operator_locked_zones"] == ["zone_b"]
    assert body["safety_mode"] == "maintenance_lockout", \
        "a lock that changes nothing is a fake control"
    assert body["requested_safety_mode"] == "automatic", \
        "a settings panel must be able to tell the promotion from the operator's choice"

    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["locked_out"] is True
    assert zones["zone_a"]["locked_out"] is False

    body = fresh_client.post("/api/controller", json={"unlock_zone": "zone_b"}).json()
    assert body["locked_zones"] == []
    assert body["safety_mode"] == "automatic", "the operator's mode was not restored"


def test_an_explicit_safety_mode_survives_a_lock(fresh_client):
    fresh_client.post("/api/controller", json={"safety_mode": "recommend_only"})
    body = fresh_client.post("/api/controller", json={"lock_zone": "zone_b"}).json()
    assert body["safety_mode"] == "recommend_only", \
        "an operator who deliberately chose a mode was overridden"
    assert body["note"], "the mismatch between lock and mode was not explained"
    fresh_client.post("/api/controller", json={"unlock_zone": "zone_b",
                                               "safety_mode": "automatic"})


def test_controller_rejects_every_kind_of_bad_input(fresh_client):
    assert fresh_client.post("/api/controller", json={}).status_code == 400
    assert fresh_client.post("/api/controller", json={"objective": "cheapest"}).status_code == 400
    assert fresh_client.post("/api/controller", json={"safety_mode": "yolo"}).status_code == 400
    assert fresh_client.post("/api/controller", json={"lock_zone": "zone_z"}).status_code == 400
    assert fresh_client.post("/api/controller", json={"unlock_zone": "zone_z"}).status_code == 400
    assert fresh_client.post("/api/controller", json={"bogus": "x"}).status_code == 422


def test_the_controller_payload_advertises_its_own_vocabulary(fresh_client):
    body = fresh_client.get("/api/state").json()["controller"]
    assert set(body["objectives"]) == {"comfort", "energy", "cost", "carbon", "balanced"}
    assert set(body["safety_modes"]) == {"automatic", "recommend_only", "human_approval",
                                         "emergency_override", "maintenance_lockout"}
    assert all(isinstance(v, str) and v for v in body["safety_modes"].values())


# ==========================================================================
# 5 · decisions
# ==========================================================================

def test_the_decision_log_fills_up_and_can_be_filtered_and_fetched(fresh_client, live):
    fresh_client.post("/api/complaint", json={"text": "the lobby is really too hot"})
    live.advance(10)

    body = fresh_client.get("/api/decisions").json()
    assert body["decisions"], "10 sim-minutes with a live complaint logged no decision"
    assert body["total"] >= len(body["decisions"])
    row = body["decisions"][0]
    for key in ("id", "zone", "reason_code", "summary", "constraints", "objective",
                "safety_mode", "new_setpoint", "base_setpoint", "est_energy_delta_pct"):
        assert key in row, f"a decision record is missing {key!r}"
    assert row["summary"], "a decision with no summary explains nothing"

    filtered = fresh_client.get("/api/decisions", params={"zone": "zone_d"}).json()
    assert filtered["zone"] == "zone_d"
    assert all(d["zone"] == "zone_d" for d in filtered["decisions"])

    one = fresh_client.get(f"/api/decisions/{row['id']}")
    assert one.status_code == 200
    assert one.json()["decision"]["id"] == row["id"]


def test_decisions_rejects_a_bad_zone_or_limit_and_404s_an_unknown_id(fresh_client):
    assert fresh_client.get("/api/decisions", params={"zone": "zone_z"}).status_code == 400
    assert fresh_client.get("/api/decisions", params={"limit": 0}).status_code == 400
    assert fresh_client.get("/api/decisions", params={"limit": 501}).status_code == 400
    assert fresh_client.get("/api/decisions", params={"limit": "abc"}).status_code == 422
    assert fresh_client.get("/api/decisions/dec-999999").status_code == 404


# ==========================================================================
# 6 · constraints, approval and rejection
# ==========================================================================

def test_constraints_returns_empty_but_valid_shapes_on_a_fresh_building(fresh_client):
    body = fresh_client.get("/api/constraints").json()
    assert body["active"] == [] and body["pending"] == []
    assert body["stats"]["total"] == 0
    assert body["safety_mode"] in ("automatic", "maintenance_lockout")
    assert isinstance(body["now_t"], (int, float))


def test_active_constraints_are_ordered_heaviest_first_per_zone(fresh_client):
    fresh_client.post("/api/complaint", json={"text": "slightly warm in room b"})
    fresh_client.post("/api/complaint", json={"text": "room b is so hot i cant work"})
    active = [c for c in fresh_client.get("/api/constraints").json()["active"]
              if c["zone"] == "zone_b"]
    assert len(active) == 2
    assert active[0]["weight"] >= active[1]["weight"], \
        "row 0 must be the complaint actually driving the setpoint"
    assert active[0]["severity"] == 3


@pytest.fixture
def pending_constraint(fresh_client, live):
    """One filed-but-unapproved constraint. Nothing in the shipped API creates one
    (add_many approves by default), so human_approval mode is set up here directly
    on the live store — the endpoint under test is still the real one."""
    fresh_client.post("/api/complaint", json={"text": "conference room b is too hot"})
    with live.lock:
        constraint = live.store.items[-1]
        constraint.approved = False
    return constraint.id


def test_a_pending_constraint_is_listed_and_moves_nothing(fresh_client, pending_constraint):
    body = fresh_client.get("/api/constraints").json()
    assert [c["id"] for c in body["pending"]] == [pending_constraint]
    assert body["pending"][0]["zone_name"] == "Conference Room B"
    assert body["stats"]["pending"] == 1
    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["offset"] == 0.0, "an unapproved complaint steered control"
    assert zones["zone_b"]["active_constraints"] == 1, \
        "the withheld complaint vanished from the zone row entirely"


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: /api/state -> zones[].pending_constraints reads 0 in exactly the case "
    "the field exists for. app.zone_rows() derives it from "
    "ConstraintStore.zone_adjustments(), which documents that it OMITS a zone whose "
    "constraints are all pending ('exactly as if nothing had been filed'), so "
    "`adj` is None and the row falls back to the literal 0. The count only appears "
    "when the zone ALSO has an approved constraint to be weighted alongside. So in "
    "human_approval mode, a zone with one complaint waiting shows 'pending 0' on "
    "the very panel an operator is meant to act from. GET /api/constraints reports "
    "it correctly (stats.pending and the pending[] list), and "
    "ConstraintStore.explain() computes it independently of zone_adjustments, so "
    "the fix is to read the count from explain(zone, t)['pending'] (or count "
    "pending_approvals per zone) rather than from the arbitration result."))
def test_a_zone_whose_only_complaint_is_pending_still_reports_the_count(
        fresh_client, pending_constraint):
    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["pending_constraints"] == 1, \
        "the operator's own panel says nothing is waiting"


def test_approving_a_constraint_lets_it_steer_control(fresh_client, pending_constraint):
    body = fresh_client.post(f"/api/constraints/{pending_constraint}/approve").json()
    assert body["ok"] is True and body["approved"] == pending_constraint
    assert body["pending"] == []
    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["offset"] < 0.0, "approval did not reach the controller"


def test_rejecting_a_constraint_keeps_it_in_history_but_not_in_control(
        fresh_client, pending_constraint):
    body = fresh_client.post(f"/api/constraints/{pending_constraint}/reject").json()
    assert body["ok"] is True and body["rejected"] == pending_constraint
    assert body["pending"] == [], "a rejected request is not still waiting"
    assert body["stats"]["rejected"] == 1
    assert body["stats"]["total"] == 1, "rejecting deleted the history"
    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["offset"] == 0.0


def test_approve_and_reject_404_an_unknown_id_and_422_a_non_integer(fresh_client):
    assert fresh_client.post("/api/constraints/999999/approve").status_code == 404
    assert fresh_client.post("/api/constraints/999999/reject").status_code == 404
    assert fresh_client.post("/api/constraints/not-an-int/approve").status_code == 422


# ==========================================================================
# 7 · what-if
# ==========================================================================

def test_the_scenario_registry_is_self_describing(fresh_client):
    body = fresh_client.get("/api/scenarios").json()
    assert len(body["scenarios"]) >= 8
    for entry in body["scenarios"]:
        assert set(entry) == {"key", "label", "kind", "params", "help"}
        assert entry["help"], f"{entry['key']} has no plain-language help"
    assert body["default_horizon_h"] > 0 and body["default_seeds"]
    assert "normal approximation" in body["ci95_note"]
    assert body["metrics"]["kwh"] == {"verb": "uses", "unit": "kWh", "direction": "lower"}


def test_a_whatif_run_is_measured_isolated_and_leaves_the_building_alone(fresh_client):
    before = fresh_client.get("/api/state").json()
    body = fresh_client.post("/api/whatif",
                             json={"scenario": "outdoor_hotter", "horizon_h": 0.5}).json()
    assert body["isolation_verified"] is True, "the engine could not prove isolation"
    assert body["snapshot"]["fingerprint"] and body["snapshot"]["clock"]
    assert body["baseline"]["kind"] == body["scenario"]["kind"] == "measured"
    assert body["delta"]["kwh"]["abs"] > 0.0, "a heat wave cost nothing"
    assert body["headline"]

    after = fresh_client.get("/api/state").json()
    assert after["meters"]["us"]["kwh"] == before["meters"]["us"]["kwh"], \
        "the what-if run charged energy to the live building"
    assert after["constraint_stats"]["total"] == before["constraint_stats"]["total"]


def test_a_whatif_run_can_price_the_complaints_that_are_actually_live(fresh_client):
    fresh_client.post("/api/complaint", json={"text": "The lobby and cafeteria are too hot"})
    body = fresh_client.post("/api/whatif",
                             json={"scenario": "ignore_complaint", "horizon_h": 1.0}).json()
    assert body["isolation_verified"] is True
    assert any(row["abs"] != 0.0 for row in body["delta"].values()), \
        "ignoring two live complaints measured no difference at all"
    assert len(fresh_client.get("/api/constraints").json()["active"]) == 2, \
        "the scenario dropped the LIVE complaints, not the clone's"


def test_a_multi_seed_run_reports_a_confidence_interval(fresh_client):
    body = fresh_client.post("/api/whatif", json={"scenario": "outdoor_hotter",
                                                  "horizon_h": 0.5,
                                                  "seeds": [7, 8, 9]}).json()
    assert [r["seed"] for r in body["scenario"]["per_seed"]] == [7, 8, 9]
    assert body["scenario"]["ci95"]["kwh"] > 0.0


def test_whatif_rejects_every_kind_of_bad_request(fresh_client):
    bad = [
        ({"scenario": "teleport"}, 400),
        ({"scenario": "setpoint_up", "horizon_h": 0}, 400),
        ({"scenario": "setpoint_up", "horizon_h": -3}, 400),
        ({"scenario": "setpoint_up", "horizon_h": 99}, 400),
        ({"scenario": "setpoint_up", "seeds": []}, 400),
        ({"scenario": "setpoint_up", "seeds": [1, 2, 3, 4, 5, 6]}, 400),
        ({"scenario": "setpoint_up", "seeds": [-1]}, 400),
        ({"scenario": "setpoint_up", "bogus": 1}, 422),
        ({}, 422),
    ]
    for body, expected in bad:
        got = fresh_client.post("/api/whatif", json=body).status_code
        assert got == expected, f"{body} -> {got}, expected {expected}"


# ==========================================================================
# 8 · analytics, maintenance, experiments
# ==========================================================================

def test_analytics_returns_full_zeroed_shapes_before_any_samples(fresh_client):
    body = fresh_client.get("/api/analytics").json()
    for section in ("heatmap", "energy", "complaints", "controller", "summary"):
        assert body[section] is not None, f"analytics.{section} was null, not empty-but-valid"
    assert isinstance(body["samples"], int)
    assert json.loads(json.dumps(body)) == body


def test_analytics_fills_in_once_the_building_has_run(fresh_client, live):
    fresh_client.post("/api/complaint", json={"text": "the lobby is too hot"})
    live.advance(60)
    body = fresh_client.get("/api/analytics").json()
    assert body["samples"] >= 1, "an hour of simulation produced no analytics sample"
    assert body["complaints"], "the occupant-feed breakdown is empty after a complaint"


def test_maintenance_reports_no_alerts_on_a_healthy_building(fresh_client, live):
    live.advance(30)
    body = fresh_client.get("/api/maintenance").json()
    assert body["alerts"] == [], f"a healthy building raised alerts: {body['alerts']}"
    assert body["suppressed_zones"] == []
    assert body["interval_s"] > 0
    assert isinstance(body["history"], list)


def test_a_starved_coil_raises_a_capacity_alert_and_suppresses_setpoint_chasing(
        fresh_client, live):
    """The only place the maintenance monitor and the controller meet: a real
    capacity alert must both surface AND stop the controller chasing a setpoint the
    coil cannot reach."""
    fresh_client.post("/api/conditions", json={"capacity_scale": 0.1, "outdoor_offset": 8.0})
    try:
        live.advance(90)
        body = fresh_client.get("/api/maintenance").json()
        assert body["alerts"], "a 10% coil in a heat wave raised no alert at all"
        kinds = {a["kind"] for a in body["alerts"]}
        assert "capacity" in kinds, f"alert kinds were {kinds}"
        for alert in body["alerts"]:
            assert alert["evidence"], "an alert with no evidence is not actionable"
            assert alert["recommendation"]
            assert 0.0 <= alert["confidence"] <= 1.0
            assert alert["severity"] in ("low", "medium", "high")

        state = fresh_client.get("/api/state").json()
        assert state["alerts"], "/api/state did not surface the alerts"
        alerted = {z["id"] for z in state["zones"] if z["alerts"]}
        assert alerted, "no zone row carried its alert"
        if body["suppressed_zones"]:
            assert state["controller"]["safety_mode"] == "maintenance_lockout", \
                "a suppressed zone did not promote the effective safety mode"
            locked = {z["id"] for z in state["zones"] if z["locked_out"]}
            assert locked == set(body["suppressed_zones"])
    finally:
        fresh_client.post("/api/conditions", json={"capacity_scale": 1.0,
                                                   "outdoor_offset": 0.0})


def test_experiments_never_500s_and_always_reports_availability(fresh_client):
    body = fresh_client.get("/api/experiments").json()
    assert "available" in body and isinstance(body["available"], bool)
    assert body["path"], "the payload does not say where the file should be"
    if body["available"]:
        assert body["scenarios"], "available=true with no scenarios in it"
    else:
        assert body["note"], "available=false with no explanation of how to fix it"


# ==========================================================================
# 9 · privacy
# ==========================================================================

def test_export_returns_the_messages_and_the_constraints_derived_from_them(fresh_client):
    fresh_client.post("/api/complaint", json={"text": "room b is too hot", "author": "amy"})
    body = fresh_client.get("/api/export").json()
    assert body["counts"]["records"] >= 1
    assert body["counts"]["constraints"] >= 1, \
        "'we deleted your message but kept the setpoint it caused' is not deletion"
    assert body["schema"] and body["notes"]
    assert all("id" in r for r in body["records"])


def test_export_can_be_scoped_to_one_author_and_validates_it(fresh_client):
    fresh_client.post("/api/complaint", json={"text": "room b is too hot", "author": "amy"})
    fresh_client.post("/api/complaint", json={"text": "cabin c is freezing", "author": "bob"})
    amy = fresh_client.get("/api/export", params={"author": "amy"}).json()
    assert amy["counts"]["records"] == 1, f"author filter leaked: {amy['counts']}"
    assert fresh_client.get("/api/export", params={"author": " "}).status_code == 400
    assert fresh_client.get("/api/export", params={"author": "x" * 65}).status_code == 400


def test_redact_removes_the_record_and_reaches_the_control_layer(fresh_client):
    fresh_client.post("/api/complaint", json={"text": "room b is too hot", "author": "amy"})
    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["offset"] < 0.0, "fixture never influenced control"

    entry_id = fresh_client.get("/api/export").json()["records"][0]["id"]
    body = fresh_client.post("/api/redact", json={"entry_id": entry_id}).json()
    assert body["ok"] is True and body["entry_id"] == entry_id

    zones = {z["id"]: z for z in fresh_client.get("/api/state").json()["zones"]}
    assert zones["zone_b"]["offset"] == 0.0, \
        "the message was deleted but the setpoint it caused was not released"
    remaining = json.dumps(fresh_client.get("/api/export").json())
    assert "room b is too hot" not in remaining


def test_redact_404s_an_unknown_id_and_400s_a_blank_one(fresh_client):
    assert fresh_client.post("/api/redact", json={"entry_id": "rec-nope"}).status_code == 404
    assert fresh_client.post("/api/redact", json={"entry_id": "  "}).status_code == 400
    assert fresh_client.post("/api/redact", json={}).status_code == 422


# ==========================================================================
# 10 · the guided demo
# ==========================================================================

def test_reading_the_demo_position_never_advances_it(fresh_client):
    first = fresh_client.get("/api/demo").json()
    assert first["total"] >= 5 and first["started"] is False
    again = fresh_client.get("/api/demo").json()
    assert again["index"] == first["index"] and again["started"] is False


def test_the_demo_walks_forwards_and_backwards_without_repeating_its_actions(fresh_client):
    start = fresh_client.post("/api/demo", json={"action": "start"}).json()
    assert start["index"] == 0 and start["started"] is True and start["applied"] is True
    assert start["title"] and start["narration"]

    second = fresh_client.post("/api/demo", json={"action": "next"}).json()
    assert second["index"] == 1
    back = fresh_client.post("/api/demo", json={"action": "prev"}).json()
    assert back["index"] == 0, "prev did not move back"
    again = fresh_client.post("/api/demo", json={"action": "prev"}).json()
    assert again["index"] == 0, "prev walked off the front of the script"

    reset = fresh_client.post("/api/demo", json={"action": "reset"}).json()
    assert reset["started"] is False and reset["index"] == 0


def test_a_demo_step_performs_a_real_action(fresh_client):
    """Step 1 sets the weather knobs for real, so /api/state must agree with it."""
    fresh_client.post("/api/demo", json={"action": "reset"})
    fresh_client.post("/api/demo", json={"action": "start"})
    conditions = fresh_client.get("/api/state").json()["sim"]["conditions"]
    assert conditions["outdoor_offset"] != 0.0 or conditions["occ_scale"] != 1.0, \
        "the first demo step changed nothing in the real building"
    fresh_client.post("/api/demo", json={"action": "reset"})


def test_the_demo_rejects_an_unknown_action_or_field(fresh_client):
    assert fresh_client.post("/api/demo", json={"action": "fly"}).status_code == 400
    assert fresh_client.post("/api/demo", json={"action": "next", "x": 1}).status_code == 422


# ==========================================================================
# 11 · reset, pages, and the D1 race
# ==========================================================================

def test_reset_rebuilds_the_state_but_keeps_the_operator_configuration(client, live):
    client.post("/api/controller", json={"objective": "energy",
                                         "safety_mode": "recommend_only"})
    client.post("/api/speed", json={"speed": 1})
    client.post("/api/complaint", json={"text": "The lobby and cafeteria are too hot"})
    live.advance(60)

    before = client.get("/api/state").json()
    assert before["constraint_stats"]["total"] >= 2 and before["feed"]
    assert before["meters"]["us"]["kwh"] > 0.0

    after = client.post("/api/reset").json()
    assert after["constraint_stats"]["total"] == 0, "the constraint store survived a reset"
    assert after["feed"] == [], "the occupant channel survived a reset"
    assert after["history"] == [], "the energy history survived a reset"
    assert after["meters"]["us"]["kwh"] == 0.0, "the meters were not rebuilt"
    assert after["sim"]["hour"] < before["sim"]["hour"], "the clock was not rewound"
    assert after["alerts"] == [] and after["health"]["errors"] == []

    assert after["controller"]["objective"] == "energy", \
        "the operator's objective is configuration and must survive a reset"
    assert after["controller"]["requested_safety_mode"] == "recommend_only"
    assert after["sim"]["speed"] == 1.0
    client.post("/api/controller", json={"objective": "balanced",
                                         "safety_mode": "automatic"})


def test_reset_also_rewinds_the_guided_demo(client):
    client.post("/api/demo", json={"action": "start"})
    assert client.get("/api/demo").json()["started"] is True
    client.post("/api/reset")
    assert client.get("/api/demo").json()["started"] is False, \
        "a fresh building was left with a half-walked script"


def test_the_dashboard_and_its_static_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/index.html").status_code == 200


def test_the_occupant_page_404s_with_a_useful_message_until_it_exists(client):
    response = client.get("/occupant")
    assert response.status_code in (200, 404)
    if response.status_code == 404:
        assert "occupant.html" in response.json()["detail"], \
            "a missing page must say which file is missing"


def test_polling_state_while_the_sim_steps_never_tears_a_response(client, live):
    """Regression for defect D1. The sim thread appends to history and rebinds the
    feed while FastAPI serialises a response, so handing out an internal container
    by reference is a real "list changed size during iteration" mid-demo. 120 polls
    against 120 interleaved advances, each taking the lock briefly.
    """
    failures = []

    def drive():
        try:
            for _ in range(120):
                live.advance(1)
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            failures.append(f"advance: {exc!r}")

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    for _ in range(120):
        response = client.get("/api/state")
        if response.status_code != 200:
            failures.append(f"GET /api/state -> {response.status_code}: {response.text[:200]}")
            break
        payload = response.json()
        assert isinstance(payload["history"], list) and isinstance(payload["feed"], list)
    worker.join(timeout=60)
    assert not failures, "state was served while it was being mutated:\n" + "\n".join(failures)

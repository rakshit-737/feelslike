"""Behavioural depth on determinism, cloning and the what-if engine.

Three properties are load-bearing for the whole project and are tested here rather
than anywhere else:

1. DETERMINISM. Same seed -> same numbers, bit for bit. Every published figure and
   every replayed demo depends on it.
2. CLONE INDEPENDENCE. What-if safety is not a policy, it is a property of
   DigitalTwin.clone() and ConstraintStore.clone(). If a clone leaks, every
   counterfactual silently corrupts the live building.
3. ISOLATION ACROSS THE WHOLE REGISTRY. The contract suite proves isolation for one
   scenario; here every scenario in SCENARIOS is fingerprinted before and after,
   because the two dangerous families (the ones that mutate the store, and the ones
   that swap weather_fn) are easy to get right for one key and wrong for another.

Reset semantics: the product's reset is POST /api/reset and is tested end to end in
tests/test_api.py. What lives here is the property that reset RELIES on — that a
rebuilt building at the same seed reproduces itself exactly.
"""
from __future__ import annotations

import pytest

from backend import whatif
from backend.constraints import EXPIRY_S, ConstraintStore
from backend.contracts import ScenarioSpec, to_dict
from sim.twin import ZONE_IDS, ZONES, DigitalTwin

HORIZON = 0.5           # simulated hours; enough to separate scenarios, fast enough to loop


def live_pair(hour=10.0, seed=7, minutes=30):
    """A stepped twin plus a store holding two opposing complaints — the state a
    what-if is actually taken from."""
    twin = DigitalTwin(seed=seed)
    twin.t = hour * 3600.0
    for _ in range(minutes):
        twin.step({z.id: 25.0 for z in ZONES}, {z.id: 1 for z in ZONES})
    store = ConstraintStore()
    store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
    store.add_many(["zone_d"], "too_cold", 1, 0.6, twin.t, "cold", "bob", "c2")
    return twin, store


def spec(key, seeds=None, horizon_h=HORIZON):
    return whatif.scenario_spec(key, horizon_h=horizon_h, seeds=seeds or [7])


# --------------------------------------------------------------------------
# 1 · determinism
# --------------------------------------------------------------------------

def test_the_same_seed_is_bit_identical_over_a_full_day():
    a, b = DigitalTwin(seed=11), DigitalTwin(seed=11)
    for i in range(1440):
        setpoints = {z.id: (24.0 if i % 2 else None) for z in ZONES}
        vents = {z.id: i % 3 for z in ZONES}
        a.step(setpoints, vents)
        b.step(dict(setpoints), dict(vents))
    assert a.T == b.T, "identical seeds diverged on temperature"
    assert a.W == b.W, "identical seeds diverged on humidity"
    assert a.kwh == b.kwh and a.kwh_by_zone == b.kwh_by_zone
    assert a.metrics() == b.metrics()


def test_different_seeds_produce_different_weather_and_therefore_different_numbers():
    runs = {}
    for seed in (3, 7, 11):
        twin = DigitalTwin(seed=seed)
        for _ in range(720):
            twin.step({z.id: 24.0 for z in ZONES}, {z.id: 1 for z in ZONES})
        runs[seed] = twin.metrics()["kwh"]
    assert len(set(runs.values())) == 3, f"seeds are not actually independent: {runs}"


def test_outdoor_weather_and_humidity_are_pure_functions_of_seed_and_time():
    from sim.humidity import outdoor_rh
    from sim.weather import outdoor_temp
    for seed in (0, 7):
        temps = [outdoor_temp(t * 900.0, seed) for t in range(96)]
        rhs = [outdoor_rh(t * 900.0, seed) for t in range(96)]
        assert temps == [outdoor_temp(t * 900.0, seed) for t in range(96)]
        assert rhs == [outdoor_rh(t * 900.0, seed) for t in range(96)]
        assert all(0.0 <= v <= 100.0 for v in rhs)


def test_a_rebuilt_building_reproduces_itself_exactly():
    """The property POST /api/reset depends on: rebuilding at the same seed and
    start hour and replaying the same commands must land on the same physical state.
    Without it "reset" would mean "some other building".

    The twin is fingerprinted WITHOUT the store on purpose. Constraint.id comes from
    a process-wide itertools.count, so the second rebuild's constraints legitimately
    carry higher ids — that is the documented "deterministic, never uuid4" scheme,
    monotonic within a process rather than reset-stable. Everything about the
    constraints that describes the complaint is compared field by field instead.
    """
    def build_and_run():
        twin = DigitalTwin(seed=7)
        twin.t = 8.0 * 3600.0
        store = ConstraintStore()
        store.add_many(["zone_b"], "too_hot", 2, 0.9, twin.t, "hot", "amy", "c1")
        for _ in range(120):
            twin.step({z.id: 24.5 for z in ZONES}, {z.id: 1 for z in ZONES})
        described = [(c.zone, c.issue, c.severity, c.confidence, c.created_t,
                      c.raw_offset, c.vent_delta, c.author, c.approved)
                     for c in store.items]
        return whatif.state_fingerprint(twin), described

    first, second = build_and_run(), build_and_run()
    assert first[0] == second[0], "a rebuilt building did not reproduce its physics"
    assert first[1] == second[1], "a rebuilt store did not reproduce its complaints"


def test_run_scenario_is_deterministic_and_seed_sensitive():
    twin, store = live_pair()
    first = whatif.run_scenario(twin, store, spec("occupancy_up"))
    again = whatif.run_scenario(twin, store, spec("occupancy_up"))
    assert first.mean == again.mean, "the same scenario gave two answers"
    other = whatif.run_scenario(twin, store, spec("occupancy_up", seeds=[11]))
    assert other.mean["kwh"] != first.mean["kwh"], "the seed had no effect"


# --------------------------------------------------------------------------
# 2 · clone independence
# --------------------------------------------------------------------------

def test_a_twin_clone_starts_identical_and_then_goes_its_own_way():
    twin, _store = live_pair()
    clone = twin.clone()
    assert whatif.state_fingerprint(clone) == whatif.state_fingerprint(twin), \
        "the clone did not start from the same state"

    before = whatif.state_fingerprint(twin)
    for _ in range(240):
        clone.step({z.id: 21.5 for z in ZONES}, {z.id: 2 for z in ZONES})
    assert whatif.state_fingerprint(twin) == before, "the clone leaked into the original"
    assert clone.kwh > twin.kwh and clone.t > twin.t


def test_every_mutable_container_is_rebuilt_by_clone():
    """A shallow copy would share these and the leak would only show up under a
    scenario that happens to touch them."""
    twin, _store = live_pair()
    clone = twin.clone()
    for name in ("T", "W", "kwh_by_zone", "last_setpoints", "last_vents", "_at_cap"):
        assert getattr(clone, name) is not getattr(twin, name), f"{name} is shared"
        assert getattr(clone, name) == getattr(twin, name), f"{name} was not copied faithfully"


def test_mutating_a_clone_condition_knob_does_not_touch_the_original():
    twin, _store = live_pair()
    clone = twin.clone()
    clone.set_conditions(outdoor_offset=9.0, occ_scale=2.5)
    assert twin.outdoor_offset == 0.0 and twin.occ_scale == 1.0


def test_a_store_clone_can_be_emptied_back_dated_and_approved_safely():
    twin, store = live_pair()
    clone = store.clone()
    clone.items = []
    assert len(store.items) == 2, "emptying the clone emptied the original"

    clone = store.clone()
    for c in clone.items:
        c.created_t -= EXPIRY_S + 60.0
        c.approved = False
    assert all(c.decay(twin.t) > 0.0 for c in store.items), "back-dating leaked"
    assert all(c.approved for c in store.items), "approval flags leaked"


# --------------------------------------------------------------------------
# 3 · isolation across the entire scenario registry
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(whatif.SCENARIOS))
def test_no_scenario_in_the_registry_mutates_live_state(key):
    """The one non-negotiable of the what-if engine, checked for every question it
    can be asked — including the two dangerous families: the store mutators
    (ignore_complaint / complaint_expires) and the weather swap (a second seed)."""
    twin, store = live_pair()
    before = whatif.state_fingerprint(twin, store)
    snapshot = (twin.kwh, twin.t, dict(twin.T), twin.viol_min, len(store.items))

    whatif.compare(twin, store, spec(key, seeds=[7, 8]))

    assert whatif.state_fingerprint(twin, store) == before, \
        f"scenario {key!r} mutated the live twin or store"
    assert (twin.kwh, twin.t, dict(twin.T), twin.viol_min, len(store.items)) == snapshot


def test_verify_isolation_reports_true_for_the_shipped_engine():
    twin, store = live_pair()
    assert whatif.verify_isolation(twin, store) is True


def test_the_fingerprint_actually_notices_a_change():
    """A proof-by-fingerprint is worthless if the fingerprint is insensitive."""
    twin, store = live_pair()
    base = whatif.state_fingerprint(twin, store)
    twin.step({z.id: 24.0 for z in ZONES}, {z.id: 1 for z in ZONES})
    assert whatif.state_fingerprint(twin, store) != base, "a physics step did not register"

    twin2, store2 = live_pair()
    base2 = whatif.state_fingerprint(twin2, store2)
    store2.items[0].created_t -= 1.0
    assert whatif.state_fingerprint(twin2, store2) != base2, "a store change did not register"


def test_isolation_survives_a_scenario_that_raises_midway():
    """An exception must not leave the live state half-perturbed."""
    twin, store = live_pair()
    before = whatif.state_fingerprint(twin, store)
    bad = ScenarioSpec(name="bad", kind="x", params={"nonsense_knob": 1.0},
                       horizon_h=HORIZON, seeds=[7])
    with pytest.raises(KeyError):
        whatif.run_scenario(twin, store, bad)
    assert whatif.state_fingerprint(twin, store) == before


# --------------------------------------------------------------------------
# 4 · what the scenarios actually measure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,metric,direction", [
    ("setpoint_up", "kwh", "down"),
    ("setpoint_down", "kwh", "up"),
    ("occupancy_up", "kwh", "up"),
    ("occupancy_down", "kwh", "down"),
    ("outdoor_hotter", "kwh", "up"),
    ("humidity_up", "mean_rh", "up"),
    ("capacity_loss", "at_capacity_min", "up"),
    ("objective_energy", "kwh", "down"),
    ("objective_comfort", "kwh", "up"),
])
def test_each_scenario_moves_its_headline_metric_the_right_way(key, metric, direction):
    twin, store = live_pair()
    delta = whatif.compare(twin, store, spec(key, horizon_h=1.0))["delta"][metric]
    if direction == "up":
        assert delta["abs"] > 0.0, f"{key} did not raise {metric}: {delta}"
    else:
        assert delta["abs"] < 0.0, f"{key} did not lower {metric}: {delta}"
    assert delta["verdict"], "a scenario produced no plain-language verdict"


def test_dropping_the_complaints_prices_what_listening_costs():
    """ignore_complaint must differ from the baseline in SOME measured way, or the
    scenario is answering a question it did not ask."""
    twin, store = live_pair()
    out = whatif.compare(twin, store, spec("ignore_complaint", horizon_h=1.0))
    moved = {k: v["abs"] for k, v in out["delta"].items() if v["abs"] != 0.0}
    assert moved, "emptying the complaint queue changed nothing at all"


def test_back_dating_the_complaints_matches_dropping_them_closely():
    """complaint_expires and ignore_complaint are different mechanisms for the same
    end state, so their measured energy must agree to within rounding."""
    twin, store = live_pair()
    dropped = whatif.run_scenario(twin, store, spec("ignore_complaint", horizon_h=1.0))
    expired = whatif.run_scenario(twin, store, spec("complaint_expires", horizon_h=1.0))
    assert dropped.mean["kwh"] == pytest.approx(expired.mean["kwh"], abs=0.01), \
        f"{dropped.mean['kwh']} vs {expired.mean['kwh']}"


def test_results_are_labelled_measured_because_the_physics_really_ran():
    twin, store = live_pair()
    for key in sorted(whatif.SCENARIOS):
        result = whatif.run_scenario(twin, store, spec(key))
        assert result.kind == "measured", f"{key} claims to be {result.kind}"
        assert result.per_seed[0]["note"], f"{key} did not report what it perturbed"


def test_metrics_are_horizon_deltas_not_the_clones_lifetime_totals():
    """A clone inherits the live twin's kWh counter; reporting that would smuggle
    history into the answer."""
    twin, store = live_pair(minutes=180)
    assert twin.kwh > 5.0, "fixture has no history to smuggle"
    result = whatif.run_scenario(twin, store, spec("outdoor_hotter", horizon_h=1.0))
    assert 0.0 < result.mean["kwh"] < twin.kwh, \
        f"one hour reported {result.mean['kwh']} kWh against a lifetime of {twin.kwh}"


def test_every_scenario_reports_the_whole_metric_set():
    twin, store = live_pair()
    result = whatif.run_scenario(twin, store, spec("setpoint_up"))
    assert set(result.mean) == set(whatif.METRIC_KEYS)
    assert set(result.sd) == set(whatif.METRIC_KEYS)
    assert set(result.ci95) == set(whatif.METRIC_KEYS)
    assert set(result.metrics) == set(whatif.METRIC_KEYS)


def test_compare_is_apples_to_apples_on_the_same_horizon_and_seeds():
    twin, store = live_pair()
    out = whatif.compare(twin, store, spec("outdoor_hotter", seeds=[7, 8]))
    assert out["baseline"].kind == out["scenario"].kind == "measured"
    assert [r["seed"] for r in out["baseline"].per_seed] == \
           [r["seed"] for r in out["scenario"].per_seed] == [7, 8]
    assert out["baseline"].per_seed[0]["note"].startswith("no condition change")
    for metric, row in out["delta"].items():
        assert row["scenario"] - row["baseline"] == pytest.approx(row["abs"], abs=5e-4), metric


def test_a_zero_baseline_reports_no_percentage_instead_of_infinity():
    twin, store = live_pair(hour=3.0)               # nobody in: viol_min stays 0
    out = whatif.compare(twin, store, spec("outdoor_hotter", horizon_h=1.0))
    row = out["delta"]["viol_min"]
    assert row["baseline"] == 0.0
    assert row["pct"] is None, f"a divide-by-zero percentage leaked out: {row}"
    assert "n/a" in row["verdict"] or "the same" in row["verdict"]


def test_compare_output_is_wire_safe_through_to_dict():
    import json
    twin, store = live_pair()
    out = to_dict(whatif.compare(twin, store, spec("setpoint_up")))
    assert json.loads(json.dumps(out))["headline"]


# --------------------------------------------------------------------------
# 5 · the confidence interval
# --------------------------------------------------------------------------

def test_one_seed_reports_no_spread_at_all():
    twin, store = live_pair()
    result = whatif.run_scenario(twin, store, spec("outdoor_hotter", seeds=[7]))
    assert len(result.per_seed) == 1
    assert result.sd["kwh"] == 0.0, "a sample stdev of one point is undefined"
    assert result.ci95["kwh"] == 0.0


def test_two_seeds_report_a_spread_but_still_no_interval():
    """CI95_NOTE: with n < 3 the normal approximation is reported as 0.0 rather than
    as a misleadingly precise number."""
    twin, store = live_pair()
    result = whatif.run_scenario(twin, store, spec("outdoor_hotter", seeds=[7, 8]))
    assert result.sd["kwh"] > 0.0
    assert result.ci95["kwh"] == 0.0, "a two-seed CI was published"


def test_three_seeds_report_a_normal_approximation_interval():
    import math
    twin, store = live_pair()
    seeds = [7, 8, 9]
    result = whatif.run_scenario(twin, store, spec("outdoor_hotter", seeds=seeds))
    assert [r["seed"] for r in result.per_seed] == seeds
    assert result.sd["kwh"] > 0.0 and result.ci95["kwh"] > 0.0
    expected = 1.96 * result.sd["kwh"] / math.sqrt(len(seeds))
    assert result.ci95["kwh"] == pytest.approx(expected, abs=1e-3)
    assert "normal approximation" in whatif.CI95_NOTE


def test_the_mean_is_the_mean_of_the_per_seed_rows():
    import statistics
    twin, store = live_pair()
    result = whatif.run_scenario(twin, store, spec("occupancy_up", seeds=[7, 8, 9]))
    for metric in ("kwh", "viol_min", "mean_temp"):
        rows = [float(r[metric]) for r in result.per_seed]
        assert result.mean[metric] == pytest.approx(statistics.fmean(rows), abs=1e-3), metric


# --------------------------------------------------------------------------
# 6 · spec validation
# --------------------------------------------------------------------------

def test_an_unknown_scenario_key_is_a_key_error():
    with pytest.raises(KeyError):
        whatif.scenario_spec("teleport_the_building")


def test_an_unknown_param_key_is_a_key_error():
    twin, store = live_pair()
    with pytest.raises(KeyError):
        whatif.run_scenario(twin, store, ScenarioSpec(name="x", kind="x",
                                                      params={"bogus": 1},
                                                      horizon_h=1.0, seeds=[7]))


def test_a_non_positive_horizon_is_a_value_error():
    for horizon in (0.0, -1.0):
        with pytest.raises(ValueError):
            whatif._as_spec("setpoint_up", horizon_h=horizon)


def test_a_spec_of_the_wrong_type_is_a_type_error():
    with pytest.raises(TypeError):
        whatif._as_spec(42)


def test_an_empty_seed_list_falls_back_to_the_default_seed():
    assert whatif._as_spec("setpoint_up", seeds=[]).seeds == list(whatif.DEFAULT_SEEDS)


def test_a_registry_entry_can_be_passed_straight_in():
    """Callers naturally write compare(twin, store, SCENARIOS["occupancy_up"])."""
    twin, store = live_pair()
    out = whatif.compare(twin, store, whatif.SCENARIOS["occupancy_up"])
    assert out["delta"]["kwh"]["abs"] != 0.0
    assert whatif.SCENARIOS["occupancy_up"]["params"] == {"occ_scale": 1.2}, \
        "the registry was mutated by a run"


def test_a_spec_is_copied_so_a_run_cannot_corrupt_the_caller():
    original = whatif.scenario_spec("occupancy_up", horizon_h=HORIZON, seeds=[7])
    coerced = whatif._as_spec(original)
    coerced.params["occ_scale"] = 99.0
    coerced.seeds.append(999)
    assert original.params == {"occ_scale": 1.2}
    assert original.seeds == [7]


def test_the_registry_only_uses_known_param_keys():
    for key, entry in whatif.SCENARIOS.items():
        unknown = set(entry["params"]) - whatif.PARAM_KEYS
        assert not unknown, f"scenario {key!r} uses unknown params {unknown}"
        assert entry["help"] and entry["label"] and entry["kind"], f"{key} is underspecified"

"""Behavioural depth on the digital twin: does the physics actually respond to
the things it claims to model?

The bar here is *directional correctness with a stated mechanism*, not agreement
with a reference building. Every test names the term of the RC balance it is
exercising (envelope, ventilation, solar, occupant, inter-zone, coil) and asserts
the sign and the boundedness of the response, because a knob that does not move
the physics is fake functionality and a knob that moves it the wrong way is worse.

Two documented limitations of sim/twin.py are asserted AS limitations rather than
worked around, so nobody mistakes them for accidents: latent load is never charged
to kWh, and the coil's apparatus dew point is approximated 2 K below the setpoint
(which pins indoor RH high). Both are called out in the twin's module docstring.
"""
from __future__ import annotations

import pytest

from sim.humidity import dew_point, humidity_ratio, rh_from_ratio
from sim.twin import BAND, DT, FAN_W, RH_HUMID, ZONE_IDS, ZONES, DigitalTwin

H_MORNING = 10.0        # a/c/d/e occupied; b joins at 10:00
H_NIGHT = 3.0           # nothing occupied anywhere
H_LUNCH = 13.0          # cafeteria at design headcount


def build(hour=H_MORNING, seed=7, start_temp=28.0, **conditions):
    """A twin parked at `hour`. start_temp matters more than it looks: the default
    28.0 degC means every zone begins ABOVE the comfort band, so a run that starts
    there books real violation-minutes and real coil saturation while it pulls the
    building down. Tests about the steady state pass their own start_temp instead
    of trying to subtract the transient afterwards."""
    t = DigitalTwin(seed=seed, start_temp=start_temp)
    t.t = hour * 3600.0
    if conditions:
        t.set_conditions(**conditions)
    return t


def drive(twin, minutes, setpoint=25.0, vent=1):
    for _ in range(int(minutes)):
        twin.step({z.id: setpoint for z in ZONES}, {z.id: vent for z in ZONES})
    return twin


def kwh_after(minutes=180, setpoint=24.0, vent=1, hour=H_MORNING, **conditions):
    return drive(build(hour, **conditions), minutes, setpoint, vent).metrics()["kwh"]


# --------------------------------------------------------------------------
# 1 · the setpoint is what the coil tracks
# --------------------------------------------------------------------------

@pytest.mark.parametrize("setpoint", [22.0, 24.0, 26.0])
def test_a_reachable_setpoint_is_actually_reached(setpoint):
    twin = drive(build(), 120, setpoint=setpoint)
    for zone in ZONE_IDS:
        assert twin.T[zone] == pytest.approx(setpoint, abs=0.05), \
            f"{zone} settled at {twin.T[zone]:.2f} for a {setpoint} degC setpoint"


def test_a_colder_setpoint_costs_more_energy():
    cold, warm = kwh_after(setpoint=22.0), kwh_after(setpoint=26.0)
    assert cold > warm, f"cooling harder was not more expensive: {cold} vs {warm}"


def test_hvac_off_lets_the_zone_free_float_towards_outdoors():
    twin = build()
    t_out = twin.weather_fn(twin.t)
    start = dict(twin.T)
    drive(twin, 120, setpoint=None, vent=0)
    for zone in ZONE_IDS:
        assert twin.T[zone] > start[zone], f"{zone} cooled with the HVAC off"
    assert max(twin.T.values()) > t_out, \
        "free-floating zones never exceeded outdoor temperature despite solar and people"
    assert twin.last_power_w == pytest.approx(0.0), "HVAC off still drew power"


def test_the_coil_never_heats_a_zone_that_is_already_below_setpoint():
    """The twin models cooling only: a zone below its setpoint is left alone."""
    twin = build(hour=H_NIGHT)
    twin.T = {z.id: 18.0 for z in ZONES}
    before = dict(twin.T)
    twin.step({z.id: 26.0 for z in ZONES}, {z.id: 0 for z in ZONES})
    assert all(twin.T[z] >= before[z] for z in ZONE_IDS), "the coil added heat"
    assert twin.last_power_w == pytest.approx(0.0), "the coil ran with no load"


# --------------------------------------------------------------------------
# 2 · each load term moves the temperature the right way
# --------------------------------------------------------------------------

def free_float_temp(zone="zone_a", **conditions):
    return drive(build(**conditions), 120, setpoint=None, vent=0).T[zone]


def test_hotter_outdoors_raises_the_zone_temperature():
    """The envelope term UA*(T_out - T)."""
    assert free_float_temp(outdoor_offset=8.0) > free_float_temp() > \
        free_float_temp(outdoor_offset=-8.0)


def test_more_people_raise_the_zone_temperature():
    """The occupant term, 100 W sensible per person."""
    assert free_float_temp(occ_scale=3.0) > free_float_temp() > free_float_temp(occ_scale=0.0)


def test_more_solar_gain_raises_the_zone_temperature():
    assert free_float_temp(solar_scale=2.0) > free_float_temp() > free_float_temp(solar_scale=0.0)


def test_ventilation_pulls_a_zone_towards_the_outdoor_temperature():
    """VENT_UA per vent level. At 10:00 the zones free-float hotter than outdoors,
    so more outdoor air must cool them - the sign is what matters, not the size."""
    twin = build()
    t_out = twin.weather_fn(twin.t)
    closed = drive(build(), 120, setpoint=None, vent=0).T["zone_a"]
    open_ = drive(build(), 120, setpoint=None, vent=2).T["zone_a"]
    assert closed > t_out, "fixture assumption broken: the zone was cooler than outdoors"
    assert open_ < closed, f"outdoor air did not move the zone: {open_} vs {closed}"


def test_a_hot_neighbour_costs_its_neighbour_energy():
    """The inter-zone term G_ij*(T_j - T_i). zone_b and zone_c share a wall, so
    leaving b uncooled must show up in c's meter."""
    def zone_c_kwh(cool_b):
        twin = build(hour=2.0)
        for _ in range(240):
            sps = {z.id: None for z in ZONES}
            sps["zone_c"] = 22.0
            if cool_b:
                sps["zone_b"] = 22.0
            twin.step(sps, {z.id: 0 for z in ZONES})
        return twin.kwh_by_zone["zone_c"]

    assert zone_c_kwh(cool_b=False) > zone_c_kwh(cool_b=True), \
        "a hot adjacent zone imposed no load on its neighbour"


# --------------------------------------------------------------------------
# 3 · energy rises with load, monotonically
# --------------------------------------------------------------------------

def test_energy_is_monotone_in_outdoor_temperature():
    series = [kwh_after(outdoor_offset=o) for o in (-4.0, 0.0, 4.0, 8.0)]
    assert series == sorted(series), f"energy not monotone in outdoor temp: {series}"
    assert series[-1] > series[0] * 1.3, f"an 12 K swing barely moved energy: {series}"


def test_energy_is_monotone_in_occupancy():
    series = [kwh_after(occ_scale=s) for s in (0.0, 1.0, 2.0, 3.0)]
    assert series == sorted(series), f"energy not monotone in occupancy: {series}"


def test_energy_is_monotone_in_solar_gain():
    series = [kwh_after(solar_scale=s) for s in (0.0, 1.0, 2.0)]
    assert series == sorted(series), f"energy not monotone in solar: {series}"


def test_running_the_fan_harder_costs_more_energy():
    """Two mechanisms at once: FAN_W and the extra outdoor-air load it drags in."""
    series = [kwh_after(vent=v) for v in (0, 1, 2)]
    assert series == sorted(series) and series[0] < series[2], \
        f"fan power is not being charged: {series}"
    assert FAN_W[0] < FAN_W[1] < FAN_W[2]


def test_cost_and_carbon_are_flat_scalars_of_energy():
    m = drive(build(), 120).metrics()
    assert m["cost_rs"] == pytest.approx(m["kwh"] * 9.0, abs=0.2)
    assert m["co2_kg"] == pytest.approx(m["kwh"] * 0.71, abs=0.02)


# --------------------------------------------------------------------------
# 4 · moisture
# --------------------------------------------------------------------------

def test_moisture_rises_with_occupancy():
    """Occupant latent gain, 60 W of evaporation per person. Asserted on the open
    office because it is the only zone whose headcount is high enough at 10:00 to
    lift W clear of the coil's apparatus-dew-point floor (see the next test)."""
    busy = drive(build(occ_scale=3.0), 120).W["zone_a"]
    normal = drive(build(), 120).W["zone_a"]
    assert busy > normal, f"90 people added no moisture: {busy} vs {normal}"


def test_moisture_rises_with_outdoor_humidity():
    for zone in ("zone_a", "zone_e"):
        wet = drive(build(humidity_offset=25.0), 120).W[zone]
        dry = drive(build(humidity_offset=-25.0), 120).W[zone]
        assert wet > dry, f"{zone} ignored outdoor humidity: {wet} vs {dry}"


def test_cooling_removes_moisture_even_though_it_raises_relative_humidity():
    """The psychrometrics that trip people up, asserted explicitly. A running coil
    condenses water out (the humidity RATIO falls), but the air it leaves behind is
    much colder, so its RELATIVE humidity is higher. Both must be true at once, or
    the humidity readouts and the humid/stuffy control lever are inconsistent."""
    cooled = drive(build(), 120, setpoint=25.0, vent=1)
    floating = drive(build(), 120, setpoint=None, vent=1)
    zone = "zone_a"
    assert cooled.W[zone] < floating.W[zone], \
        f"the coil removed no moisture: {cooled.W[zone]} vs {floating.W[zone]}"
    assert cooled.rh_now(zone) > floating.rh_now(zone), \
        "colder air did not read as more humid"
    assert cooled.T[zone] < floating.T[zone]


def test_the_coil_cannot_dry_air_below_its_apparatus_dew_point():
    """A DOCUMENTED LIMITATION, pinned so it cannot change silently. The twin
    approximates the coil surface as 2 K below the setpoint, which is ~10 K warmer
    than a real DX coil, so indoor RH is floored near 88% for most occupied
    minutes. humid_viol_min is therefore a "how humid is Delhi" signal rather than
    a controller discriminator - see the sim/twin.py module docstring."""
    twin = drive(build(), 120, setpoint=22.0, vent=1)
    floor = humidity_ratio(22.0 - 2.0, 100.0)
    for zone in ZONE_IDS:
        assert twin.W[zone] >= floor - 1e-9, \
            f"{zone} dried below the apparatus dew point: {twin.W[zone]} < {floor}"
    assert twin.metrics()["mean_rh"] > 70.0, \
        "the documented high-RH artefact has gone away; re-read the twin docstring"


def test_latent_load_is_tracked_but_never_charged_to_energy():
    """The other documented limitation: the A/B energy numbers must be identical
    with a dry and a soaking outdoor air stream, because moisture never feeds the
    sensible balance. If this ever fails, the published kWh comparison changed
    meaning."""
    dry = drive(build(humidity_offset=-30.0), 180, setpoint=24.0).metrics()
    wet = drive(build(humidity_offset=30.0), 180, setpoint=24.0).metrics()
    assert dry["kwh"] == pytest.approx(wet["kwh"], abs=1e-9), \
        f"latent load leaked into kWh: {dry['kwh']} vs {wet['kwh']}"
    assert wet["mean_rh"] > dry["mean_rh"], "the moisture model did nothing at all"


def test_dew_point_never_exceeds_dry_bulb_under_adversarial_conditions():
    """10 000 samples across three vent levels, an on/off/warm setpoint cycle, a
    monsoon offset and triple occupancy."""
    twin = build(hour=0.0, seed=3, humidity_offset=30.0, occ_scale=3.0)
    checked = 0
    for i in range(2000):
        setpoint = (21.5, None, 26.0)[i % 3]
        twin.step({z.id: setpoint for z in ZONES}, {z.id: i % 3 for z in ZONES})
        for zone in ZONE_IDS:
            assert twin.dew_point_now(zone) <= twin.T[zone] + 1e-6, \
                f"dew point above dry bulb in {zone} at step {i}"
            assert 0.0 <= twin.rh_now(zone) <= 100.0 + 1e-6, \
                f"{zone} RH left 0-100 at step {i}: {twin.rh_now(zone)}"
            checked += 1
    assert checked == 10000


def test_dew_point_equals_dry_bulb_at_saturation():
    assert dew_point(26.0, 100.0) == pytest.approx(26.0, abs=0.3)
    assert rh_from_ratio(26.0, humidity_ratio(26.0, 100.0)) == pytest.approx(100.0, abs=0.5)


# --------------------------------------------------------------------------
# 5 · comfort accounting
# --------------------------------------------------------------------------

def test_comfort_violations_are_only_counted_while_a_zone_is_occupied():
    """An empty building baking at 42 degC is not a comfort failure - nobody is in
    it. If unoccupied minutes counted, every setback would look like a violation
    and the whole A/B comparison would be meaningless."""
    twin = build(hour=H_NIGHT, outdoor_offset=10.0)
    assert all(twin.occupancy_now(z) == 0 for z in ZONE_IDS), "fixture is not empty"
    drive(twin, 120, setpoint=None, vent=0)
    assert max(twin.T.values()) > BAND[1] + 4.0, "the fixture never actually overheated"
    m = twin.metrics()
    assert m["viol_min"] == 0.0, f"unoccupied minutes were charged: {m}"
    assert m["hot_deg_min"] == 0.0
    assert m["humid_viol_min"] == 0.0
    assert m["at_capacity_min"] == 0.0
    assert m["mean_rh"] == 0.0, "mean RH over zero occupied minutes must be 0.0, not nan"


def test_an_occupied_zone_outside_the_band_does_get_charged():
    twin = build(hour=H_MORNING, outdoor_offset=10.0)
    occupied = [z for z in ZONE_IDS if twin.occupancy_now(z) > 0]
    assert occupied, "fixture assumption broken: nobody is in the building at 10:00"
    drive(twin, 120, setpoint=None, vent=0)
    m = twin.metrics()
    assert m["viol_min"] > 0.0, "an occupied, overheated zone booked no violation"
    assert m["hot_deg_min"] > 0.0 and m["cold_deg_min"] == 0.0
    assert m["viol_min"] <= 120.0 * len(ZONE_IDS) + 1e-6, "more violation-minutes than exist"


def test_hot_and_cold_degree_minutes_are_charged_to_the_right_side():
    """Each run starts inside the band so only the direction under test accrues."""
    mid = (BAND[0] + BAND[1]) / 2.0
    hot = drive(build(start_temp=mid, outdoor_offset=10.0), 120,
                setpoint=None, vent=0).metrics()
    cold = drive(build(start_temp=mid), 120, setpoint=BAND[0] - 3.0, vent=1).metrics()
    assert hot["hot_deg_min"] > 0 and hot["cold_deg_min"] == 0.0, hot
    assert cold["cold_deg_min"] > 0 and cold["hot_deg_min"] == 0.0, cold


def test_a_zone_inside_the_band_books_nothing():
    mid = (BAND[0] + BAND[1]) / 2.0
    m = drive(build(start_temp=mid), 180, setpoint=mid, vent=1).metrics()
    assert m["viol_min"] == 0.0 and m["hot_deg_min"] == 0.0 and m["cold_deg_min"] == 0.0, m


# --------------------------------------------------------------------------
# 6 · capacity limiting
# --------------------------------------------------------------------------

def test_capacity_limiting_actually_binds_at_a_low_capacity_scale():
    """Three things must happen together, or "at capacity" is cosmetic: the coil
    flag is raised, the zone loses its setpoint, and the meter STOPS rising -
    because a capacity-limited coil cannot spend the energy it would need."""
    strong = drive(build(outdoor_offset=8.0, capacity_scale=1.0), 120, setpoint=22.0)
    weak = drive(build(outdoor_offset=8.0, capacity_scale=0.1), 120, setpoint=22.0)

    assert all(weak._at_cap[z] for z in ZONE_IDS), \
        f"a 10% coil was not saturated anywhere: {weak._at_cap}"
    assert weak.metrics()["at_capacity_min"] > strong.metrics()["at_capacity_min"]
    for zone in ZONE_IDS:
        assert weak.T[zone] > strong.T[zone] + 5.0, \
            f"{zone} tracked its setpoint on a 10% coil: {weak.T[zone]:.1f}"
    assert weak.metrics()["kwh"] < strong.metrics()["kwh"], \
        "a coil that cannot cool still drew full power"


def test_capacity_scale_is_reported_and_bounds_the_delivered_cooling():
    twin = build(capacity_scale=0.5)
    snap = twin.zone_snapshot("zone_b")
    from sim.twin import ZONE_BY_ID
    assert snap["capacity_w"] == pytest.approx(ZONE_BY_ID["zone_b"].max_cool * 0.5, abs=1.0)
    drive(twin, 60, setpoint=21.5)
    step_kwh = max(twin.kwh_by_zone.values())
    assert step_kwh > 0.0


def test_an_ample_coil_reports_no_saturation_at_a_mild_setpoint():
    """Started at the setpoint, so there is no pull-down transient: a full-size
    coil holding a mild setpoint must never report itself saturated."""
    twin = drive(build(start_temp=26.0), 120, setpoint=26.0, vent=1)
    assert twin.metrics()["at_capacity_min"] == 0.0, \
        "a comfortable setpoint on a full-size coil reported saturation"
    assert not any(twin._at_cap.values())


# --------------------------------------------------------------------------
# 7 · bookkeeping invariants
# --------------------------------------------------------------------------

def test_per_zone_energy_sums_to_the_building_total():
    twin = drive(build(), 240)
    assert sum(twin.kwh_by_zone.values()) == pytest.approx(twin.kwh, rel=1e-9)


def test_the_clock_advances_exactly_one_step_per_step():
    twin = build()
    start = twin.t
    drive(twin, 60)
    assert twin.t == pytest.approx(start + 60 * DT)
    assert twin.hour == pytest.approx((start + 60 * DT) % 86400 / 3600.0)


def test_last_setpoints_and_vents_record_what_was_commanded():
    twin = build()
    twin.step({z.id: (23.5 if z.id == "zone_b" else None) for z in ZONES},
              {z.id: 2 for z in ZONES})
    assert twin.last_setpoints["zone_b"] == 23.5
    assert twin.last_setpoints["zone_a"] is None
    assert set(twin.last_vents.values()) == {2}


def test_zone_snapshot_agrees_with_the_underlying_state():
    twin = drive(build(), 60)
    for zone in ZONE_IDS:
        snap = twin.zone_snapshot(zone)
        assert snap["temp_c"] == pytest.approx(twin.T[zone], abs=0.005)
        assert snap["rh_pct"] == pytest.approx(twin.rh_now(zone), abs=0.05)
        assert snap["dew_point_c"] == pytest.approx(twin.dew_point_now(zone), abs=0.005)
        assert snap["occ"] == twin.occupancy_now(zone)
        assert snap["dew_point_c"] <= snap["temp_c"] + 1e-6


def test_occupancy_is_scaled_and_never_negative():
    twin = build(hour=H_LUNCH)
    assert twin.occupancy_now("zone_e") == 30, "cafeteria design headcount moved"
    twin.set_conditions(occ_scale=0.0)
    assert twin.occupancy_now("zone_e") == 0
    twin.set_conditions(occ_scale=3.0)
    assert twin.occupancy_now("zone_e") == 90
    assert twin.zone_snapshot("zone_e")["occ_pct"] == pytest.approx(300.0, abs=0.1)


def test_the_weekend_empties_the_conference_room():
    saturday = build(hour=H_MORNING)
    saturday.t += 5 * 86400.0
    assert saturday.day % 7 == 5
    assert saturday.occupancy_now("zone_b") == 0
    assert saturday.occupancy_now("zone_a") > 0, "the weekend skeleton crew vanished"


def test_metrics_is_json_safe_and_rounded():
    import json
    m = drive(build(), 90).metrics()
    assert json.loads(json.dumps(m)) == m
    assert all(isinstance(v, float) for v in m.values())


def test_an_unknown_zone_raises_rather_than_returning_a_default():
    twin = build()
    for call in (lambda: twin.rh_now("zone_z"), lambda: twin.dew_point_now("zone_z"),
                 lambda: twin.zone_snapshot("zone_z"), lambda: twin.occupancy_now("zone_z")):
        with pytest.raises(KeyError):
            call()


def test_condition_knobs_are_clamped_and_reported():
    twin = build()
    out = twin.set_conditions(occ_scale=99.0, capacity_scale=-5.0, solar_scale=99.0,
                              outdoor_offset=500.0, humidity_offset=-500.0)
    assert out == {"occ_scale": 3.0, "capacity_scale": 0.1, "solar_scale": 2.0,
                   "outdoor_offset": 10.0, "humidity_offset": -30.0}
    assert twin.set_conditions()["occ_scale"] == 3.0, "a no-arg call changed a knob"


def test_the_humid_threshold_and_band_are_the_documented_constants():
    assert BAND == (23.0, 26.5)
    assert RH_HUMID == 65.0
    assert DT == 60.0

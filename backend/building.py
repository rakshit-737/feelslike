"""Commercial building profiles, demand model and executive KPIs (Building tab).

WHAT THIS MODULE IS. The digital twin (sim/twin.py) models ONE physical thing:
five zones of an office-like floor plate. A facility manager thinks in a larger
frame — "this is a hospital, 6 floors, open 24 h, CO2 must stay under 800 ppm,
how hard is the building working right now versus what I expect at this hour?".
This module is that frame. It is a pure, lock-free projection layer:

  BuildingConfig   the operator's configuration (one per LiveSim, survives a
                   reset because it is a choice, not state). Built from one of
                   six PROFILES and validated field-by-field.
  topology()       Building -> floors -> zones. The five twin zones are mapped
                   onto floors; floors/zones the twin does not model are listed
                   as NOT MODELLED, never populated with invented numbers.
  zone_cards()     per-zone status (comfortable / warm / cold / high_co2 /
                   high_occupancy / hvac_active / warning / critical) evaluated
                   against the PROFILE's comfort range and CO2 threshold.
  demand_now()     the building-demand concept: occupancy, HVAC, cooling,
                   heating, ventilation, lighting, energy, comfort, peak.
  demand_series()  hourly actual (from telemetry) vs expected (from the profile
                   schedule) for a zone or the whole building.
  kpis()           the executive KPI strip.
  explain_zone()   "why is the HVAC doing this", built from the controller's
                   actual decision record and live values — no free text.

HONESTY RULES (inherited from backend/telemetry.py; the source tag vocabulary is
the same — "sim", "derived", "hardware", "real", "historical", "predicted"):
  * Changing the profile NEVER changes the physics. The twin stays the 5-zone
    model; the profile changes how its state is EVALUATED (comfort range, CO2
    threshold, operating hours), what is EXPECTED (the demand schedule) and,
    through the operating mode, which existing controller lever is pulled
    (objective / safety mode — the same levers /api/controller exposes).
  * Expected values come from the profile schedule and are tagged "predicted"
    with basis "profile_schedule" — they are a planning expectation, not a
    forecast of the twin (that is /api/forecast).
  * Heating is reported as not modelled: the twin is cooling-only.
  * Lighting is an ESTIMATE (lighting power density x floor area x occupancy);
    the twin has no lighting load, so it is tagged "derived" and is NOT part of
    the twin's kWh.
  * Profile occupancy capacity and HVAC capacity are configuration. They are not
    used to extrapolate the 5 modelled zones to a whole building — that scale-out
    is deliberately left for a later phase.

NOTHING here writes to the twin, the store or the controller. Applying an
operating mode is done by backend/app.py through LiveSim._sync_controller().
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from backend.telemetry import RH_LIMITATION_NOTE as TEL_RH_NOTE
from backend.telemetry import THRESHOLDS as TEL_THRESHOLDS
from sim.twin import (_OCC_PEAK, BAND, CEILING_H, COP, CP_AIR, FAN_W, INFIL_ACH,
                      RHO_AIR, VENT_UA, ZONE_BY_ID, ZONE_IDS, ZONES)

# ------------------------------------------------------------------ vocabularies
BUILDING_TYPES = ("office", "mall", "hospital", "hotel", "college", "data_center")

OPERATING_MODES = {
    "normal": "Balanced operation; the objective follows the comfort/energy priorities.",
    "energy_saving": "Minimise kWh; comfort is a soft floor (objective = energy).",
    "comfort_priority": "Occupants win almost every tie (objective = comfort).",
    "peak_demand_reduction": ("Cost objective (energy-weighted). There is no dedicated "
                              "peak-shaving control law yet; this reuses the cost objective."),
    "emergency": "Safety mode emergency_override: complaints ignored, zones driven to the safe band.",
    "simulation": ("Dry run: safety mode recommend_only. Decisions are computed and logged "
                   "but not applied; the twin follows its base schedule."),
}

ZONE_STATUSES = ("comfortable", "warm", "cold", "high_co2", "high_occupancy",
                 "hvac_active", "warning", "critical", "unoccupied")

DEMAND_LEVELS = (("low", 0.35), ("medium", 0.60), ("high", 0.85), ("very_high", 9.9))

HIGH_OCC_PCT = 90.0          # zone status "high_occupancy" at >= this % of design headcount
CRIT_TEMP_MARGIN_C = 2.0     # beyond the comfort range by this much = critical
CRIT_CO2_FACTOR = 1.5        # CO2 above threshold x this = critical

# ------------------------------------------------------------------ validation limits
LIMITS = {
    "floors": (1, 200),
    "zones": (len(ZONES), 5000),
    "occupancy_capacity": (1, 1_000_000),
    "hvac_capacity_w": (1_000.0, 100_000_000.0),
    "open_hour": (0.0, 24.0),
    "close_hour": (0.0, 24.0),
    "comfort_min_c": (16.0, 30.0),
    "comfort_max_c": (16.0, 32.0),
    "humidity_min_pct": (10.0, 80.0),
    "humidity_max_pct": (20.0, 90.0),
    "co2_max_ppm": (600.0, 5000.0),
    "comfort_priority": (0.0, 100.0),
    "energy_priority": (0.0, 100.0),
    "occupancy_sensitivity": (0.0, 1.0),
    "base_load_fraction": (0.0, 1.0),
    "expected_occupancy_pct": (0.0, 150.0),
    "oa_per_person_ls": (0.0, 30.0),
    "oa_per_area_ls_m2": (0.0, 5.0),
    "lighting_w_m2": (0.0, 40.0),
}
MIN_COMFORT_WIDTH_C = 1.0
MIN_HUMIDITY_WIDTH_PCT = 10.0


# ------------------------------------------------------------------ the model
@dataclass
class BuildingConfig:
    """One commercial building's configuration. Every field is operator-editable.

    Units follow DATA_CONTRACTS.md: degC, W, kWh, %, ppm, L/s. Hours are
    wall-clock hours of the SIM day (0..24); close_hour 24 = open until midnight.
    `schedule` is 24 hourly expected-occupancy fractions (0..1) for an open day;
    `weekend_factor` scales it on days that are not in `open_days`... except for
    buildings open every day, where it is simply 1.0.
    `zone_roles` maps twin zone id -> the role that zone plays in THIS building
    type; `zone_floor` maps twin zone id -> floor number (1-based).
    """
    building_type: str = "office"
    name: str = "FeelsLike HQ"
    floors: int = 4
    zones: int = 12
    occupancy_capacity: int = 120
    hvac_capacity_w: float = 60_000.0
    open_hour: float = 8.0
    close_hour: float = 20.0
    open_days: list = field(default_factory=lambda: [0, 1, 2, 3, 4])
    comfort_min_c: float = BAND[0]
    comfort_max_c: float = BAND[1]
    humidity_min_pct: float = 30.0
    humidity_max_pct: float = 65.0
    co2_max_ppm: float = 1000.0
    comfort_priority: float = 60.0
    energy_priority: float = 40.0
    occupancy_sensitivity: float = 0.8
    base_load_fraction: float = 0.15
    expected_occupancy_pct: float = 100.0
    oa_per_person_ls: float = 2.5
    oa_per_area_ls_m2: float = 0.3
    lighting_w_m2: float = 8.9
    # Phase 3: weights of the engineering comfort index (backend/comfort.py).
    # Indicative / operator configurable — not a regulatory requirement.
    comfort_weights: dict = field(default_factory=lambda: {"thermal": 0.5, "humidity": 0.2, "air_quality": 0.3})
    operating_mode: str = "normal"
    schedule: list = field(default_factory=list)
    closed_factor: float = 0.1
    zone_roles: dict = field(default_factory=dict)
    zone_floor: dict = field(default_factory=dict)
    model_fit_note: str = ""
    basis: str = ""


def _curve(anchors: dict) -> list:
    """24 hourly fractions by linear interpolation between (hour -> fraction)
    anchors, wrapping midnight. Data, not logic: profiles below are anchors."""
    pts = sorted((float(h) % 24.0, float(v)) for h, v in anchors.items())
    out = []
    for h in range(24):
        prev = max((p for p in pts if p[0] <= h), default=pts[-1])
        nxt = min((p for p in pts if p[0] > h), default=pts[0])
        span = (nxt[0] - prev[0]) % 24.0 or 24.0
        k = ((h - prev[0]) % 24.0) / span
        out.append(round(prev[1] + (nxt[1] - prev[1]) * k, 3))
    return out


_BASIS = ("Indicative design values: comfort per ASHRAE 55 practice, outdoor air per "
          "ASHRAE 62.1 Table 6.2.2.1 space types, lighting power density per ASHRAE "
          "90.1 space-by-space order of magnitude, CO2 guideline ~1000 ppm (lower for "
          "healthcare). Operator-editable; not a compliance calculation.")

# Office-like twin zones on two floors: ground = public (lobby, cafeteria), upper = work.
_OFFICE_FLOORS = {"zone_d": 1, "zone_e": 1, "zone_a": 2, "zone_b": 2, "zone_c": 2}

PROFILES: dict = {
    "office": BuildingConfig(
        building_type="office", name="Corporate Office Tower", floors=4, zones=12,
        occupancy_capacity=120, hvac_capacity_w=60_000.0, open_hour=8.0, close_hour=20.0,
        open_days=[0, 1, 2, 3, 4], comfort_min_c=23.0, comfort_max_c=26.5,
        humidity_min_pct=30.0, humidity_max_pct=65.0, co2_max_ppm=1000.0,
        comfort_priority=60.0, energy_priority=40.0, occupancy_sensitivity=0.8,
        base_load_fraction=0.15, oa_per_person_ls=2.5, oa_per_area_ls_m2=0.3,
        lighting_w_m2=8.9, closed_factor=0.2,
        comfort_weights={"thermal": 0.5, "humidity": 0.2, "air_quality": 0.3},
        schedule=_curve({0: 0.02, 6: 0.03, 8: 0.25, 9: 0.55, 11: 0.9, 13: 0.9,
                         14: 0.85, 17: 0.7, 18: 0.5, 20: 0.2, 21: 0.08, 23: 0.02}),
        zone_roles={"zone_a": "Open-plan workspace", "zone_b": "Conference room",
                    "zone_c": "Manager cabin", "zone_d": "Reception lobby",
                    "zone_e": "Staff cafeteria"},
        zone_floor=dict(_OFFICE_FLOORS),
        model_fit_note="The twin's zones and occupancy schedules were built for this type.",
        basis=_BASIS),
    "mall": BuildingConfig(
        building_type="mall", name="City Centre Mall", floors=3, zones=40,
        occupancy_capacity=3000, hvac_capacity_w=900_000.0, open_hour=10.0, close_hour=22.0,
        open_days=[0, 1, 2, 3, 4, 5, 6], comfort_min_c=23.0, comfort_max_c=26.0,
        humidity_min_pct=30.0, humidity_max_pct=65.0, co2_max_ppm=1100.0,
        comfort_priority=50.0, energy_priority=50.0, occupancy_sensitivity=0.9,
        base_load_fraction=0.2, oa_per_person_ls=3.8, oa_per_area_ls_m2=0.3,
        lighting_w_m2=13.7, closed_factor=1.0,
        comfort_weights={"thermal": 0.5, "humidity": 0.15, "air_quality": 0.35},
        schedule=_curve({0: 0.02, 8: 0.05, 10: 0.5, 13: 0.8, 16: 0.75, 18: 1.0,
                         20: 0.95, 22: 0.3, 23: 0.05}),
        zone_roles={"zone_a": "Retail concourse", "zone_b": "Events hall",
                    "zone_c": "Centre management office", "zone_d": "Entrance atrium",
                    "zone_e": "Food court"},
        zone_floor={"zone_d": 1, "zone_a": 1, "zone_e": 2, "zone_b": 2, "zone_c": 2},
        model_fit_note=("Twin occupancy follows office schedules; retail footfall peaks are "
                        "expressed in the expected-demand schedule, not in the physics."),
        basis=_BASIS),
    "hospital": BuildingConfig(
        building_type="hospital", name="General Hospital", floors=6, zones=60,
        occupancy_capacity=800, hvac_capacity_w=1_200_000.0, open_hour=0.0, close_hour=24.0,
        open_days=[0, 1, 2, 3, 4, 5, 6], comfort_min_c=22.0, comfort_max_c=25.0,
        humidity_min_pct=30.0, humidity_max_pct=60.0, co2_max_ppm=800.0,
        comfort_priority=85.0, energy_priority=15.0, occupancy_sensitivity=0.4,
        base_load_fraction=0.55, oa_per_person_ls=2.5, oa_per_area_ls_m2=0.3,
        lighting_w_m2=10.9, closed_factor=1.0,
        comfort_weights={"thermal": 0.4, "humidity": 0.2, "air_quality": 0.4},
        schedule=_curve({0: 0.55, 6: 0.6, 9: 0.9, 12: 0.95, 16: 0.85, 20: 0.7, 23: 0.58}),
        zone_roles={"zone_a": "General ward", "zone_b": "Outpatient consultation",
                    "zone_c": "Doctors' office", "zone_d": "Emergency reception",
                    "zone_e": "Cafeteria"},
        zone_floor={"zone_d": 1, "zone_e": 1, "zone_b": 1, "zone_a": 2, "zone_c": 2},
        model_fit_note=("Clinical ventilation (air changes, pressure cascades) is not in the "
                        "twin; comfort and CO2 limits here are the tighter healthcare values."),
        basis=_BASIS),
    "hotel": BuildingConfig(
        building_type="hotel", name="Harbour View Hotel", floors=8, zones=150,
        occupancy_capacity=400, hvac_capacity_w=700_000.0, open_hour=0.0, close_hour=24.0,
        open_days=[0, 1, 2, 3, 4, 5, 6], comfort_min_c=22.0, comfort_max_c=25.0,
        humidity_min_pct=30.0, humidity_max_pct=60.0, co2_max_ppm=1000.0,
        comfort_priority=75.0, energy_priority=25.0, occupancy_sensitivity=0.6,
        base_load_fraction=0.4, oa_per_person_ls=2.5, oa_per_area_ls_m2=0.3,
        lighting_w_m2=8.1, closed_factor=1.0,
        comfort_weights={"thermal": 0.55, "humidity": 0.25, "air_quality": 0.2},
        schedule=_curve({0: 0.75, 6: 0.7, 8: 0.55, 11: 0.3, 15: 0.35, 19: 0.8, 21: 0.9}),
        zone_roles={"zone_a": "Guest room block", "zone_b": "Banquet / meeting room",
                    "zone_c": "Guest suite", "zone_d": "Lobby & reception",
                    "zone_e": "Restaurant"},
        zone_floor={"zone_d": 1, "zone_e": 1, "zone_b": 1, "zone_a": 2, "zone_c": 2},
        model_fit_note=("Guest-room occupancy is night-weighted here; the twin's zone schedules "
                        "stay daytime, so expected and actual occupancy diverge by design."),
        basis=_BASIS),
    "college": BuildingConfig(
        building_type="college", name="College Academic Block", floors=4, zones=30,
        occupancy_capacity=1500, hvac_capacity_w=500_000.0, open_hour=8.0, close_hour=18.0,
        open_days=[0, 1, 2, 3, 4, 5], comfort_min_c=23.0, comfort_max_c=26.5,
        humidity_min_pct=30.0, humidity_max_pct=65.0, co2_max_ppm=1000.0,
        comfort_priority=50.0, energy_priority=50.0, occupancy_sensitivity=0.9,
        base_load_fraction=0.12, oa_per_person_ls=3.8, oa_per_area_ls_m2=0.3,
        lighting_w_m2=8.9, closed_factor=0.15,
        comfort_weights={"thermal": 0.45, "humidity": 0.15, "air_quality": 0.4},
        schedule=_curve({0: 0.01, 7: 0.05, 8: 0.45, 10: 0.95, 12: 0.7, 13: 0.6,
                         14: 0.9, 16: 0.6, 18: 0.15, 20: 0.03}),
        zone_roles={"zone_a": "Lecture hall", "zone_b": "Seminar room",
                    "zone_c": "Faculty cabin", "zone_d": "Entrance hall",
                    "zone_e": "Canteen"},
        zone_floor={"zone_d": 1, "zone_e": 1, "zone_a": 2, "zone_b": 2, "zone_c": 2},
        model_fit_note="Close to the twin's office model: daytime, weekday-heavy occupancy.",
        basis=_BASIS),
    "data_center": BuildingConfig(
        building_type="data_center", name="Tier III Data Centre", floors=2, zones=10,
        occupancy_capacity=40, hvac_capacity_w=2_000_000.0, open_hour=0.0, close_hour=24.0,
        open_days=[0, 1, 2, 3, 4, 5, 6], comfort_min_c=22.0, comfort_max_c=26.0,
        humidity_min_pct=20.0, humidity_max_pct=60.0, co2_max_ppm=1000.0,
        comfort_priority=40.0, energy_priority=60.0, occupancy_sensitivity=0.1,
        base_load_fraction=0.85, oa_per_person_ls=2.5, oa_per_area_ls_m2=0.3,
        lighting_w_m2=10.0, closed_factor=1.0,
        # humidity weighted up: electronics care about RH/condensation; IT-equipment
        # thermal envelopes (ASHRAE TC 9.9 classes) are NOT modelled by this index
        comfort_weights={"thermal": 0.3, "humidity": 0.5, "air_quality": 0.2},
        schedule=_curve({0: 0.2, 8: 0.6, 12: 0.7, 18: 0.4, 22: 0.2}),
        zone_roles={"zone_a": "White space (IT hall)", "zone_b": "Customer meeting room",
                    "zone_c": "Electrical / UPS room", "zone_d": "NOC & security",
                    "zone_e": "Staff break room"},
        zone_floor={"zone_d": 1, "zone_e": 1, "zone_c": 1, "zone_a": 2, "zone_b": 2},
        model_fit_note=("IT heat load dominates a real data centre and is NOT modelled by the "
                        "twin (people + solar only). Expected HVAC demand is flat by design; "
                        "the twin's actual load will not match it."),
        basis=_BASIS),
}

TYPE_LABELS = {"office": "Office", "mall": "Shopping Mall", "hospital": "Hospital",
               "hotel": "Hotel", "college": "College / Educational", "data_center": "Data Center"}

EDITABLE = tuple(k for k in LIMITS) + ("name", "open_days", "operating_mode", "building_type",
                                        "comfort_weights")
COMFORT_WEIGHT_KEYS = ("thermal", "humidity", "air_quality")


def default_config(building_type: str = "office") -> BuildingConfig:
    """A fresh, independent copy of a profile. KeyError for an unknown type."""
    p = PROFILES[building_type]
    return replace(p, open_days=list(p.open_days), schedule=list(p.schedule),
                   zone_roles=dict(p.zone_roles), zone_floor=dict(p.zone_floor),
                   comfort_weights=dict(p.comfort_weights))


def validate(cfg: BuildingConfig) -> list:
    """Every problem with a config, as human strings. [] means valid. Pure."""
    errs = []
    if cfg.building_type not in PROFILES:
        errs.append(f"building_type must be one of {list(BUILDING_TYPES)}")
    if cfg.operating_mode not in OPERATING_MODES:
        errs.append(f"operating_mode must be one of {list(OPERATING_MODES)}")
    name = str(cfg.name or "").strip()
    if not 1 <= len(name) <= 80:
        errs.append("name must be 1-80 characters")
    for k, (lo, hi) in LIMITS.items():
        v = getattr(cfg, k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            errs.append(f"{k} must be a number")
        elif not lo <= v <= hi:
            extra = (f" (the twin models {len(ZONES)} zones, so fewer cannot be mapped)"
                     if k == "zones" else "")
            errs.append(f"{k} must be between {lo:g} and {hi:g}{extra}")
    if isinstance(cfg.floors, float) and not float(cfg.floors).is_integer():
        errs.append("floors must be a whole number")
    if isinstance(cfg.zones, float) and not float(cfg.zones).is_integer():
        errs.append("zones must be a whole number")
    if not errs:
        if cfg.comfort_max_c - cfg.comfort_min_c < MIN_COMFORT_WIDTH_C:
            errs.append(f"comfort range must be at least {MIN_COMFORT_WIDTH_C:g} °C wide "
                        "(comfort_min_c < comfort_max_c)")
        if cfg.humidity_max_pct - cfg.humidity_min_pct < MIN_HUMIDITY_WIDTH_PCT:
            errs.append(f"humidity range must be at least {MIN_HUMIDITY_WIDTH_PCT:g} % wide")
        if cfg.close_hour <= cfg.open_hour:
            errs.append("close_hour must be later than open_hour (use 0 and 24 for 24-hour operation)")
        if cfg.zones < cfg.floors:
            errs.append("zones must be at least the number of floors")
    cw = cfg.comfort_weights
    if (not isinstance(cw, dict) or set(cw) != set(COMFORT_WEIGHT_KEYS)
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in cw.values())
            or abs(sum(cw.values()) - 1.0) > 0.011):
        errs.append("comfort_weights must be {thermal, humidity, air_quality}, each 0..1, summing to 1")
    days = cfg.open_days
    if (not isinstance(days, list) or not days
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)
            or len(set(days)) != len(days)):
        errs.append("open_days must be a non-empty list of distinct day numbers 0 (Mon) .. 6 (Sun)")
    return errs


def apply_update(cfg: BuildingConfig, changes: dict) -> tuple:
    """Return (new_config, errors). A change of building_type RESETS every field to
    that profile's defaults first, then applies the other supplied fields on top —
    so "switch to hospital, name it St Mary's" is one request. Never mutates cfg.
    Unknown keys are an error, not silently ignored."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        return cfg, [f"unknown field(s): {unknown}"]
    t = changes.get("building_type")
    if t is not None and t not in PROFILES:
        return cfg, [f"building_type must be one of {list(BUILDING_TYPES)}"]
    if t is not None and t != cfg.building_type:
        base = default_config(t)
        base.operating_mode = cfg.operating_mode       # a mode is not a building property
    else:                                              # same type: edit the CURRENT config
        base = replace(cfg, open_days=list(cfg.open_days), schedule=list(cfg.schedule),
                       zone_roles=dict(cfg.zone_roles), zone_floor=dict(cfg.zone_floor),
                       comfort_weights=dict(cfg.comfort_weights))
    upd = {k: v for k, v in changes.items() if k != "building_type"}
    for k in ("floors", "zones", "occupancy_capacity"):
        if isinstance(upd.get(k), float) and upd[k].is_integer():
            upd[k] = int(upd[k])
    if isinstance(upd.get("name"), str):
        upd["name"] = upd["name"].strip()
    new = replace(base, **upd)
    errs = validate(new)
    return (cfg, errs) if errs else (new, [])


def config_dict(cfg: BuildingConfig) -> dict:
    d = asdict(cfg)
    d["type_label"] = TYPE_LABELS.get(cfg.building_type, cfg.building_type)
    return d


def catalog() -> dict:
    """Everything a configuration form needs: types, defaults, limits, modes."""
    return {
        "types": [{"key": k, "label": TYPE_LABELS[k], "defaults": config_dict(PROFILES[k])}
                  for k in BUILDING_TYPES],
        "operating_modes": dict(OPERATING_MODES),
        "limits": {k: list(v) for k, v in LIMITS.items()},
        "editable": list(EDITABLE),
        "zone_statuses": list(ZONE_STATUSES),
        "demand_levels": [k for k, _ in DEMAND_LEVELS],
        "comfort_weights_note": ("Engineering comfort index weights — indicative / operator "
                                 "configurable, not a regulatory requirement."),
    }


# ------------------------------------------------------------------ operating mode
def mode_levers(cfg: BuildingConfig) -> tuple:
    """(objective, safety_mode) the operating mode maps onto — the SAME levers
    /api/controller exposes; no new control law. In normal and simulation modes
    the objective follows the priorities: a 30-point lead picks comfort/energy."""
    lead = cfg.energy_priority - cfg.comfort_priority
    by_priority = "energy" if lead >= 30 else "comfort" if lead <= -30 else "balanced"
    return {
        "normal": (by_priority, "automatic"),
        "energy_saving": ("energy", "automatic"),
        "comfort_priority": ("comfort", "automatic"),
        "peak_demand_reduction": ("cost", "automatic"),
        "emergency": ("balanced", "emergency_override"),
        "simulation": (by_priority, "recommend_only"),
    }[cfg.operating_mode]


# ------------------------------------------------------------------ helpers
def _is_open(cfg: BuildingConfig, day: int, hour: float) -> bool:
    return (day % 7) in cfg.open_days and cfg.open_hour <= hour < cfg.close_hour


is_open = _is_open            # public name, used by backend/dataset


def expected_fraction(cfg: BuildingConfig, day: int, hour: float) -> float:
    """Expected occupancy as a fraction of design (0..1.5), from the profile."""
    sched = cfg.schedule or [0.0] * 24
    h0 = int(hour) % 24
    k = hour - int(hour)
    v = sched[h0] + (sched[(h0 + 1) % 24] - sched[h0]) * k
    if not _is_open(cfg, day, hour):
        v *= cfg.closed_factor
    return max(0.0, v * cfg.expected_occupancy_pct / 100.0)


def expected_hvac_fraction(cfg: BuildingConfig, occ_fraction: float) -> float:
    b, s = cfg.base_load_fraction, cfg.occupancy_sensitivity
    return max(0.0, min(1.5, b + (1.0 - b) * s * occ_fraction))


def demand_level(fraction: float) -> str:
    for name, hi in DEMAND_LEVELS:
        if fraction < hi:
            return name
    return DEMAND_LEVELS[-1][0]


def comfort_score(cfg: BuildingConfig, temp, rh) -> float | None:
    """0..100 against the PROFILE's ranges. Same shape as telemetry.comfort_score
    (thermal term falls to 0 at 3 degC outside the range; -1 point per %RH above
    humidity_max_pct, floored at -25) so the two scores agree for the office
    profile, whose ranges are the twin's BAND and RH_HUMID. DERIVED."""
    if temp is None or rh is None:
        return None
    lo, hi = cfg.comfort_min_c, cfg.comfort_max_c
    dev = (lo - temp) if temp < lo else ((temp - hi) if temp > hi else 0.0)
    thermal = max(0.0, 100.0 * (1.0 - dev / 3.0))
    humid = max(-25.0, -max(0.0, rh - cfg.humidity_max_pct))
    return round(max(0.0, min(100.0, thermal + humid)), 1)


def design_power_w(zone_id: str) -> float:
    """Electrical draw of a zone's unit at full cooling capacity and top fan."""
    return ZONE_BY_ID[zone_id].max_cool / COP + FAN_W[2]


def required_oa_ls(cfg: BuildingConfig, zone_id: str, people: float) -> float:
    """ASHRAE 62.1 ventilation-rate procedure: Rp * Pz + Ra * Az (L/s)."""
    return cfg.oa_per_person_ls * max(0.0, people) + cfg.oa_per_area_ls_m2 * ZONE_BY_ID[zone_id].area


def supplied_oa_ls(zone_id: str, vent: int) -> float:
    """Outdoor air the TWIN actually moves: the same couplings sim/twin.py uses."""
    z = ZONE_BY_ID[zone_id]
    m = VENT_UA * int(vent) / CP_AIR + INFIL_ACH * (z.area * CEILING_H) * RHO_AIR / 3600.0
    return m / RHO_AIR * 1000.0


def lighting_w(cfg: BuildingConfig, zone_id: str, occ_fraction: float, is_open: bool) -> float:
    """Estimated lighting draw: LPD x area x max(10 %, occupancy) when open, 10 % closed."""
    frac = max(0.1, min(1.0, occ_fraction)) if is_open else 0.1
    return cfg.lighting_w_m2 * ZONE_BY_ID[zone_id].area * frac


def _v(value, source: str, unit: str = "", **extra) -> dict:
    return {"value": value, "source": source, "unit": unit, **extra}


# ------------------------------------------------------------------ zones
def zone_state(cfg: BuildingConfig, row: dict, tel_zone: dict | None, alerts: list) -> dict:
    """Status flags and the headline status for one zone.

    INPUT: the /api/state zone row (LiveSim.zone_rows()), the latest telemetry
      zone dict (for CO2; may be None before the first step), maintenance alerts
      for this zone. OUTPUT: {"flags": [...], "status": one of ZONE_STATUSES,
      "severity": "normal"|"warning"|"critical"}.
    """
    temp, occ = row["temp"], row["occ"]
    co2 = (tel_zone or {}).get("co2")
    occupied = occ > 0
    flags = []
    lo, hi = cfg.comfort_min_c, cfg.comfort_max_c
    if temp > hi:
        flags.append("warm")
    elif temp < lo:
        flags.append("cold")
    elif occupied:
        flags.append("comfortable")
    if co2 is not None and co2 > cfg.co2_max_ppm:
        flags.append("high_co2")
    if (row.get("occ_pct") or 0) >= HIGH_OCC_PCT:
        flags.append("high_occupancy")
    if (row.get("cool_w") or 0) > 0 or (row.get("vent") or 0) > 0:
        flags.append("hvac_active")
    severity = "normal"
    if occupied and (temp > hi or temp < lo):
        severity = "warning"
    if co2 is not None and co2 > cfg.co2_max_ppm:
        severity = "warning"
    if row.get("at_capacity") and occupied and temp > hi:
        severity = "warning"
    if any(a.get("severity") in ("medium", "high") for a in alerts):
        severity = "warning"
    if occupied and (temp > hi + CRIT_TEMP_MARGIN_C or temp < lo - CRIT_TEMP_MARGIN_C):
        severity = "critical"
    if co2 is not None and co2 > cfg.co2_max_ppm * CRIT_CO2_FACTOR:
        severity = "critical"
    if any(a.get("severity") == "high" for a in alerts):
        severity = "critical"
    if severity != "normal":
        flags.append(severity)
    if severity != "normal":
        status = severity
    elif "warm" in flags and occupied:
        status = "warm"
    elif "cold" in flags and occupied:
        status = "cold"
    elif not occupied:
        status = "unoccupied"
    else:
        status = "comfortable"
    return {"flags": flags, "status": status, "severity": severity}


def zone_demand(cfg: BuildingConfig, row: dict, day: int, hour: float) -> dict:
    """The demand concept for one modelled zone, every value source-tagged."""
    zid = row["id"]
    peak = _OCC_PEAK[zid]
    occ_frac_now = row["occ"] / peak if peak else 0.0
    exp_frac = expected_fraction(cfg, day, hour)
    is_open = _is_open(cfg, day, hour)
    req = required_oa_ls(cfg, zid, row["occ"])
    sup = supplied_oa_ls(zid, row.get("vent") or 0)
    light = lighting_w(cfg, zid, occ_frac_now, is_open)
    hvac_w = float(row.get("power_w") or 0.0)
    exp_hvac_w = expected_hvac_fraction(cfg, exp_frac) * design_power_w(zid)
    return {
        "current_occupancy": _v(row["occ"], "sim", "people", pct=row.get("occ_pct")),
        "expected_occupancy": _v(round(exp_frac * peak, 1), "predicted", "people",
                                 pct=round(100.0 * exp_frac, 1), basis="profile_schedule"),
        "hvac_demand": _v(round(hvac_w, 1), "derived", "W",
                          pct=round(100.0 * hvac_w / design_power_w(zid), 1)),
        "expected_hvac_demand": _v(round(exp_hvac_w, 1), "predicted", "W",
                                   basis="profile_schedule"),
        "cooling_demand": _v(row.get("cool_w"), "derived", "W",
                             pct=row.get("capacity_pct")),
        "heating_demand": _v(None, "none", "W",
                             note="Not modelled: the twin is cooling-only."),
        "ventilation_demand": _v(round(req, 1), "derived", "L/s",
                                 supplied=round(sup, 1),
                                 met=bool(sup >= req),
                                 basis="ASHRAE 62.1 Rp·Pz + Ra·Az with the profile rates"),
        "lighting_demand": _v(round(light, 1), "derived", "W",
                              note="Estimate (LPD x area x occupancy); not in the twin's kWh."),
        "energy_demand": _v(round(hvac_w + light, 1), "derived", "W",
                            note="HVAC (twin) + lighting (estimate)"),
        "comfort_demand": _v(_comfort_gap(cfg, row["temp"]), "derived", "°C",
                             note="Distance outside the profile comfort range; 0 = inside."),
        "level": demand_level(max(occ_frac_now, hvac_w / design_power_w(zid))),
        "expected_level": demand_level(exp_frac),
    }


def _comfort_gap(cfg: BuildingConfig, temp: float) -> float:
    if temp > cfg.comfort_max_c:
        return round(temp - cfg.comfort_max_c, 2)
    if temp < cfg.comfort_min_c:
        return round(cfg.comfort_min_c - temp, 2)
    return 0.0


def zone_cards(cfg: BuildingConfig, zone_rows: list, tel_latest: dict | None,
               alerts: list, day: int, hour: float, hardware: dict | None = None) -> list:
    """Per-zone card payload for the building overview. Pure."""
    tz = (tel_latest or {}).get("zones", {})
    by_zone: dict = {}
    for a in alerts:
        by_zone.setdefault(a.get("zone"), []).append(a)
    out = []
    for row in zone_rows:
        zid = row["id"]
        tzone = tz.get(zid)
        st = zone_state(cfg, row, tzone, by_zone.get(zid, []))
        card = {
            "id": zid, "name": row["name"],
            "role": cfg.zone_roles.get(zid, row["name"]),
            "floor": min(cfg.floors, int(cfg.zone_floor.get(zid, 1))),
            "temp": _v(row["temp"], "sim", "°C"),
            "humidity": _v(row.get("rh"), "sim", "%"),
            "occupancy": _v(row["occ"], "sim", "people", pct=row.get("occ_pct")),
            "co2": _v(tzone.get("co2") if tzone else None, "derived", "ppm",
                      estimated=True),
            "comfort_score": _v(comfort_score(cfg, row["temp"], row.get("rh")), "derived", "/100"),
            "hvac": {"setpoint": row.get("setpoint"), "vent": row.get("vent"),
                     "cool_w": row.get("cool_w"), "capacity_pct": row.get("capacity_pct"),
                     "at_capacity": row.get("at_capacity"), "locked_out": row.get("locked_out"),
                     "active": "hvac_active" in st["flags"], "source": "sim"},
            "energy": {"power_w": _v(row.get("power_w"), "derived", "W"),
                       "kwh": _v(None, "sim", "kWh")},
            "demand": zone_demand(cfg, row, day, hour),
            "active_constraints": row.get("active_constraints", 0),
            "conflict": row.get("conflict", False),
            "alerts": len(by_zone.get(zid, [])),
            **st,
        }
        if tzone:
            card["energy"]["kwh"]["value"] = round(tzone.get("kwh", 0.0), 3)
        if hardware and hardware.get("zone") == zid and hardware.get("reading"):
            r = hardware["reading"]
            card["hardware"] = {"connected": bool(hardware.get("connected")),
                                "temp_c": r.get("temp_c"), "rh_pct": r.get("rh_pct"),
                                "source": "hardware",
                                "note": "Real sensor on the bench rig; shown beside, never "
                                        "instead of, the twin value."}
        out.append(card)
    return out


def topology(cfg: BuildingConfig, cards: list) -> dict:
    """Building -> floors -> zones. Floors with no modelled zone say so."""
    floors = []
    for f in range(1, int(cfg.floors) + 1):
        zs = [c for c in cards if c["floor"] == f]
        floors.append({
            "floor": f, "label": "Ground floor" if f == 1 else f"Floor {f}",
            "modelled": bool(zs), "zones": [c["id"] for c in zs],
            "status": _worst([c["severity"] for c in zs]) if zs else "not_modelled",
        })
    return {"name": cfg.name, "type": cfg.building_type,
            "floors": floors, "configured_floors": int(cfg.floors),
            "configured_zones": int(cfg.zones), "modelled_zones": len(cards),
            "note": (f"The digital twin models {len(cards)} of the {int(cfg.zones)} configured "
                     f"zones. Unmodelled floors and zones carry no values.")}


def _worst(sev: list) -> str:
    order = {"critical": 2, "warning": 1, "normal": 0}
    return max(sev, key=lambda s: order.get(s, 0)) if sev else "normal"


# ------------------------------------------------------------------ building demand
def demand_now(cfg: BuildingConfig, cards: list, day: int, hour: float,
               peak_today: dict) -> dict:
    """Building-level aggregation of every zone's demand, source-tagged."""
    def tot(key, sub="value"):
        vals = [c["demand"][key][sub] for c in cards if c["demand"][key][sub] is not None]
        return round(sum(vals), 1) if vals else None

    design_occ = sum(_OCC_PEAK[c["id"]] for c in cards) or 1
    design_w = sum(design_power_w(c["id"]) for c in cards) or 1.0
    exp_frac = expected_fraction(cfg, day, hour)
    occ = sum(c["occupancy"]["value"] for c in cards)
    hvac = tot("hvac_demand") or 0.0
    occupied = [c for c in cards if c["occupancy"]["value"] > 0]
    gaps = [c["demand"]["comfort_demand"]["value"] for c in occupied]
    req, sup = tot("ventilation_demand"), tot("ventilation_demand", "supplied")
    return {
        "current_occupancy": _v(occ, "sim", "people", pct=round(100.0 * occ / design_occ, 1)),
        "expected_occupancy": _v(round(exp_frac * cfg.occupancy_capacity), "predicted", "people",
                                 pct=round(100.0 * exp_frac, 1), basis="profile_schedule",
                                 note="Expected share of the configured occupancy capacity."),
        "hvac_demand": _v(round(hvac, 1), "derived", "W", pct=round(100.0 * hvac / design_w, 1)),
        "expected_hvac_demand": _v(round(expected_hvac_fraction(cfg, exp_frac) * design_w, 1),
                                   "predicted", "W", basis="profile_schedule",
                                   pct=round(100.0 * expected_hvac_fraction(cfg, exp_frac), 1)),
        "cooling_demand": _v(tot("cooling_demand"), "derived", "W"),
        "heating_demand": _v(None, "none", "W", note="Not modelled: the twin is cooling-only."),
        "ventilation_demand": _v(req, "derived", "L/s", supplied=sup,
                                 met=bool(sup is not None and req is not None and sup >= req)),
        "lighting_demand": _v(tot("lighting_demand"), "derived", "W",
                              note="Estimate; not in the twin's kWh."),
        "energy_demand": _v(tot("energy_demand"), "derived", "W"),
        "comfort_demand": _v(round(max(gaps), 2) if gaps else 0.0, "derived", "°C",
                             zones_outside=sum(1 for g in gaps if g > 0),
                             occupied_zones=len(occupied)),
        "peak_demand": _v(peak_today.get("power_w"), "derived", "W", t=peak_today.get("t")),
        "level": demand_level(max(occ / design_occ, hvac / design_w)),
        "expected_level": demand_level(exp_frac),
        "is_open": _is_open(cfg, day, hour),
        "scope": "modelled zones",
    }


def peak_today(rows: list, now_t: float) -> dict:
    """Highest total HVAC power in telemetry rows since today's 00:00 (sim)."""
    start = (now_t // 86400) * 86400
    best = None
    for r in rows:
        if r["t"] >= start and (best is None or r["power_w"] > best["power_w"]):
            best = r
    return {"power_w": best["power_w"], "t": best["t"]} if best else {"power_w": None, "t": None}


def demand_series(cfg: BuildingConfig, rows: list, now_t: float, zone: str = "all",
                  hours: int = 24, ahead_h: int = 6) -> dict:
    """Hourly actual vs expected demand.

    INPUT: telemetry rows (oldest first), the sim clock, zone "all" or a zone id,
      hours of history (1..168) and hours ahead (0..24).
    OUTPUT: {"points": [{t, hour, day, actual_power_w, peak_power_w, actual_occ_pct,
      actual_cool_w, expected_occ_pct, expected_hvac_w, expected_level, is_open,
      samples, future}], "fields": source per field, ...}. Hours with no telemetry
      carry actual_* = None — an unsampled hour is never zero-filled.
    SIDE EFFECTS: none.
    """
    zids = ZONE_IDS if zone == "all" else [zone]
    design_occ = sum(_OCC_PEAK[z] for z in zids) or 1
    design_w = sum(design_power_w(z) for z in zids) or 1.0
    cur_hour = int(now_t // 3600)
    first = cur_hour - int(hours) + 1
    buckets: dict = {}
    for r in rows:
        hb = int(r["t"] // 3600)
        if hb < first or hb > cur_hour:
            continue
        zs = r["zones"]
        p = sum(zs[z]["power_w"] for z in zids)
        o = sum(zs[z]["occ"] for z in zids)
        c = sum(zs[z]["cool_w"] for z in zids)
        b = buckets.setdefault(hb, [0.0, 0.0, 0.0, 0, 0.0])
        b[0] += p; b[1] += o; b[2] += c; b[3] += 1; b[4] = max(b[4], p)
    pts = []
    for hb in range(first, cur_hour + int(ahead_h) + 1):
        day, hour = hb // 24, float(hb % 24)
        ef = expected_fraction(cfg, day, hour + 0.5)
        b = buckets.get(hb)
        pts.append({
            "t": hb * 3600.0, "day": day % 7, "hour": int(hour), "future": hb > cur_hour,
            "actual_power_w": round(b[0] / b[3], 1) if b else None,
            "peak_power_w": round(b[4], 1) if b else None,
            "actual_occ_pct": round(100.0 * b[1] / b[3] / design_occ, 1) if b else None,
            "actual_cool_w": round(b[2] / b[3], 1) if b else None,
            "expected_occ_pct": round(100.0 * ef, 1),
            "expected_hvac_w": round(expected_hvac_fraction(cfg, ef) * design_w, 1),
            "expected_level": demand_level(ef),
            "is_open": _is_open(cfg, day, hour),
            "samples": b[3] if b else 0,
        })
    return {"zone": zone, "hours": int(hours), "ahead_h": int(ahead_h), "now_t": now_t,
            "design_power_w": round(design_w, 1), "design_occupancy": design_occ,
            "points": pts,
            "fields": {"actual_power_w": "derived", "peak_power_w": "derived",
                       "actual_occ_pct": "sim", "actual_cool_w": "derived",
                       "expected_occ_pct": "predicted", "expected_hvac_w": "predicted"},
            "note": ("Actual = hourly means of the step-cadence telemetry of the modelled zones. "
                     "Expected = the building profile's schedule (basis profile_schedule), "
                     "not a forward run of the twin.")}


# ------------------------------------------------------------------ KPIs
def kpis(cfg: BuildingConfig, cards: list, meters: dict, tel_rows: list, now_t: float,
         peak: dict, maint_alerts: list, health: dict) -> dict:
    """The executive KPI strip. Every item: value, unit, label, source, status."""
    occupied = [c for c in cards if c["occupancy"]["value"] > 0] or cards

    def avg(key):
        vals = [c[key]["value"] for c in occupied if c[key]["value"] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    power = round(sum((c["energy"]["power_w"]["value"] or 0.0) for c in cards), 1)
    cool = sum((c["hvac"]["cool_w"] or 0.0) for c in cards)
    cap = sum(ZONE_BY_ID[c["id"]].max_cool for c in cards) or 1.0
    day_start = (now_t // 86400) * 86400
    kwh_now = float(meters["us"]["kwh"])
    start_row = None
    for r in tel_rows:                       # oldest first: first row at/after 00:00
        if r["t"] >= day_start:
            start_row = r
            break
    # counter at 00:00 = the last row before midnight, else the twin's zero at build
    prev = [r for r in tel_rows if r["t"] < day_start]
    kwh_day0 = prev[-1]["kwh_us"] if prev else 0.0
    today = round(max(0.0, kwh_now - kwh_day0), 2)
    partial = not prev and start_row is not None and start_row["t"] - day_start > 120
    occ = sum(c["occupancy"]["value"] for c in cards)
    design_occ = sum(_OCC_PEAK[c["id"]] for c in cards) or 1
    temp, rh, co2 = avg("temp"), avg("humidity"), avg("co2")
    comfort = avg("comfort_score")
    zone_warn = sum(1 for c in cards if c["severity"] == "warning")
    zone_crit = sum(1 for c in cards if c["severity"] == "critical")
    n_alerts = len(maint_alerts) + zone_warn + zone_crit

    def item(label, value, unit, source, status="normal", **extra):
        return {"label": label, "value": value, "unit": unit, "source": source,
                "status": status, **extra}

    def band(v, lo, hi, margin):
        if v is None:
            return "unknown"
        if v < lo - margin or v > hi + margin:
            return "critical"
        return "warning" if (v < lo or v > hi) else "normal"

    load_pct = round(100.0 * cool / cap, 1)
    items = {
        "current_energy": item("Current energy draw", power, "W", "derived",
                               note="HVAC electrical power, modelled zones"),
        "today_energy": item("Today's energy", today, "kWh", "sim",
                             note=("since simulation start (building started mid-day)"
                                   if partial else "since 00:00 sim time")),
        "hvac_load": item("Current HVAC load", load_pct, "%", "derived",
                          status="critical" if load_pct >= 100 else
                          "warning" if load_pct >= 90 else "normal",
                          note=f"{round(cool):,} W of {round(cap):,} W thermal capacity"),
        "occupancy": item("Occupancy", occ, "people", "sim",
                          pct=round(100.0 * occ / design_occ, 1),
                          status="warning" if occ > design_occ else "normal"),
        "avg_temperature": item("Average temperature", temp, "°C", "sim",
                                status=band(temp, cfg.comfort_min_c, cfg.comfort_max_c,
                                            CRIT_TEMP_MARGIN_C),
                                note="occupied zones"),
        "avg_humidity": item("Average humidity", rh, "%", "sim",
                             status=band(rh, cfg.humidity_min_pct, cfg.humidity_max_pct, 15.0),
                             note=("occupied zones" if rh is None or rh <= cfg.humidity_max_pct
                                   else "occupied zones · high RH is a known twin model "
                                        "limitation (coil ADP approximation)"),
                             limitation=TEL_RH_NOTE),
        "avg_co2": item("Average CO₂", co2, "ppm", "derived",
                        status=("unknown" if co2 is None else
                                "critical" if co2 > cfg.co2_max_ppm * CRIT_CO2_FACTOR else
                                "warning" if co2 > cfg.co2_max_ppm else "normal"),
                        note="estimated by mass balance, occupied zones"),
        "comfort_score": item("Comfort score", comfort, "/100", "derived",
                              status=("unknown" if comfort is None else
                                      "critical" if comfort < 40 else
                                      "warning" if comfort < 70 else "normal"),
                              note="against the profile comfort range"),
        "energy_saving": item("Energy saving", meters.get("saved_pct"), "%", "derived",
                              note="vs the static-schedule baseline twin, same weather"),
        "peak_demand": item("Peak demand today", peak.get("power_w"), "W", "derived",
                            t=peak.get("t")),
        "active_alerts": item("Active alerts", n_alerts, "", "derived",
                              status="critical" if zone_crit or any(
                                  a.get("severity") == "high" for a in maint_alerts)
                              else "warning" if n_alerts else "normal",
                              breakdown={"maintenance": len(maint_alerts),
                                         "zone_warning": zone_warn, "zone_critical": zone_crit}),
        "system_health": item("System health", health["status"], "", "derived",
                              status={"ok": "normal", "degraded": "warning"}.get(
                                  health["status"], "critical"),
                              checks=health["checks"]),
    }
    return {"t": now_t, "items": items,
            "order": list(items),
            "thresholds": {"comfort_c": [cfg.comfort_min_c, cfg.comfort_max_c],
                           "humidity_pct": [cfg.humidity_min_pct, cfg.humidity_max_pct],
                           "co2_ppm": cfg.co2_max_ppm,
                           "hvac_load_pct": TEL_THRESHOLDS["capacity_pct"]["normal"]}}


def system_health(errors: list, telemetry_rows: int, hw_connected: bool,
                  hw_seen: bool, external_available: bool, external_enabled: bool) -> dict:
    """ok / degraded / failing from the software's own health signals."""
    checks = [
        {"name": "simulation subsystems", "ok": not errors,
         "detail": "no subsystem failures" if not errors else
         f"{len(errors)} subsystem(s) failing: " + ", ".join(e["subsystem"] for e in errors)},
        {"name": "telemetry", "ok": telemetry_rows > 0,
         "detail": f"{telemetry_rows} step rows buffered"},
        {"name": "hardware rig", "ok": hw_connected or not hw_seen,
         "detail": "connected" if hw_connected else
         ("node went silent" if hw_seen else "not attached (simulation only)")},
        {"name": "external weather feed", "ok": external_available or not external_enabled,
         "detail": "available" if external_available else
         ("unavailable" if external_enabled else "disabled")},
    ]
    critical = bool(errors) and any(e.get("subsystem") in ("decisions", "telemetry") for e in errors)
    bad = [c for c in checks if not c["ok"]]
    status = "failing" if critical else "degraded" if bad else "ok"
    return {"status": status, "checks": checks}


# ------------------------------------------------------------------ explanation
def explain_zone(cfg: BuildingConfig, card: dict, decision: dict | None,
                 constraint_explain: dict | None) -> dict:
    """WHY the HVAC in this zone is doing what it is doing, from real values.

    INPUT: the zone card (live values), the controller's current ControllerDecision
      for the zone as a dict (None when the controller produced no material
      decision — the zone is on its base schedule), the store's explain() dict.
    OUTPUT: {"action", "headline", "factors": [{label, value, unit, source, flag}],
      "reason_code", "decision_summary", "constraint", "sentence"}.
    Nothing here is free text from a model: every number is a field of the inputs.
    """
    hv = card["hvac"]
    temp = card["temp"]["value"]
    occ_pct = card["occupancy"]["pct"]
    co2 = card["co2"]["value"]
    sp, vent = hv.get("setpoint"), hv.get("vent")
    if sp is None:
        action = "HVAC off"
    elif (hv.get("cool_w") or 0) > 0:
        action = f"Cooling to {sp:.1f} °C"
    else:
        action = f"Holding setpoint {sp:.1f} °C (no cooling needed this step)"
    if vent:
        action += f", fan level {vent}"
    base = (decision or {}).get("base_setpoint")
    new = (decision or {}).get("new_setpoint")
    if decision and base is not None and new is not None and abs(new - base) >= 0.05:
        direction = "increased" if new < base else "reduced"
        headline = f"{card['role']} cooling {direction}"
    elif decision:
        headline = f"{card['role']}: controller intervened"
    else:
        headline = f"{card['role']}: running the base schedule"

    lo, hi = cfg.comfort_min_c, cfg.comfort_max_c
    factors = [
        {"label": "Occupancy", "value": occ_pct, "unit": "%", "source": "sim",
         "flag": "high" if (occ_pct or 0) >= HIGH_OCC_PCT else None},
        {"label": "Temperature", "value": temp, "unit": "°C", "source": "sim",
         "flag": "above range" if temp > hi else "below range" if temp < lo else None},
        {"label": "Comfort target", "value": f"{lo:g}–{hi:g}", "unit": "°C",
         "source": "config", "flag": None},
        {"label": "Humidity", "value": card["humidity"]["value"], "unit": "%", "source": "sim",
         "flag": "high" if (card["humidity"]["value"] or 0) > cfg.humidity_max_pct else None},
        {"label": "CO₂ (estimated)", "value": co2, "unit": "ppm", "source": "derived",
         "flag": "above threshold" if co2 is not None and co2 > cfg.co2_max_ppm else None},
        {"label": "Capacity used", "value": hv.get("capacity_pct"), "unit": "%",
         "source": "derived", "flag": "at capacity" if hv.get("at_capacity") else None},
    ]
    if decision:
        factors.append({"label": "Outdoor", "value": decision.get("outdoor_c"), "unit": "°C",
                        "source": "sim", "flag": None})
        if decision.get("constraints"):
            factors.append({"label": "Occupant constraints", "value": len(decision["constraints"]),
                            "unit": "", "source": "sim",
                            "flag": "conflict" if decision.get("conflict") else None})
    flagged = [f for f in factors if f["flag"]]

    def shown(f):
        v, u = f["value"], f["unit"]
        if isinstance(v, (int, float)):
            v = f"{v:.0f}" if u in ("%", "ppm") else f"{v:.1f}"
        return f"{f['label']} = {v}{u if u == '%' else ' ' + u}"
    parts = [shown(f) for f in factors
             if f["label"] in ("Occupancy", "Temperature", "Comfort target", "CO₂ (estimated)")
             and f["value"] is not None]
    sentence = headline + " because: " + "; ".join(parts) + "."
    return {
        "action": action, "headline": headline, "factors": factors,
        "flagged": [f["label"] for f in flagged],
        "reason_code": (decision or {}).get("reason_code") or "base_schedule",
        "decision_summary": (decision or {}).get("summary") or
        "No material controller event this step: the zone follows its occupancy schedule.",
        "decision_id": (decision or {}).get("id") or None,
        "objective": (decision or {}).get("objective"),
        "safety_mode": (decision or {}).get("safety_mode"),
        "est_energy_delta_pct": (decision or {}).get("est_energy_delta_pct"),
        "est_comfort_delta_pct": (decision or {}).get("est_comfort_delta_pct"),
        "constraint": constraint_explain,
        "sentence": sentence,
    }

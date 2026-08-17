"""FeelsLike digital twin: 5-zone RC (resistor-capacitor) thermal model.

Physics per zone i (standard lumped-parameter building model):
    C_i dT_i/dt = UA_i (T_out - T_i) + sum_j G_ij (T_j - T_i)
                  + Q_solar_i + Q_occupants_i + Q_hvac_i

HVAC = "perfect thermostat with finite capacity": each step it removes exactly
the heat needed to hit the setpoint, capped at the unit's capacity.
Electrical power = thermal cooling / COP + ventilation fan power.

Moisture (added later): each zone also carries a humidity ratio W, integrated
with an explicit mass balance (see step()). MODELING ASSUMPTION / KNOWN
LIMITATION: latent load is tracked and reported, never charged to kWh, and it
does not feed back into the sensible balance — so the A/B energy numbers are
identical with and without the humidity model. See sim/humidity.py for why.

SECOND KNOWN LIMITATION: the coil's apparatus dew point is approximated as
ADP_APPROACH kelvin below the setpoint (the spec'd approximation). A real DX
coil surface runs at ~10-13 degC, not ~22, so this floor binds almost every
occupied minute in monsoon weather and pins indoor RH near 88%. Consequence:
humid_viol_min saturates and is currently a "how humid is Delhi" signal rather
than a controller discriminator. Fix is one constant (a real ADP, e.g. 12 degC)
once the team agrees to move the number.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sim.humidity import (
    H_FG,
    LATENT_PER_PERSON_W,
    SHR,
    dew_point,
    humidity_ratio,
    outdoor_rh,
    rh_from_ratio,
)
from sim.weather import outdoor_temp, solar_factor

DT = 60.0            # simulation step (s)
COP = 3.4            # cooling coefficient of performance
FAN_W = {0: 0.0, 1: 150.0, 2: 420.0}   # fan power by vent level, per zone
VENT_UA = 45.0       # extra W/K of outdoor-air coupling per vent level
BAND = (23.0, 26.5)  # occupied comfort band (degC), ASHRAE-ish for offices

GRID_CO2 = 0.71      # kgCO2 per kWh (India grid, CEA ~FY23)
TARIFF = 9.0         # Rs per kWh (commercial ToU average)

# --- moisture model constants ---
CEILING_H = 3.0      # m, assumed floor-to-ceiling height for zone air volume
RHO_AIR = 1.2        # kg/m3
CP_AIR = 1005.0      # J/kgK, used to turn a W/K coupling into a kg/s mass flow
INFIL_ACH = 0.3      # baseline air changes/hour of envelope leakage at vent 0
RH_HUMID = 65.0      # %RH above which an occupied zone counts as "muggy"
ADP_APPROACH = 2.0   # K below the coil-entering air temp -> apparatus dew point


@dataclass
class Zone:
    id: str
    name: str
    aliases: list
    orientation: str
    area: float          # m2
    C: float             # thermal capacitance J/K
    UA: float            # envelope conductance W/K
    solar_peak: float    # W at full factor
    max_cool: float      # W thermal
    occ_profile: str     # key into OCC_PROFILES


ZONES: list = [
    Zone("zone_a", "Open Office A", ["open office", "office", "zone a", "bullpen", "desk"],
         "N", 220, 3.2e6, 500, 1600, 16000, "office"),
    Zone("zone_b", "Conference Room B", ["conference", "conf room", "room b", "meeting room", "board room"],
         "S", 60, 0.9e6, 160, 2400, 7000, "conference"),
    Zone("zone_c", "Cabin C", ["cabin", "cabin c", "room c", "manager"],
         "E", 40, 0.6e6, 110, 1700, 4000, "cabin"),
    Zone("zone_d", "Lobby D", ["lobby", "reception", "entrance", "zone d"],
         "W", 90, 1.4e6, 300, 2600, 8000, "lobby"),
    Zone("zone_e", "Cafeteria E", ["cafeteria", "canteen", "cafe", "pantry", "zone e"],
         "S", 110, 1.6e6, 340, 2100, 10000, "cafeteria"),
]
ZONE_IDS = [z.id for z in ZONES]
ZONE_BY_ID = {z.id: z for z in ZONES}
# Weekday peak headcount per zone, for reporting occupancy as % of design load.
_OCC_PEAK = {}

# Inter-zone conductance G_ij (W/K), symmetric, sparse (shared walls)
ADJACENCY = {
    ("zone_a", "zone_b"): 90.0,
    ("zone_a", "zone_c"): 70.0,
    ("zone_a", "zone_d"): 110.0,
    ("zone_d", "zone_e"): 90.0,
    ("zone_b", "zone_c"): 40.0,
}


def _pw(h: float, pieces) -> float:
    """Piecewise-constant helper: pieces = [(start_h, end_h, value), ...]."""
    for a, b, v in pieces:
        if a <= h < b:
            return v
    return 0.0


def occupancy(profile: str, day: int, h: float) -> float:
    """People in the zone. day: 0=Mon .. 6=Sun."""
    weekend = day % 7 >= 5
    if profile == "office":
        if weekend:
            return _pw(h, [(10, 16, 2)])
        return _pw(h, [(8, 9, 10), (9, 13, 24), (13, 14, 14), (14, 18, 24), (18, 20, 8)])
    if profile == "conference":
        if weekend:
            return 0
        return _pw(h, [(10, 11, 10), (14, 15.5, 12), (16.5, 17.5, 8)])
    if profile == "cabin":
        return 0 if weekend else _pw(h, [(9, 19, 3)])
    if profile == "lobby":
        return _pw(h, [(8, 20, 1 if weekend else 4)])
    if profile == "cafeteria":
        if weekend:
            return 0
        return _pw(h, [(8, 12, 3), (12, 14.5, 30), (14.5, 18, 4)])
    return 0


_OCC_PEAK.update({
    z.id: max([occupancy(z.occ_profile, 0, q / 4.0) for q in range(96)] + [1])
    for z in ZONES
})


class DigitalTwin:
    """Steps the 5-zone thermal model; accumulates energy & comfort metrics."""

    def __init__(self, seed: int = 0, start_temp: float = 28.0, weather_fn=None):
        self.seed = seed
        self.t = 0.0                       # sim seconds since Monday 00:00
        self.T = {z.id: start_temp for z in ZONES}
        self.weather_fn = weather_fn or (lambda t: outdoor_temp(t, seed))
        self.kwh = 0.0
        self.kwh_by_zone = {z.id: 0.0 for z in ZONES}
        self.viol_min = 0.0                # occupied minutes outside band
        self.hot_deg_min = 0.0             # degree-minutes above band (occupied)
        self.cold_deg_min = 0.0            # degree-minutes below band (occupied)
        self.last_power_w = 0.0

        # --- live condition knobs (what-if scenarios mutate these) ---
        self.occ_scale = 1.0
        self.capacity_scale = 1.0
        self.solar_scale = 1.0
        self.outdoor_offset = 0.0
        self.humidity_offset = 0.0

        # --- moisture state: humidity ratio per zone, seeded from outdoors ---
        t0_out = self.weather_fn(0.0) + self.outdoor_offset
        self.W = {z.id: humidity_ratio(t0_out, self._outdoor_rh_at(0.0)) for z in ZONES}
        self.humid_viol_min = 0.0          # occupied minutes with RH > RH_HUMID
        self.at_capacity_min = 0.0         # occupied minutes the coil was saturated
        self._rh_sum = 0.0                 # sum of RH over occupied zone-minutes
        self._rh_min = 0.0                 # count of those occupied zone-minutes
        self._at_cap = {z.id: False for z in ZONES}   # coil saturated last step

        self.last_setpoints = {}
        self.last_vents = {}

    # ---- clock helpers -------------------------------------------------
    @property
    def day(self) -> int:
        return int(self.t // 86400)

    @property
    def hour(self) -> float:
        return (self.t % 86400) / 3600.0

    def occupancy_now(self, zone_id: str, ahead_h: float = 0.0):
        """People in a zone now (or ahead_h hours ahead), scaled by occ_scale.

        INPUT: zone_id (must exist), ahead_h float hours.
        OUTPUT: int >= 0.  SIDE EFFECTS: none.
        ERROR STATES: KeyError on an unknown zone_id (unchanged behaviour).
        """
        z = ZONE_BY_ID[zone_id]
        t = self.t + ahead_h * 3600.0
        occ = occupancy(z.occ_profile, int(t // 86400), (t % 86400) / 3600.0)
        return max(0, int(round(occ * self.occ_scale)))

    def _outdoor_rh_at(self, t: float) -> float:
        """Outdoor RH at sim time t including humidity_offset, clamped 0..100."""
        return min(100.0, max(0.0, outdoor_rh(t, self.seed) + self.humidity_offset))

    # ---- condition knobs ------------------------------------------------
    def set_conditions(self, occ_scale=None, capacity_scale=None, solar_scale=None,
                       outdoor_offset=None, humidity_offset=None) -> dict:
        """Set the live what-if knobs; each argument left None is unchanged.

        Ranges (out-of-range values are CLAMPED, not rejected):
          occ_scale 0..3, capacity_scale 0.1..1.5, solar_scale 0..2,
          outdoor_offset -10..+10 degC, humidity_offset -30..+30 %RH.

        INPUT:  floats or None.
        OUTPUT: dict of the five resulting knob values.
        SIDE EFFECTS: mutates this twin; every knob feeds the next step().
        ERROR STATES: a non-numeric argument raises TypeError from float().
        """
        def clamp(v, lo, hi):
            return min(hi, max(lo, float(v)))

        if occ_scale is not None:
            self.occ_scale = clamp(occ_scale, 0.0, 3.0)
        if capacity_scale is not None:
            self.capacity_scale = clamp(capacity_scale, 0.1, 1.5)
        if solar_scale is not None:
            self.solar_scale = clamp(solar_scale, 0.0, 2.0)
        if outdoor_offset is not None:
            self.outdoor_offset = clamp(outdoor_offset, -10.0, 10.0)
        if humidity_offset is not None:
            self.humidity_offset = clamp(humidity_offset, -30.0, 30.0)
        return {
            "occ_scale": self.occ_scale,
            "capacity_scale": self.capacity_scale,
            "solar_scale": self.solar_scale,
            "outdoor_offset": self.outdoor_offset,
            "humidity_offset": self.humidity_offset,
        }

    # ---- humidity readouts ----------------------------------------------
    def rh_now(self, zone_id: str) -> float:
        """Zone relative humidity right now, percent 0..100 (Magnus inverse).

        INPUT: zone_id.  OUTPUT: float %RH.  SIDE EFFECTS: none.
        ERROR STATES: KeyError on an unknown zone_id.
        """
        return rh_from_ratio(self.T[zone_id], self.W[zone_id])

    def dew_point_now(self, zone_id: str) -> float:
        """Zone dew-point temperature, degC (Magnus inverse).

        INPUT: zone_id.  OUTPUT: float degC <= zone temp.  SIDE EFFECTS: none.
        ERROR STATES: KeyError on an unknown zone_id.
        """
        return dew_point(self.T[zone_id], self.rh_now(zone_id))

    def zone_snapshot(self, zone_id: str) -> dict:
        """Everything a UI or explainer needs about one zone in one dict.

        INPUT: zone_id.
        OUTPUT: dict(temp_c, setpoint_c, vent, occ, occ_pct, rh_pct,
                     dew_point_c, capacity_w, at_capacity). setpoint_c is None
                when HVAC was off on the last step; at_capacity reflects the
                last step only (False before the first step).
        SIDE EFFECTS: none.  ERROR STATES: KeyError on an unknown zone_id.
        """
        z = ZONE_BY_ID[zone_id]
        occ = self.occupancy_now(zone_id)
        cap = z.max_cool * self.capacity_scale
        peak = _OCC_PEAK[zone_id]   # 100% = this zone's weekday design headcount
        return {
            "temp_c": round(self.T[zone_id], 2),
            "setpoint_c": self.last_setpoints.get(zone_id),
            "vent": int(self.last_vents.get(zone_id, 0)),
            "occ": occ,
            "occ_pct": round(100.0 * occ / peak, 1),
            "rh_pct": round(self.rh_now(zone_id), 1),
            "dew_point_c": round(self.dew_point_now(zone_id), 2),
            "capacity_w": round(cap, 1),
            "at_capacity": bool(self._at_cap.get(zone_id, False)),
        }

    # ---- independent copy ------------------------------------------------
    def clone(self) -> "DigitalTwin":
        """Deep-enough copy for what-if runs: mutating the clone never touches self.

        Every mutable container (T, W, kwh_by_zone, last_setpoints, last_vents,
        _at_cap) is rebuilt; scalars and counters are copied by value; seed and
        weather_fn are shared by reference (both are pure/immutable).

        INPUT: none.  OUTPUT: a new DigitalTwin at the same sim time and state.
        SIDE EFFECTS: none on self.  ERROR STATES: none.
        """
        c = DigitalTwin(seed=self.seed, weather_fn=self.weather_fn)
        c.t = self.t
        c.T = dict(self.T)
        c.W = dict(self.W)
        c.kwh = self.kwh
        c.kwh_by_zone = dict(self.kwh_by_zone)
        c.viol_min = self.viol_min
        c.hot_deg_min = self.hot_deg_min
        c.cold_deg_min = self.cold_deg_min
        c.last_power_w = self.last_power_w
        c.occ_scale = self.occ_scale
        c.capacity_scale = self.capacity_scale
        c.solar_scale = self.solar_scale
        c.outdoor_offset = self.outdoor_offset
        c.humidity_offset = self.humidity_offset
        c.humid_viol_min = self.humid_viol_min
        c.at_capacity_min = self.at_capacity_min
        c._rh_sum = self._rh_sum
        c._rh_min = self._rh_min
        c.last_setpoints = dict(self.last_setpoints)
        c.last_vents = dict(self.last_vents)
        c._at_cap = dict(self._at_cap)
        return c

    # ---- one simulation step ------------------------------------------
    def step(self, setpoints: dict, vents: dict, dt: float = DT) -> dict:
        """setpoints: zone_id -> degC (or None = HVAC off). vents: zone_id -> 0|1|2."""
        t_out = self.weather_fn(self.t) + self.outdoor_offset
        rh_out = self._outdoor_rh_at(self.t)
        w_out = humidity_ratio(t_out, rh_out)              # kg/kg outdoor air
        day, h = self.day, self.hour
        newT, newW, power_w = {}, {}, 0.0

        for z in ZONES:
            T = self.T[z.id]
            occ = self.occupancy_now(z.id)
            vent = int(vents.get(z.id, 0))

            q_env = z.UA * (t_out - T)
            q_vent = VENT_UA * vent * (t_out - T)          # outdoor-air load
            q_int = (z.solar_peak * solar_factor(z.orientation, h) * self.solar_scale
                     + 100.0 * occ)
            q_nbr = 0.0
            for (a, b), g in ADJACENCY.items():
                if a == z.id:
                    q_nbr += g * (self.T[b] - T)
                elif b == z.id:
                    q_nbr += g * (self.T[a] - T)

            # Free-floating temperature after this step
            T_free = T + dt / z.C * (q_env + q_vent + q_int + q_nbr)

            # Cooling to setpoint, capacity-limited
            sp = setpoints.get(z.id)
            q_cool = 0.0
            cap = z.max_cool * self.capacity_scale
            at_cap = False
            if sp is not None and T_free > sp:
                q_need = z.C * (T_free - sp) / dt          # W to remove
                q_cool = min(q_need, cap)
                at_cap = q_need > cap
            T_new = T_free - dt * q_cool / z.C
            self._at_cap[z.id] = at_cap

            p_zone = q_cool / COP + FAN_W[vent]
            power_w += p_zone
            self.kwh_by_zone[z.id] += p_zone * dt / 3.6e6

            # ---- moisture balance (does NOT feed back into the thermal side) --
            # M_air dW/dt = m_air*(W_out - W) + G_latent - m_dehum
            # M_air: zone air mass = floor area * ceiling height * air density.
            W = self.W[z.id]
            m_air_mass = z.area * CEILING_H * RHO_AIR                    # kg
            # Ventilation mass flow from the SAME coupling the sensible side
            # uses: a UA of X W/K is an air stream of X/cp_air kg/s, because
            # q = m_dot * cp * dT. Plus baseline envelope leakage at INFIL_ACH
            # air changes/hour of the zone volume, present even at vent 0.
            m_vent = VENT_UA * vent / CP_AIR                             # kg/s
            m_inf = INFIL_ACH * (z.area * CEILING_H) * RHO_AIR / 3600.0  # kg/s
            m_out = m_vent + m_inf
            # Occupant latent gain: 60 W/person of evaporation, /h_fg -> kg/s.
            g_lat = LATENT_PER_PERSON_W * occ / H_FG                     # kg/s
            # Coil dehumidification: with a sensible heat ratio SHR the coil's
            # latent capacity is q_sensible*(1-SHR)/SHR watts, /h_fg -> kg/s.
            m_dehum = q_cool * (1.0 - SHR) / SHR / H_FG if q_cool > 0 else 0.0
            W_new = W + dt * (m_out * (w_out - W) + g_lat - m_dehum) / m_air_mass
            if m_dehum > 0.0:
                # A coil cannot dry air below its apparatus dew point: saturated
                # air a couple of K below the entering/leaving air temperature.
                t_adp = min(sp if sp is not None else T_new, T_new) - ADP_APPROACH
                W_new = max(W_new, humidity_ratio(t_adp, 100.0))
            # Never negative, never supersaturated at the zone temperature.
            W_new = min(max(W_new, 0.0), humidity_ratio(T_new, 100.0))
            newW[z.id] = W_new

            # Comfort accounting (only when occupied)
            if occ > 0:
                lo, hi = BAND
                if T_new > hi:
                    self.hot_deg_min += (T_new - hi) * dt / 60.0
                    self.viol_min += dt / 60.0
                elif T_new < lo:
                    self.cold_deg_min += (lo - T_new) * dt / 60.0
                    self.viol_min += dt / 60.0
                rh_new = rh_from_ratio(T_new, W_new)
                self._rh_sum += rh_new * dt / 60.0
                self._rh_min += dt / 60.0
                if rh_new > RH_HUMID:
                    self.humid_viol_min += dt / 60.0
                if at_cap:
                    self.at_capacity_min += dt / 60.0
            newT[z.id] = T_new

        self.T = newT
        self.W = newW
        self.kwh += power_w * dt / 3.6e6
        self.last_power_w = power_w
        self.last_setpoints = dict(setpoints)
        self.last_vents = {z.id: int(vents.get(z.id, 0)) for z in ZONES}
        self.t += dt
        return {"t": self.t, "t_out": t_out, "power_w": power_w,
                "rh_out": rh_out}

    # ---- reporting -----------------------------------------------------
    def metrics(self) -> dict:
        """Cumulative energy + comfort scorecard.

        INPUT: none. OUTPUT: dict with the original six keys (kwh, cost_rs,
        co2_kg, viol_min, hot_deg_min, cold_deg_min) plus humid_viol_min
        (occupied min with RH>65), mean_rh (mean RH over occupied zone-minutes,
        0.0 if never occupied) and at_capacity_min (occupied min the coil could
        not meet the setpoint). SIDE EFFECTS: none. ERROR STATES: none.
        """
        return {
            "kwh": round(self.kwh, 2),
            "cost_rs": round(self.kwh * TARIFF, 1),
            "co2_kg": round(self.kwh * GRID_CO2, 2),
            "viol_min": round(self.viol_min, 1),
            "hot_deg_min": round(self.hot_deg_min, 1),
            "cold_deg_min": round(self.cold_deg_min, 1),
            "humid_viol_min": round(self.humid_viol_min, 1),
            "mean_rh": round(self._rh_sum / self._rh_min, 1) if self._rh_min else 0.0,
            "at_capacity_min": round(self.at_capacity_min, 1),
        }

"""FeelsLike digital twin: 5-zone RC (resistor-capacitor) thermal model.

Physics per zone i (standard lumped-parameter building model):
    C_i dT_i/dt = UA_i (T_out - T_i) + sum_j G_ij (T_j - T_i)
                  + Q_solar_i + Q_occupants_i + Q_hvac_i

HVAC = "perfect thermostat with finite capacity": each step it removes exactly
the heat needed to hit the setpoint, capped at the unit's capacity.
Electrical power = thermal cooling / COP + ventilation fan power.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sim.weather import outdoor_temp, solar_factor

DT = 60.0            # simulation step (s)
COP = 3.4            # cooling coefficient of performance
FAN_W = {0: 0.0, 1: 150.0, 2: 420.0}   # fan power by vent level, per zone
VENT_UA = 45.0       # extra W/K of outdoor-air coupling per vent level
BAND = (23.0, 26.5)  # occupied comfort band (degC), ASHRAE-ish for offices

GRID_CO2 = 0.71      # kgCO2 per kWh (India grid, CEA ~FY23)
TARIFF = 9.0         # Rs per kWh (commercial ToU average)


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

    # ---- clock helpers -------------------------------------------------
    @property
    def day(self) -> int:
        return int(self.t // 86400)

    @property
    def hour(self) -> float:
        return (self.t % 86400) / 3600.0

    def occupancy_now(self, zone_id: str, ahead_h: float = 0.0):
        z = ZONE_BY_ID[zone_id]
        t = self.t + ahead_h * 3600.0
        return occupancy(z.occ_profile, int(t // 86400), (t % 86400) / 3600.0)

    # ---- one simulation step ------------------------------------------
    def step(self, setpoints: dict, vents: dict, dt: float = DT) -> dict:
        """setpoints: zone_id -> degC (or None = HVAC off). vents: zone_id -> 0|1|2."""
        t_out = self.weather_fn(self.t)
        day, h = self.day, self.hour
        newT, power_w = {}, 0.0

        for z in ZONES:
            T = self.T[z.id]
            occ = occupancy(z.occ_profile, day, h)
            vent = int(vents.get(z.id, 0))

            q_env = z.UA * (t_out - T)
            q_vent = VENT_UA * vent * (t_out - T)          # outdoor-air load
            q_int = z.solar_peak * solar_factor(z.orientation, h) + 100.0 * occ
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
            if sp is not None and T_free > sp:
                q_need = z.C * (T_free - sp) / dt          # W to remove
                q_cool = min(q_need, z.max_cool)
            T_new = T_free - dt * q_cool / z.C

            p_zone = q_cool / COP + FAN_W[vent]
            power_w += p_zone
            self.kwh_by_zone[z.id] += p_zone * dt / 3.6e6

            # Comfort accounting (only when occupied)
            if occ > 0:
                lo, hi = BAND
                if T_new > hi:
                    self.hot_deg_min += (T_new - hi) * dt / 60.0
                    self.viol_min += dt / 60.0
                elif T_new < lo:
                    self.cold_deg_min += (lo - T_new) * dt / 60.0
                    self.viol_min += dt / 60.0
            newT[z.id] = T_new

        self.T = newT
        self.kwh += power_w * dt / 3.6e6
        self.last_power_w = power_w
        self.t += dt
        return {"t": self.t, "t_out": t_out, "power_w": power_w}

    # ---- reporting -----------------------------------------------------
    def metrics(self) -> dict:
        return {
            "kwh": round(self.kwh, 2),
            "cost_rs": round(self.kwh * TARIFF, 1),
            "co2_kg": round(self.kwh * GRID_CO2, 2),
            "viol_min": round(self.viol_min, 1),
            "hot_deg_min": round(self.hot_deg_min, 1),
            "cold_deg_min": round(self.cold_deg_min, 1),
        }

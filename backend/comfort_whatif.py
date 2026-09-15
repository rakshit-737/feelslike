"""Comfort vs energy trade-off: SIMULATED / WHAT-IF, never a guaranteed outcome.

Reuses the what-if engine's isolation rules: both runs step CLONES of the live twin and
constraint store with the SAME controller; `whatif.state_fingerprint` proves the inputs
were not mutated. The "proposed" run applies one comfort action on top of the
controller's own output for one zone:
  cool   setpoint -1 K      raise  setpoint +1 K      vent   fan level +1
  auto   picked from the comfort engine's primary cause (warm->cool, cold->raise,
         high_co2->vent, humid->cool; none -> cool)
Comfort per step = the engine's score for the zone (from the clone's temperature, RH,
the telemetry CO2 mass balance and occupancy); energy = kWh over the horizon.
"""
from __future__ import annotations

from backend import comfort as C
from backend import telemetry, whatif
from sim.twin import COP, DT, FAN_W, ZONES

SP_LIMITS = (21.5, 29.0)          # sim/controllers.py hard envelope
ACTIONS = {"cool": "Lower the setpoint by 1 K", "raise": "Raise the setpoint by 1 K",
           "vent": "Increase the fan level by 1"}


class _Override:
    def __init__(self, inner, zone, action):
        self.inner, self.zone, self.action = inner, zone, action

    def act(self, twin, store=None):
        sps, vents = self.inner.act(twin, store)
        sps, vents = dict(sps), dict(vents)
        sp = sps.get(self.zone)
        if self.action in ("cool", "raise") and sp is not None:
            d = -1.0 if self.action == "cool" else 1.0
            sps[self.zone] = min(SP_LIMITS[1], max(SP_LIMITS[0], sp + d))
        elif self.action == "vent":
            vents[self.zone] = min(2, int(vents.get(self.zone, 0) or 0) + 1)
        return sps, vents


def pick_action(assessment: dict) -> str:
    keys = set(assessment.get("issues") or [])
    for issue, act in (("warm", "cool"), ("high_co2", "vent"), ("cold", "raise"), ("humid", "cool")):
        if issue in keys:
            return act
    return "cool"


def _run(twin, store, controller, cfg, zone, steps, co2_init):
    tw, st = twin.clone(), store.clone()
    tel = telemetry.TelemetryStore(capacity=steps + 2)
    tel.co2.update({k: float(v) for k, v in (co2_init or {}).items() if k in tel.co2})
    kwh0, zk0 = tw.kwh, tw.kwh_by_zone[zone]
    scores, occ_scores = [], []
    for _ in range(steps):
        sps, vents = controller.act(tw, st)
        kb = dict(tw.kwh_by_zone)
        tw.step(sps, vents)
        pw = {z.id: (tw.kwh_by_zone[z.id] - kb[z.id]) * 3.6e6 / DT for z in ZONES}
        cw = {z.id: max(0.0, (pw[z.id] - FAN_W.get(int(vents.get(z.id, 0) or 0), 0.0)) * COP) for z in ZONES}
        row = tel.record(tw, None, pw, cw)["zones"][zone]
        a = C.assess(C.ComfortReading(zone_id=zone, temp_c=row["temp"], rh_pct=row["rh"], co2_ppm=row["co2"],
                                      occupancy=row["occ"], occupancy_pct=row["occ_pct"]), cfg)
        if a["score"] is not None:
            scores.append(a["score"])
            if a["comfort_relevant"]:
                occ_scores.append(a["score"])
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None           # noqa: E731
    return {"comfort_score": mean(scores), "occupied_comfort_score": mean(occ_scores),
            "occupied_steps": len(occ_scores), "energy_kwh": round(tw.kwh - kwh0, 3),
            "zone_energy_kwh": round(tw.kwh_by_zone[zone] - zk0, 3)}


def tradeoff(twin, store, controller_factory, cfg, zone: str, action: str = "auto",
             horizon_h: float = 1.0, co2_init: dict | None = None, assessment: dict | None = None) -> dict:
    if action == "auto":
        action = pick_action(assessment or {})
    if action not in ACTIONS:
        raise ValueError(f"action must be auto or one of {list(ACTIONS)}")
    if not 0 < horizon_h <= 6:
        raise ValueError("horizon_h must be > 0 and <= 6")
    steps = max(1, int(round(horizon_h * 3600 / DT)))
    before = whatif.state_fingerprint(twin, store)
    cur = _run(twin, store, controller_factory(), cfg, zone, steps, co2_init)
    prop = _run(twin, store, _Override(controller_factory(), zone, action), cfg, zone, steps, co2_init)
    delta = {k: (round(prop[k] - cur[k], 3) if prop[k] is not None and cur[k] is not None else None)
             for k in ("comfort_score", "occupied_comfort_score", "energy_kwh", "zone_energy_kwh")}
    note = None
    if cur["occupied_steps"] == 0:
        note = "The zone stays unoccupied over this horizon, so the comfort change affects nobody."
    return {"kind": "predicted", "label": "SIMULATED / WHAT-IF", "zone_id": zone, "action": action,
            "action_text": ACTIONS[action], "horizon_h": horizon_h, "current": cur, "proposed": prop,
            "delta": delta, "note": note,
            "isolation_verified": whatif.state_fingerprint(twin, store) == before,
            "method": ("Both runs step clones of the live twin + constraint store with the same controller; "
                       "'proposed' applies the action for this zone on top of the controller output. "
                       "A what-if estimate, not a guaranteed future result.")}

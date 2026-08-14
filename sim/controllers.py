"""Controllers: the wasteful baseline, a reactive thermostat, and FeelsLike's
constraint-aware controller (the honest fallback if the RL agent isn't ready).

Interface: controller.act(twin, constraint_store) -> (setpoints, vents)
where setpoints: zone_id -> degC | None and vents: zone_id -> 0|1|2.
"""
from __future__ import annotations

from sim.twin import ZONES


class StaticSchedule:
    """What real facilities teams do to pre-empt complaints: overcool the whole
    building on a fixed schedule. This is the baseline we race against."""

    name = "Static 22degC schedule (baseline)"

    def act(self, twin, store=None):
        h, day = twin.hour, twin.day
        on = (7.0 <= h < 21.0) if day % 7 < 5 else (8.0 <= h < 18.0)
        sp = 22.0 if on else None
        return ({z.id: sp for z in ZONES},
                {z.id: (1 if on else 0) for z in ZONES})


class ReactiveComfort:
    """Per-zone thermostat at a fixed occupied setpoint. Better, still dumb:
    it reacts only to current occupancy, never to people or forecasts."""

    name = "Reactive thermostat 24degC"

    def act(self, twin, store=None):
        sps, vents = {}, {}
        for z in ZONES:
            occ = twin.occupancy_now(z.id)
            sps[z.id] = 24.0 if occ > 0 else None
            vents[z.id] = 1 if occ > 0 else 0
        return sps, vents


class ConstraintAware:
    """FeelsLike rules controller: occupancy-aware setbacks + 30-min pre-cool
    + live complaint constraints from the store. Also the RL fallback flag."""

    name = "FeelsLike (constraint-aware)"

    def __init__(self, base_occupied: float = 25.0, base_precool: float = 26.0,
                 base_unoccupied: float | None = 28.5):
        self.base_occupied = base_occupied
        self.base_precool = base_precool
        self.base_unoccupied = base_unoccupied

    def act(self, twin, store=None):
        offsets = store.zone_adjustments(twin.t) if store is not None else {}
        sps, vents = {}, {}
        for z in ZONES:
            occ_now = twin.occupancy_now(z.id)
            occ_soon = twin.occupancy_now(z.id, ahead_h=0.5)
            if occ_now > 0:
                sp = self.base_occupied
                vent = 2 if occ_now >= 20 else 1
            elif occ_soon > 0:
                sp, vent = self.base_precool, 1          # pre-cool before people arrive
            else:
                sp, vent = self.base_unoccupied, 0
            adj = offsets.get(z.id)
            if adj is not None and sp is not None:
                sp = min(max(sp + adj["setpoint_offset"], 21.5), 29.0)
                vent = min(max(vent + adj["vent_delta"], 0), 2)
            sps[z.id] = sp
            vents[z.id] = vent
        return sps, vents


class RLPolicy:
    """Wraps a trained stable-baselines3 model. Falls back loudly if missing."""

    name = "FeelsLike (PPO agent)"

    def __init__(self, model_path: str = "rl/models/ppo_feelslike.zip"):
        from stable_baselines3 import PPO  # heavy import, only when used
        self.model = PPO.load(model_path)
        self.fallback = ConstraintAware()

    def act(self, twin, store=None):
        from sim.env import build_obs, apply_action
        import numpy as np
        obs = build_obs(twin, store)
        action, _ = self.model.predict(np.array(obs, dtype="float32"), deterministic=True)
        return apply_action(twin, store, action)

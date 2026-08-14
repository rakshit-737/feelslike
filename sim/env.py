"""Gymnasium environment wrapping the digital twin for RL training.

Episode = 1 simulated day, control decision every 5 minutes (288 steps).
Action  = per-zone setpoint offset in [-2, +2] degC around the rules base.
Reward  = -(energy kWh) - W_COMFORT * (degree-minutes outside band)
          - W_COMPLAINT * (unmet active-complaint pressure)

Synthetic complaints are injected during training so the agent learns to
respect human constraints, not just the static band.
"""
from __future__ import annotations

import math
import random

from sim.twin import DigitalTwin, ZONES, ZONE_IDS
from sim.controllers import ConstraintAware
from backend.constraints import ConstraintStore, Constraint

CONTROL_DT = 300.0     # act every 5 sim-minutes
W_COMFORT = 0.12       # per degree-minute
W_COMPLAINT = 0.6      # per unmet-complaint degree-minute
BASE = ConstraintAware()


def build_obs(twin: DigitalTwin, store) -> list:
    h = twin.hour
    obs = [twin.T[z] / 40.0 for z in ZONE_IDS]
    obs += [twin.weather_fn(twin.t) / 45.0,
            math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)]
    obs += [min(twin.occupancy_now(z), 30) / 30.0 for z in ZONE_IDS]
    adj = store.zone_adjustments(twin.t) if store else {}
    obs += [(adj.get(z, {}).get("setpoint_offset", 0.0)) / 2.0 for z in ZONE_IDS]
    return obs  # length 5 + 3 + 5 + 5 = 18


def apply_action(twin: DigitalTwin, store, action):
    """Offsets the rules controller's setpoints by the agent's action."""
    sps, vents = BASE.act(twin, store)
    for i, z in enumerate(ZONE_IDS):
        if sps[z] is not None:
            sps[z] = min(max(sps[z] + float(action[i]), 21.5), 29.0)
    return sps, vents


try:
    import gymnasium as gym
    import numpy as np

    class FeelsLikeEnv(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self, seed: int = 0):
            super().__init__()
            self._seed = seed
            self.observation_space = gym.spaces.Box(-2.0, 2.0, shape=(18,), dtype=np.float32)
            self.action_space = gym.spaces.Box(-2.0, 2.0, shape=(5,), dtype=np.float32)

        def reset(self, seed=None, options=None):
            super().reset(seed=seed)
            rnd = random.Random(seed if seed is not None else self._seed)
            self.twin = DigitalTwin(seed=rnd.randrange(10000))
            self.twin.t = rnd.randrange(0, 5) * 86400.0        # random weekday
            self.day_end = self.twin.t + 86400.0
            self.store = ConstraintStore()
            self._plan_complaints(rnd)
            return np.array(build_obs(self.twin, self.store), dtype=np.float32), {}

        def _plan_complaints(self, rnd):
            self.pending = []
            for _ in range(rnd.randrange(0, 4)):
                at = self.twin.t + rnd.uniform(2, 10) * 3600.0
                zone = rnd.choice(ZONE_IDS)
                issue = rnd.choice(["too_hot", "too_cold", "stuffy"])
                self.pending.append((at, zone, issue, rnd.randrange(1, 4)))
            self.pending.sort()

        def step(self, action):
            while self.pending and self.pending[0][0] <= self.twin.t:
                _, zone, issue, sev = self.pending.pop(0)
                self.store.add(Constraint.from_issue(zone, issue, sev, 0.9, self.twin.t))
            k0, hd0, cd0 = self.twin.kwh, self.twin.hot_deg_min, self.twin.cold_deg_min
            sps, vents = apply_action(self.twin, self.store, action)
            for _ in range(int(CONTROL_DT // 60)):
                self.twin.step(sps, vents)
            d_kwh = self.twin.kwh - k0
            d_deg = (self.twin.hot_deg_min - hd0) + (self.twin.cold_deg_min - cd0)
            unmet = self.store.unmet_pressure(self.twin, CONTROL_DT / 60.0)
            reward = -d_kwh - W_COMFORT * d_deg - W_COMPLAINT * unmet
            done = self.twin.t >= self.day_end
            obs = np.array(build_obs(self.twin, self.store), dtype=np.float32)
            return obs, float(reward), done, False, {"kwh": self.twin.kwh}

except ImportError:  # gymnasium not installed: core sim still works
    FeelsLikeEnv = None

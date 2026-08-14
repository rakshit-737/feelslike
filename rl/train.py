"""Train the PPO agent on the digital twin. Start this EARLY (hour 4) and let
it run in the background / overnight. Keep the ConstraintAware controller as
the demo fallback until eval proves the agent wins.

Setup (only needed for RL):  pip install stable-baselines3 torch
Run:    python -m rl.train --steps 2000000
Watch:  learning curve saved to rl/models/progress.csv (plot it for the slide!)
"""
from __future__ import annotations

import argparse
from pathlib import Path

MODELS = Path(__file__).resolve().parent / "models"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=2_000_000)
    ap.add_argument("--envs", type=int, default=8)
    args = ap.parse_args()

    from stable_baselines3 import PPO
    from stable_baselines3.common.logger import configure
    from stable_baselines3.common.vec_env import SubprocVecEnv, VecMonitor

    from sim.env import FeelsLikeEnv

    MODELS.mkdir(exist_ok=True)
    # VecMonitor records episode rewards -> rollout/ep_rew_mean in progress.csv
    # (that's the learning-curve column the pitch slide plots).
    env = VecMonitor(SubprocVecEnv([lambda i=i: FeelsLikeEnv(seed=i) for i in range(args.envs)]))
    model = PPO("MlpPolicy", env, verbose=1, n_steps=512, batch_size=1024,
                learning_rate=3e-4, gamma=0.995)
    model.set_logger(configure(str(MODELS), ["stdout", "csv"]))
    model.learn(total_timesteps=args.steps, progress_bar=True)
    model.save(MODELS / "ppo_feelslike.zip")
    print(f"Saved -> {MODELS / 'ppo_feelslike.zip'}")
    print("Now run: python -m rl.evaluate   (and python -m scripts.demo_day)")


if __name__ == "__main__":
    main()

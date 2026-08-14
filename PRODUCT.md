# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: hackathon judges watching a 3-minute live pitch of the dashboard on a
laptop screen shown around (normal viewing distance, judges cluster around the
machine). Secondary: the presenting team member driving the demo under time
pressure, and office occupants in the product fiction (they interact via
Slack/web chat, not the dashboard).

## Product Purpose

FeelsLike is a digital-twin building optimizer with NLP feedback: occupants
complain in plain language ("it's stuffy in Conference Room B"), a parser turns
the complaint into a typed constraint, and a constraint-aware HVAC controller
acts on a 5-zone thermal simulation. Success = judges believe, within 3
minutes, that a building can listen — and that it saves energy without
sacrificing comfort.

## Positioning

"Efficiency without sacrifice": −26.6% energy with **zero** comfort violations
vs the static-schedule baseline, while the reactive thermostat that saves
slightly more breaks comfort for 429 minutes. The live A/B race (two identical
twins, same weather, one listening and one not) is the mechanism no neighboring
team can truthfully copy without building the twin.

## Operating Context

Demo script (IMPLEMENTATION.md §9): type a complaint → parsed JSON chip appears
→ zone shifts on floor plan; teammate types an opposing complaint → CONFLICT
badge + weighted compromise; crank sim to 960× → racing energy meters open a
gap over a simulated week; money/carbon tiles scale the story. Backend is
FastAPI serving `/api/state` polled by the single-file dashboard
(`dashboard/index.html`); complaint entry via web chat on the dashboard or
Slack webhook. Venue Wi-Fi may die: the offline rules parser keeps everything
working (badge switches from `llm` to `rules`).

## Capabilities and Constraints

- Single-file dashboard: all CSS/JS inline in `dashboard/index.html`, no build
  step, no external dependencies allowed to break offline.
- Live data: 5 zones (temp, setpoint, vent, occupancy, constraints), racing
  kWh meters (FeelsLike vs baseline), history chart, complaint feed with
  parse-source/latency badges, sim clock and speed control (60×/240×/960×).
- Feed actions: applied / clarify / ignored / all-clear (retraction) /
  pre-applied (comfort memory).
- Numbers shown are real simulation output — never hardcode or inflate them.
- Demo machine is a Windows laptop; modern Chromium assumed.

## Brand Commitments

Name "FeelsLike", team "Team Goldilocks", tagline "Buildings that listen".
No other binding assets — current dashboard look is scaffold output, not a
commitment; free hand to replace it.

## Evidence on Hand

Measured, reproducible: 722 vs 530 kWh over 7 simulated days (−26.6%), 0
viol-min vs 16,328; reactive counter-example −31.7% but 429 viol-min
(`evals/results_energy.json`). NLP benchmark 50 cases: rules 100% dev / 55%
held-out (`evals/results_nlp.json`). PPO ablation: 81.6 kWh / 2 viol-min vs
rules 84.4 / 0 (`rl/models/progress.csv` for the learning curve). Do not
fabricate testimonials, customers, or certifications — none exist.

## Product Principles

1. The race is the story: every screen moment should make the A/B gap (energy
   saved, comfort kept) legible in seconds.
2. Show the machinery honestly: parsed JSON, conflict arbitration,
   parse-source badges and failure cases build trust with technical judges.
3. Never break in the demo: offline fallback and graceful degradation outrank
   any feature or flourish.
4. Real numbers only: the dashboard displays simulation output verbatim;
   persuasion comes from the mechanism, not inflated claims.
5. Respect the 3 minutes: the presenter drives fast; nothing may require
   explanation that the script doesn't already provide.

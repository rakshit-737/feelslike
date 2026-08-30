# FeelsLike — Implementation Plan & Strategy

Problem statement: **Digital Twin Building Optimizer with NLP Feedback**
Team: Goldilocks · Deliverable mapping and the winning logic live in the strategy
report; this file is the *builder's* document: what to build, in what order, with
acceptance criteria, contracts, and the demo script.

---

## 1. System in one picture

```
 Occupant (Slack / web chat)
        │  "it's stuffy in Room B"
        ▼
 ┌─────────────────┐   strict JSON    ┌──────────────────────┐
 │ backend/parser  │ ───────────────► │ backend/constraints  │
 │ LLM + rules     │  zone/issue/sev  │ decay · arbitration  │
 └─────────────────┘                  └──────────┬───────────┘
        ▲  50-case benchmark                     │ setpoint offsets
        │  (evals/)                              ▼
 ┌──────┴──────────┐    weather      ┌──────────────────────┐
 │ dashboard       │ ◄────────────── │ sim/twin.py          │
 │ floor plan·chat │   /api/state    │ 5-zone RC digital    │
 │ racing meters   │                 │ twin + energy model  │
 └─────────────────┘                 └──────────┬───────────┘
                                                │ obs/reward
                                     ┌──────────▼───────────┐
                                     │ sim/env.py + rl/     │
                                     │ PPO agent (fallback: │
                                     │ ConstraintAware)     │
                                     └──────────────────────┘
```

Two twins always run in lock-step on identical weather — FeelsLike vs the
Static-22 °C baseline. Every number shown to judges is that A/B comparison.

## 2. The one contract that holds it together

Everything communicates through `ParsedComplaint` → `Constraint`. Agreed at hour
zero, never renegotiated:

```json
{"is_comfort_complaint": true, "zone_id": "zone_b", "issue": "too_hot|too_cold|stuffy|humid|drafty|other",
 "severity": 1-3, "confidence": 0.0-1.0, "reasoning": "..."}
```

Rules that prevent judge-visible failures: a `zone_id` outside the known list is
nulled (never act on a hallucinated zone); `zone_id=null` triggers a clarifying
question, never a guess; `is_comfort_complaint=false` for anything non-thermal
("the projector is broken"); constraint influence decays with 45-min half-life
and expires at 2 h; opposing constraints in one zone are resolved by
severity×confidence×recency weighted mean with a human-readable explanation.

## 3. Current, measured status (this scaffold, tested)

| Metric | Value | Where it comes from |
|---|---|---|
| Energy vs static baseline (7 days) | **−26.6%** (530 vs 722 kWh) | `python -m scripts.demo_day` |
| Comfort violations | **0 min** vs baseline's 16,328 min | same run |
| Counter-example (why not just a colder thermostat) | Reactive 24 °C: −31.7% but 429 viol-min | same run |
| NLP dev-set accuracy (offline rules) | 30/30 | `python -m evals.run_nlp_eval --rules` |
| NLP held-out accuracy (offline rules) | 11/20 exact triple (55%) | same run — honest number; LLM column needs a key |
| RL ablation (10 random days) | PPO 81.6 kWh / 2 viol-min vs rules 84.4 kWh / 0 viol-min | `python -m rl.evaluate --episodes 10` |
| RL 7-day run | PPO −29.0% but 22 viol-min (rules −26.6% at 0) | `python -m scripts.demo_day` |
| M4 decision | **ship ConstraintAware**; RL shown as trajectory | RL wins kWh but not at equal-or-better comfort |
| Retraction | "fine now in room b" clears zone constraints | POST it after a complaint |
| Comfort memory | ≥2-day (zone, issue, hour) pattern → pre-apply 30 min early | complain same zone/issue two sim-days running |
| Slack/Teams | `POST /api/slack` (form or JSON) | point slash command through a tunnel |
| Complaint → action latency | ~0 ms rules / expect 1–2 s LLM | live API |
| Conflict arbitration | works; explained compromise | POST two opposing complaints |

Honesty notes for Q&A: the 30-case dev set is what the rules were tuned on — the
held-out 20 (rules: 55%) is the number to quote for the rules parser, and the gap
is the argument for the LLM parser (run the eval with a key and report its held-out
score honestly). The zero-violation result holds because sizing is adequate in the
model — say "in simulation" on stage. The PPO agent's 2 viol-min mean is why the
demo controller stays rules-based; per-day energy differs from the 7-day table
because ablation days are single random weekdays, not a Mon–Sun week.

## 4. Milestones (48-h clock; adapt to your window)

**M0 · H0–2 — Everyone runs it.** Clone, venv, `demo_day`, dashboard up.
*Accept: all 3 teammates see the dashboard and can send a complaint.*

**M1 · H2–8 — RL training starts (Track C).** Install SB3+torch, launch
`python -m rl.train --steps 2000000` and LEAVE IT RUNNING. Tune reward weights in
`sim/env.py` only if curves are flat after ~500k steps.
*Accept: `progress.csv` shows reward climbing; checkpoint saved.*

**M2 · H2–12 — Slack in (Track A).** Slack app (or Teams webhook) forwarding
messages to `POST /api/complaint`; bot replies with the parsed action + explanation.
*Accept: complaint typed in Slack changes a zone on the dashboard in <10 s.*

**M3 · H2–12 — Benchmark hardening (Track B).** Grow `evals/benchmark.json` to 50
with held-out cases (sarcasm, Hinglish, typos, multi-zone, "it's fine now" retraction
— note: retraction is a known limitation, handle or disclose). Run with an API key;
record LLM vs rules table.
*Accept: honest accuracy table exists; failure cases screenshot-ready.*

**M4 · H12–24 — RL beats rules or gets cut.** `python -m rl.evaluate --episodes 10`.
RL wins on kWh at equal-or-better violations → flip demo controller to `RLPolicy`.
Otherwise ship ConstraintAware and present RL as "training trajectory" with curves.
*Accept: decision made; ablation table exported.*

**M5 · H24–36 — Polish + evidence pass.** Comfort-memory stretch feature; dashboard
numbers frozen; export learning curve PNG; record the full backup demo video.
*Accept: backup video exists on two phones.*

**M6 · H36–48 — Pitch.** Slides from strategy report; 3 full rehearsals; Q&A drill
(below). *Accept: 3-minute run-through lands at 2:50 twice in a row.*

Team split (3–5 people): A = integrations/frontend, B = NLP/eval, C = RL/sim,
(D = pitch owner, E = floater/hardware).

## 5. Where each judging criterion gets scored

| Deck's criterion | Your artifact |
|---|---|
| NLP accuracy without hallucination | benchmark table + guardrails (null-zone clarify, non-complaint ignore) — demo the projector message |
| Simulated optimization | demo_day table + RL ablation + learning curves |
| End-to-end integration | live Slack → LLM → constraint → twin → dashboard in <10 s |
| Frictionless UX | one chat message vs a facilities ticket; no forms, no app installs |

### 5b. Problem-statement coverage map (checked against the brief PPTX, 2026-08-30)

| Brief asks for | Where it lives | Status |
|---|---|---|
| NL ingestion via Teams/Slack/web | `POST /api/slack` (form + JSON), web chat, `dashboard/occupant.html` | shipped |
| LLM "feels-like" translation → constraints (location, temp offset, humidity) | `backend/parser.py` → `Constraint` (`too_hot/too_cold/stuffy/humid/drafty`, °C offsets + vent deltas) | shipped, blind-probe measured |
| Digital twin; test adjustments before physical hardware | `sim/twin.py` + clone-based what-if engine (`backend/whatif.py`) + the adapter seam with a real ESP32 implementation | shipped |
| RL agent adjusting setpoints under LLM constraints + weather + energy | `sim/env.py` (constraints in obs + reward), PPO 2M steps, ablation table, learning curve | **trained + measured; NOT the live demo controller** — M4 decision: 512.7 kWh/22 viol-min loses to rules 530.3/0 at equal-comfort; presented as trajectory |
| Optimize against real-time weather and **pricing** data | weather: seeded synthetic (A/B determinism) + `fetch_openmeteo` adapter path; pricing: **flat ₹9/kWh only** | **partial — flat tariff is the honest label**; cost objective exists, and `OBJECTIVE_WEIGHTS`' cost row is the documented landing place for a ToU tariff |
| Sustainability & ROI dashboard vs baseline | race meters, money/carbon tiles, analytics panel; payback math in `docs/FEASIBILITY.md` | shipped |
| ≥3 optional features | multi-zone complaints, comfort memory, conflict arbitration, what-if, maintenance detection, safety modes, privacy/PII scrub, retraction handling, hardware-in-the-loop | exceeded |

The two "partial" rows are answered proactively in Q&A (§8b) rather than hidden.

## 6. Risk register (pre-committed fallbacks)

| Risk | Trigger | Fallback (already built) |
|---|---|---|
| RL won't converge | flat curve at H20 | ship ConstraintAware; show curves as "learning in progress" |
| Wi-Fi / LLM API dies on stage | any API error | parser auto-falls back to rules (`source: "rules"` badge); demo unchanged |
| Slack fails on venue network | M2 slips | dashboard web chat is the same endpoint |
| Twin challenged as toy | judge Q | show RC equations (twin.py docstring), degree-day sanity, "calibrate-from-BMS-logs" roadmap |
| Demo machine dies | anything | backup video from M5 |

## 7. Stretch features (only after M4)

Comfort memory: mine `store.items` for recurring (zone, issue, hour) patterns;
pre-apply a low-severity constraint 30 min before the pattern's hour, and have the
bot announce it ("Room B usually runs stuffy at 2 pm — pre-ventilating").
Hardware moment: ESP32/Arduino + small 5 V fan; subscribe to `/api/state` and spin
the fan when zone_b vent > 1 — physical proof judges can feel.
Voice input: Web Speech API on the dashboard chat box (10 lines, big wow).

## 8. Judge Q&A drill (rehearse verbatim)

"How do you stop the LLM hallucinating a setpoint?" → Schema-validated JSON only;
unknown zones nulled; low confidence → clarifying question; non-comfort messages
ignored — here's the benchmark with failures shown. "Is the RL real?" → Learning
curves + ablation vs greedy and static; reward = −energy −discomfort −unmet
complaints. "Two people disagree?" → Live demo: severity-weighted compromise with
explanation (and it decays, so neither wins forever). "How real is the twin?" →
Standard 2R2C lumped-parameter model (literature-backed), zone coupling + solar +
occupancy; calibration path: fit R/C from a week of BMS logs. "Path to production?"
→ Same architecture; twin swaps for BACnet/Modbus writes; complaints already come
from the tools offices use. "Privacy?" → Zone-level aggregation, no identity needed.

### 8b. Drill v2 — hardware, feasibility, AI (finals phase; every teammate, no notes)

"How does this attach to a real building?" → Two named paths, both through the same
five adapter protocols in `backend/adapters.py`. No-BMS office (the Indian majority):
our ESP32 sensor nodes + IR blasters for split ACs + smart plugs — sensing and
on/off + mode control, honest about what a setpoint write can't do there. BMS
building: BACnet AV/AO + MSO objects or Modbus holding registers bound to the same
three writes the controller already makes — `write_setpoint`, `write_vent`,
`read_state`. The point list per zone is five reads and two writes; it is printed in
the adapters module docstring. Our demo rig is the first real implementation of that
seam, and it passes the same conformance test the simulated adapters pass.

"What does one zone cost?" → The priced bill of materials with sources and dates is
in `docs/FEASIBILITY.md`, including the 20-zone floor total and payback under the
verified Tamil Nadu commercial tariff, with every scaling assumption labeled.
[Numbers quoted from FEASIBILITY.md at rehearsal — never from memory.]

"What if a sensor drifts or dies?" → Health is INFERRED, never self-reported: the
sensor adapter checks staleness, stuck values, out-of-range, and physically
impossible readings (dew point above dry bulb). A dead node reads as a dropout, the
zone falls back to schedule control — the same behaviour as every other zone with no
constraints — and the maintenance panel raises an alert with the numeric evidence.
Nothing chases a broken reading.

"Would you trust this on a real building?" → The safety model is layered and none of
the layers is software we wrote trusting ourselves: (0) a physical switch in the rig's
actuator supply, (1) a firmware watchdog — no server response for 10 s means
actuators off, (2) a server-side envelope — setpoints clamped to 21.5–29.0 °C,
complaint offsets to ±1.8 °C, fan 0–2, heater duty-capped at 50% of any 10-minute
window. And the controller's `emergency_override` and `maintenance_lockout` modes
exist precisely because occupant input must never outrank plant safety.

"Why an RC model and not CFD?" → CFD answers "where exactly is the warm spot" at
minutes-per-frame; control needs "what will this zone do in the next quarter hour" at
milliseconds-per-decision. Lumped RC is the standard control-oriented abstraction
(2R2C per zone, literature-backed), it runs 960× faster than real time on a laptop,
and — the part we can defend physically — we fit its R and C to a logged step
response from real hardware (`scripts/fit_rc.py`; recovery within 5% on synthetic
truth at SHT31 noise). A model you can calibrate beats a model you can only render.

"Why not just occupancy sensors and a timer?" → That's our baseline, roughly: the
static schedule IS the timer, and the reactive thermostat is the sensor. The
measured answer: reactive saves 31.7% but books 429 violation-minutes — it saves by
letting people be uncomfortable. FeelsLike saves 26.6% at zero violation-minutes,
because complaints carry information a PIR can't: *which* discomfort, how severe,
and when it stops being true. A timer cannot arbitrate "too hot" against "too cold"
in the same room.

"Why HTTP and not MQTT for the rig?" → Chosen, not defaulted (decision log §8 in
STATUS.md): zero new runtime dependencies, no broker process to die on stage, works
over a phone hotspot on hostile venue Wi-Fi, and commands ride the poll response so
there is exactly one moving part. MQTT/BACnet is the production transport — the
adapter seam is what makes that a swap, not a rewrite.

"Why doesn't humidity change your kWh numbers?" → Documented modeling assumption,
not an oversight: latent load is tracked and reported (`humid_viol_min`, `mean_rh`)
but never charged to kWh, because charging it would silently move the frozen A/B
headline numbers the report already publishes. The limitation is stated in
`sim/twin.py`'s docstring and in the limitations register — we would rather show a
smaller, true number than a bigger, moved one.

"Your parser is only 45–55% exact-triple. Why ship it?" → Because the number that
matters for safety is the zone set at 90% with zero invented zones and a
clarify-don't-guess rule: a complaint with no confident zone asks a question instead
of acting. The 45% (rules) / 55% (LLM) figures are from blind probe v2, measured
once — and the probe ROTATES: v1 was burned the moment we studied its failures, the
runner refuses burned probes, and the tuned dev set's 100% is exactly the number we
refuse to quote. Wrong-but-actionable parses are bounded by severity clamps, decay,
and the ±1.8 °C offset cap; the failure log is committed, not curated.

"The brief says optimize against pricing data — do you?" → Against a flat verified
commercial tariff, yes — and we say "flat" out loud. With a flat ₹/kWh, minimizing
rupees IS minimizing kilowatt-hours, which is exactly why our cost and carbon
objectives share weights (documented in DATA_CONTRACTS §1 — different weights would
be fake precision). A time-of-use tariff is the named next step and has one obvious
landing place: the cost row of `OBJECTIVE_WEIGHTS` plus a tariff curve where
`TARIFF` sits today. We chose not to invent a ToU optimization we couldn't verify.

"Why isn't the RL agent driving the demo?" → It was measured and it lost the
tiebreak we care about: PPO saves 29% but books 22 violation-minutes; the
constraint-aware controller saves 26.6% at zero. Zero-violations is the thesis, so
the demo ships the controller that holds it, and the PPO run — 2M steps, learning
curve, ablation over 10 random days — is shown as trajectory. The RL environment
does consume LLM constraints (they're in the observation and the reward), so the
brief's loop exists and is measured; we just refused to ship the weaker comfort
number. Decision logged before finals, not after a question.

"What did AI build, and what did you build?" → AI (disclosed in-product via
`AIDisclosure`, and used as an assistant throughout the code): the LLM parser path —
one measured component with an offline rules fallback, so the demo never depends on
it. We own and can whiteboard every load-bearing decision: the RC physics and its
hardware calibration, the constraint decay/arbitration design and its weights, the
safety envelope numbers, the A/B lock-step evidence design, the blind-probe and
capability-gate honesty machinery, and the hardware architecture. Ask us to draw any
of it — no notes.

## 9. The 3-minute demo script

0:00 Hook: space-heater-under-desk photo. "Buildings are deaf, so they overcool
everyone, just in case. HVAC is ~40–50% of a commercial building's electricity."
0:20 Live: type *"it's really stuffy in Conference Room B"* → parsed JSON chip,
zone shifts on floor plan, latency badge. 1:00 Teammate types *"Room B is freezing,
I'm wearing a jacket"* → CONFLICT badge, weighted compromise, explanation on screen.
"Every office has this argument; ours settles it transparently in seconds."
1:30 Crank sim to 960×: racing meters open a gap over the simulated week —
"**26.6% less energy, zero comfort violations** vs the standard schedule — and the
reactive thermostat that 'saves more' breaks comfort 429 minutes. Efficiency without
sacrifice is the whole point." Flash RL learning curve + benchmark table (include
failures). 2:20 Money/carbon tiles: scale story — for a 10,000 m² office, ~₹18–22
lakh/yr and ~160 tCO₂/yr. 2:45 Close: "Buildings have been deaf for a hundred
years. We taught one to listen." Hand judges the card: *type anything — try to
confuse it.*

## 10. Commands cheat-sheet

```bash
python -m scripts.demo_day                 # evidence table (pitch numbers)
python -m evals.run_nlp_eval --rules       # NLP benchmark, offline
python -m evals.run_nlp_eval               # NLP benchmark via LLM (needs key)
uvicorn backend.app:app --reload           # live demo at http://127.0.0.1:8000
python -m rl.train --steps 2000000         # start EARLY, runs for hours
python -m rl.evaluate --episodes 10        # ablation: RL vs rules vs baselines
```

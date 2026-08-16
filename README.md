# FeelsLike, Buildings That Listen

Team Goldilocks · Hackathon problem statement: **Digital Twin Building Optimizer with NLP Feedback**

Turn everyday complaints like *"it's stuffy in Conference Room B"* into precise,
energy-optimal HVAC actions — a building that listens, simulates the fix in its
digital twin, and cuts energy while complaints go down.

**Measured on this scaffold (7 simulated days, identical weather):**

| Controller | kWh | Comfort violations | vs baseline |
|---|---|---|---|
| Static 22 °C schedule (what buildings do today) | 722 | 16,328 min (overcooled) | — |
| Reactive thermostat 24 °C | 493 | 429 min | −31.7% |
| **FeelsLike (constraint-aware)** | **530** | **0 min** | **−26.6%** |

The story: the reactive thermostat saves slightly more but breaks comfort.
FeelsLike saves ~27% **with zero violations** — energy without sacrifice.

NLP benchmark (50 cases, `python -m evals.run_nlp_eval --rules`): offline rules
parser scores **30/30 on the dev set** and **11/20 (55% exact triple) on the
held-out set** it was never tuned on — that gap is *why* the LLM parser is the
product and the rules are the demo-day insurance. Run with an API key to get
the LLM's held-out score for the honest comparison table.

## Quickstart (Windows / VS Code)

```powershell
cd feelslike
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 1. The evidence table (your pitch slide):
python -m scripts.demo_day

# 2. The NLP benchmark:
python -m evals.run_nlp_eval --rules

# 3. The live demo (dashboard + chat + racing meters):
uvicorn backend.app:app --reload
# open http://127.0.0.1:8000  — type complaints, watch the building respond
```

macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate` — everything else identical.

Optional LLM parser: copy `.env.example` → `.env`, add `ANTHROPIC_API_KEY` (or
`OPENAI_API_KEY`), and `pip install python-dotenv`; run uvicorn with the venv
activated and the key exported. **Free option:** any OpenAI-compatible provider
works via `LLM_BASE_URL` — Groq's free tier (Llama 3.3 70B, sub-second) or
Google AI Studio (Gemini 2.5 Flash) cost nothing; see `.env.example` for the
exact three lines. **No key needed** — the offline rules parser keeps the whole
demo functional (that's your demo-day insurance).

## What's where
| Path | What it is |
|---|---|
| `IMPLEMENTATION.md` | **Read this first** — the full build plan, milestones, and demo script |
| `sim/twin.py` | 5-zone RC thermal digital twin (the physics) |
| `sim/controllers.py` | Baseline, reactive, and FeelsLike constraint-aware controllers |
| `sim/env.py` | Gymnasium RL environment (obs/action/reward) |
| `backend/parser.py` | Complaint → JSON constraint (LLM + offline rules fallback) + retraction detection |
| `backend/constraints.py` | Decaying constraints, conflict arbitration, explanations |
| `backend/memory.py` | Comfort memory: learns recurring complaint patterns, pre-applies fixes |
| `backend/app.py` | FastAPI: live sim, complaint API, Slack/Teams webhook, dashboard server |
| `dashboard/index.html` | Single-file dashboard: floor plan, chat, racing energy chart |
| `evals/` | 30-case NLP benchmark + scorer (grow this to 50) |
| `rl/train.py`, `rl/evaluate.py` | PPO training + ablation table (start training EARLY) |
| `scripts/demo_day.py` | 7-day controller comparison → results JSON |

## Status (updated 2026-08-14)

1. ✅ **RL track:** PPO trained 2M steps. Ablation (10 random days):
   PPO 81.6 kWh / 2 viol-min vs rules 84.4 kWh / **0** viol-min; 7-day run:
   PPO −29.0% but 22 viol-min vs rules −26.6% at zero. RL wins energy but not
   at equal-or-better comfort, so **the demo ships ConstraintAware** and RL is
   presented as trajectory (M4 decision per plan). Learning curve still climbing
   at 2M steps (−91.8 → −86.8 `ep_rew_mean` in `rl/models/progress.csv`) — an
   overnight run may flip the decision.
2. ✅ **Slack/Teams bot:** `POST /api/slack` accepts Slack slash-command form
   posts *and* Teams-style JSON. Point the slash command at
   `https://<your-tunnel>/api/slack` (e.g. `ngrok http 8000`). Replies with the
   parsed action + explanation in-channel; web chat remains the fallback.
3. ✅ **Benchmark at 50 cases** with a held-out split (typos, sarcasm, Hinglish,
   multi-zone, retractions, keyword traps): rules = 100% dev / 55% held-out.
   Run `python -m evals.run_nlp_eval` with an API key for the LLM column.
4. ✅ **Retraction handling:** "it's fine now in room b" clears the zone's active
   constraints (all-clear badge in feed + Slack reply) instead of filing a complaint.
5. ✅ **Comfort memory (stretch):** recurring (zone, issue, hour) patterns seen on
   ≥2 days get a gentle constraint pre-applied 30 min early, announced in the feed.
6. **Remaining — pitch:** follow the 3-minute demo script in `IMPLEMENTATION.md`
   §9; record the backup video (M5); rehearse Q&A drill (§8).

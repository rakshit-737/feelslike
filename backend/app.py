"""FeelsLike backend: live accelerated simulation + complaint API + dashboard.

Run:  uvicorn backend.app:app --reload      then open http://127.0.0.1:8000
Two twins run in lock-step on identical weather: FeelsLike (constraint-aware)
vs the Static-22degC baseline — that's the racing meter on the dashboard.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import parser
from backend.constraints import Constraint, ConstraintStore
from backend.memory import ComfortMemory
from sim.controllers import ConstraintAware, StaticSchedule
from sim.twin import DigitalTwin, ZONES, GRID_CO2, TARIFF

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class LiveSim:
    def __init__(self, seed: int = 7, speed: float = 240.0, start_hour: float = 8.0):
        self.lock = threading.Lock()
        self.speed = speed                      # sim-seconds per real-second
        self.store = ConstraintStore()
        self.memory = ComfortMemory()
        self.us = DigitalTwin(seed=seed)
        self.base = DigitalTwin(seed=seed)
        self.us.t = self.base.t = start_hour * 3600.0
        self.ctrl_us = ConstraintAware()
        self.ctrl_base = StaticSchedule()
        self.last_sps, self.last_vents = {}, {}
        self.history: list = []                 # sampled every 15 sim-min
        self.feed: list = []                    # complaint / event log
        self._next_sample = self.us.t
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        acc = 0.0
        while True:
            time.sleep(0.2)
            with self.lock:
                acc += self.speed * 0.2
                while acc >= 60.0:
                    sps, vents = self.ctrl_us.act(self.us, self.store)
                    self.us.step(sps, vents)
                    bs, bv = self.ctrl_base.act(self.base)
                    self.base.step(bs, bv)
                    self.last_sps, self.last_vents = sps, vents
                    for note in self.memory.tick(self.us, self.store):
                        self.add_feed(note)
                    acc -= 60.0
                    if self.us.t >= self._next_sample:
                        self.history.append({"t": self.us.t,
                                             "us": round(self.us.kwh, 2),
                                             "base": round(self.base.kwh, 2)})
                        self.history = self.history[-800:]
                        self._next_sample += 900.0

    def add_feed(self, entry: dict):
        entry["sim_clock"] = self.clock()
        self.feed = ([entry] + self.feed)[:30]

    def clock(self) -> str:
        d, h = self.us.day, self.us.hour
        return f"{DAYS[d % 7]} {int(h):02d}:{int((h % 1) * 60):02d}"

    def state(self) -> dict:
        with self.lock:
            adjustments = self.store.zone_adjustments(self.us.t)
            zones = []
            for z in ZONES:
                adj = adjustments.get(z.id)
                zones.append({
                    "id": z.id, "name": z.name,
                    "temp": round(self.us.T[z.id], 1),
                    "base_temp": round(self.base.T[z.id], 1),
                    "setpoint": self.last_sps.get(z.id),
                    "vent": self.last_vents.get(z.id, 0),
                    "occ": int(self.us.occupancy_now(z.id)),
                    "offset": adj["setpoint_offset"] if adj else 0.0,
                    "active_constraints": len(self.store.active(self.us.t, z.id)),
                })
            m_us, m_base = self.us.metrics(), self.base.metrics()
            saved_kwh = max(0.0, self.base.kwh - self.us.kwh)
            pct = 100.0 * saved_kwh / self.base.kwh if self.base.kwh > 1e-6 else 0.0
            return {
                "sim": {"clock": self.clock(), "hour": round(self.us.hour, 2),
                        "t_out": round(self.us.weather_fn(self.us.t), 1),
                        "speed": self.speed},
                "zones": zones,
                "meters": {"us": m_us, "base": m_base,
                           "saved_kwh": round(saved_kwh, 2),
                           "saved_pct": round(pct, 1),
                           "saved_rs": round(saved_kwh * TARIFF, 1),
                           "saved_co2": round(saved_kwh * GRID_CO2, 2)},
                "history": self.history,
                "feed": self.feed,
            }


sim = LiveSim()
app = FastAPI(title="FeelsLike")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


class ComplaintIn(BaseModel):
    text: str
    author: str = "occupant"


@app.get("/")
def index():
    return FileResponse(DASHBOARD)


@app.get("/api/state")
def state():
    return sim.state()


def handle_complaint(text: str, author: str) -> dict:
    """Shared complaint pipeline for the web chat and the Slack endpoint."""
    parsed, source, latency = parser.parse(text)
    entry = {"author": author, "text": text, "source": source,
             "latency_ms": latency, "parsed": parsed.model_dump()}
    if parser.detect_retraction(text):
        if parsed.zone_id is None:
            entry["action"] = "noted — glad it's better (no zone named, nothing cleared)"
            sim.add_feed(entry)
            return {"ok": True, "action": "noted", **entry}
        with sim.lock:
            n = sim.store.clear_zone(parsed.zone_id, sim.us.t)
        entry["action"] = f"all-clear — {n} constraint(s) cleared in {parsed.zone_id}"
        sim.add_feed(entry)
        return {"ok": True, "action": "cleared", "cleared": n, **entry}
    if not parsed.is_comfort_complaint:
        entry["action"] = "ignored — not a comfort complaint"
        sim.add_feed(entry)
        return {"ok": True, "action": "ignored", **entry}
    if parsed.zone_id is None:
        entry["action"] = ("clarify — which zone? (" +
                           ", ".join(z.name for z in ZONES) + ")")
        sim.add_feed(entry)
        return {"ok": True, "action": "clarify", **entry}
    with sim.lock:
        c = Constraint.from_issue(parsed.zone_id, parsed.issue, parsed.severity,
                                  parsed.confidence, sim.us.t, text, author)
        explanation = sim.store.add(c)
    entry["action"] = "applied"
    entry["explanation"] = explanation
    sim.add_feed(entry)
    return {"ok": True, "action": "applied", **entry}


@app.post("/api/complaint")
def complaint(body: ComplaintIn):
    return handle_complaint(body.text, body.author)


ZONE_NAMES = {z.id: z.name for z in ZONES}


@app.post("/api/slack")
async def slack_command(request: Request):
    """Slack slash-command webhook. Point the command's Request URL here
    (e.g. via `ngrok http 8000` -> https://xxx.ngrok.app/api/slack).
    Slack posts application/x-www-form-urlencoded and wants a reply in <3 s.
    Teams outgoing-webhook JSON bodies work too (fields: text, from.name)."""
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        author = str((body.get("from") or {}).get("name", "teams"))
    else:
        form = await request.form()
        text = str(form.get("text", "")).strip()
        author = str(form.get("user_name", "slack"))
    if not text:
        return {"response_type": "ephemeral",
                "text": "Tell the building what feels wrong — e.g. "
                        "`/feelslike it's stuffy in Conference Room B`"}

    r = handle_complaint(text, author)
    p = r["parsed"]
    zone = ZONE_NAMES.get(p["zone_id"], p["zone_id"])
    badge = f"[{r['source']} · {r['latency_ms']} ms]"
    action = r["action"]  # long form, e.g. "all-clear — 2 constraint(s) cleared..."
    if action == "applied":
        reply = (f":thermometer: Got it — *{p['issue'].replace('_', ' ')}* in "
                 f"*{zone}* (severity {p['severity']}). "
                 f"{r['explanation']['summary']} {badge}")
    elif action.startswith("all-clear"):
        reply = f":white_check_mark: All clear for *{zone}* — {r['cleared']} constraint(s) lifted. {badge}"
    elif action.startswith("clarify"):
        reply = (":grey_question: Which zone? I know: "
                 + ", ".join(ZONE_NAMES.values()) + f". {badge}")
    else:  # ignored / noted
        reply = f":speech_balloon: Noted, but that doesn't look like a comfort complaint. {badge}"
    return {"response_type": "in_channel", "text": reply}


@app.post("/api/speed")
def set_speed(body: dict):
    with sim.lock:
        sim.speed = float(max(1.0, min(3600.0, body.get("speed", 240))))
    return {"ok": True, "speed": sim.speed}

"""Complaint parser: LLM structured extraction with a deterministic rules
fallback, so the demo works even with no API key / no Wi-Fi.

parse(text) -> (ParsedComplaint, source, latency_ms)
Provider picked from env: ANTHROPIC_API_KEY, else OPENAI_API_KEY, else rules.
"""
from __future__ import annotations

import json
import os
import re
import time

from pydantic import BaseModel, Field, ValidationError

from backend.prompts import build_system_prompt
from sim.twin import ZONES

ISSUES = {"too_hot", "too_cold", "stuffy", "humid", "drafty", "other"}
VALID_ZONES = {z.id for z in ZONES}


class ParsedComplaint(BaseModel):
    is_comfort_complaint: bool
    zone_id: str | None = None
    issue: str = "other"
    severity: int = Field(default=1, ge=1, le=3)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""

    def clean(self) -> "ParsedComplaint":
        if self.zone_id not in VALID_ZONES:
            self.zone_id = None            # never act on a hallucinated zone
        if self.issue not in ISSUES:
            self.issue = "other"
        return self


# ---------------- rules fallback (offline, deterministic) ----------------
# Order matters: most-specific issues first (drafty before cold — "cold air
# blowing on me" is a draft complaint, not a temperature one).
_KEYWORDS = [
    ("drafty", ["draft", "hawa lag", "blowing directly", "air blowing", "vent blast"]),
    ("stuffy", ["stuffy", "stale", "suffocat", "can't breathe", "cant breathe",
                "airless", "ghutan", "no air", "some air"]),
    ("humid", ["humid", "sticky", "chip-chip", "chipchip", "muggy", "clammy"]),
    ("too_cold", ["ac kam", "freez", "cold", "chilly", "jacket", "sweater", "thand",
                  "sardi", "icebox", "ice age", "arctic", "shiver"]),
    ("too_hot", ["ac tez", "ac badha", "hot", "warm", "boiling", "baking", "sauna",
                 "oven", "furnace", "garmi", "garam", "pasina", "sweat", "burning",
                 "melting", "heat", "sun off", "feels like 3"]),
]
_SEV3 = ["dying", "unbearable", "can't work", "cant work", "burning up", "freezing",
         "urgent", "bahut zyada"]
_SEV2 = ["really", "very", "so ", "too ", "bahut", "extremely", "zyada", "!!"]
_LETTER_ZONE = {"a": "zone_a", "b": "zone_b", "c": "zone_c", "d": "zone_d", "e": "zone_e"}

# All-clear / retraction: "it's fine now" should CANCEL a zone's constraints,
# not file a new complaint (and never trip on issue keywords like "heat" in
# "the heat issue is fixed now").
_RETRACTIONS = ["fine now", "better now", "fixed now", "all good", "sorted now",
                "resolved", "ok now", "okay now", "back to normal",
                "theek ho gaya", "thik ho gaya", "ab theek", "ab thik"]
# Contrast markers mean the discomfort is ongoing ("all good but cabin is hot").
_ONGOING = ["but ", "still ", "except", "abhi bhi", "phir bhi"]


def detect_retraction(text: str) -> bool:
    t = " " + text.lower().strip() + " "
    return any(p in t for p in _RETRACTIONS) and not any(m in t for m in _ONGOING)


def _find_zone(t: str) -> str | None:
    for z in ZONES:
        if any(a in t for a in z.aliases):
            return z.id
    # "room B" / "zone C" style references
    m = re.search(r"\b(?:room|zone)\s*([a-e])\b", t)
    if m:
        return _LETTER_ZONE[m.group(1)]
    # bare leading letter: "b is stuffy again" (never match the article 'a')
    m = re.match(r"\s*([b-e])\b", t)
    return _LETTER_ZONE[m.group(1)] if m else None


def rules_parse(text: str) -> ParsedComplaint:
    t = " " + text.lower().strip() + " "
    zone = _find_zone(t)
    if detect_retraction(text):
        return ParsedComplaint(is_comfort_complaint=False, zone_id=zone, issue="other",
                               severity=1, confidence=0.7,
                               reasoning="Rules: retraction/all-clear detected.").clean()
    issue = None
    for name, words in _KEYWORDS:
        if any(w in t for w in words):
            issue = name
            break
    if issue is None:
        return ParsedComplaint(is_comfort_complaint=False, zone_id=zone, issue="other",
                               severity=1, confidence=0.6,
                               reasoning="Rules: no comfort keyword matched.").clean()
    sev = 3 if any(w in t for w in _SEV3) else 2 if any(w in t for w in _SEV2) else 1
    return ParsedComplaint(is_comfort_complaint=True, zone_id=zone, issue=issue,
                           severity=sev, confidence=0.55,
                           reasoning="Rules-based keyword match.").clean()


# ---------------- LLM providers ------------------------------------------
def _extract_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in LLM output")
    return json.loads(m.group(0))


def _call_anthropic(text: str) -> ParsedComplaint:
    import httpx

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"},
        json={"model": os.environ.get("LLM_MODEL", "claude-haiku-4-5"),
              "max_tokens": 300,
              "system": build_system_prompt(ZONES),
              "messages": [{"role": "user", "content": text}]},
        timeout=15,
    )
    r.raise_for_status()
    return ParsedComplaint(**_extract_json(r.json()["content"][0]["text"])).clean()


def _call_openai(text: str) -> ParsedComplaint:
    """OpenAI-compatible chat endpoint. Works with any provider that speaks the
    same protocol — set LLM_BASE_URL to use a free tier:
      Groq:   https://api.groq.com/openai/v1          (model llama-3.3-70b-versatile)
      Gemini: https://generativelanguage.googleapis.com/v1beta/openai
              (model gemini-2.5-flash)
      Ollama: http://localhost:11434/v1               (model llama3.2, key can be any string)
    """
    import httpx

    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": build_system_prompt(ZONES)},
                           {"role": "user", "content": text}]},
        timeout=15,
    )
    r.raise_for_status()
    return ParsedComplaint(**_extract_json(r.json()["choices"][0]["message"]["content"])).clean()


def parse(text: str, force_rules: bool = False):
    """Returns (ParsedComplaint, source, latency_ms)."""
    t0 = time.perf_counter()
    if not force_rules:
        for env, fn in (("ANTHROPIC_API_KEY", _call_anthropic),
                        ("OPENAI_API_KEY", _call_openai)):
            if os.environ.get(env):
                try:
                    out = fn(text)
                    return out, "llm", round((time.perf_counter() - t0) * 1000)
                except (Exception, ValidationError):
                    break  # graceful degradation to rules
    out = rules_parse(text)
    return out, "rules", round((time.perf_counter() - t0) * 1000)

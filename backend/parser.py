"""Complaint understanding: a staged NLP pipeline with an LLM front door and a
deterministic rules fallback, so the demo works with no API key / no Wi-Fi.

Why staged rather than one big keyword list: every failure mode we hit (typos,
Hinglish/Tamil-English, sarcasm, "the coffee machine is hot") is a DIFFERENT
kind of failure and needs its own repair. A flat regex list makes each fix a
special case; explicit stages make each fix a one-line lexicon entry.

    raw -> normalize -> detect_language -> detect_retraction -> extract_zones
        -> detect_intent -> detect_severity -> resolve_context -> score_confidence
        -> ParsedComplaint

Every lexicon lives at module level as plain data. Adding "kadumaiyaana kulir"
or a new appliance noun is one list entry, never a new branch. That is the
extensibility contract for this file.

parse(text) -> (ParsedComplaint, source, latency_ms)
Provider picked from env: ANTHROPIC_API_KEY, else OPENAI_API_KEY, else rules.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError, model_validator

try:  # load .env from the project root so keys work without shell exports
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from backend.contracts import ISSUES as _ISSUE_TUPLE, LANGUAGES as _LANGUAGES
from backend.prompts import build_system_prompt
from sim.twin import ZONES

ISSUES = set(_ISSUE_TUPLE)          # kept as a set: pre-existing public name
VALID_ZONES = {z.id for z in ZONES}


# ==========================================================================
# The model
# ==========================================================================

class ParsedComplaint(BaseModel):
    """One occupant message, understood.

    INPUT: constructed by rules_parse(), by an LLM response dict, or directly by
      a caller (all fields except is_comfort_complaint have defaults).
    OUTPUT: a validated record. zone_id is the BACK-COMPAT single zone and always
      equals zone_ids[0] (or None); zone_ids is the authoritative multi-zone list.
    SIDE EFFECTS: none.
    ERROR STATES: pydantic ValidationError only if is_comfort_complaint is absent
      or non-coercible. Everything else degrades: unknown issues become "other",
      hallucinated zone ids are dropped by clean(), out-of-range severity and
      confidence are clamped by the pre-validator.
    """
    is_comfort_complaint: bool
    zone_id: str | None = None
    issue: str = "other"
    severity: int = Field(default=1, ge=1, le=3)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""
    # --- additive (shared contract v1); older callers may ignore all of these --
    zone_ids: list[str] = Field(default_factory=list)
    zone_confidence: dict[str, float] = Field(default_factory=dict)
    requires_clarification: bool = False
    language: str = "en"
    normalized_text: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        """Tolerant coercion so a sloppy LLM response never 500s the endpoint."""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        z = d.get("zone_ids")
        if z is None:
            z = []
        elif isinstance(z, str):
            z = [z]                                  # LLM returned a bare string
        elif isinstance(z, (list, tuple)):
            z = [str(x) for x in z if x]
        else:
            z = []
        if not z and d.get("zone_id"):
            z = [str(d["zone_id"])]                  # LLM used the old schema
        d["zone_ids"] = z
        if "severity" in d:
            try:
                d["severity"] = max(1, min(3, int(float(d["severity"]))))
            except (TypeError, ValueError):
                d["severity"] = 1
        if "confidence" in d:
            try:
                d["confidence"] = max(0.0, min(1.0, float(d["confidence"])))
            except (TypeError, ValueError):
                d["confidence"] = 0.5
        if isinstance(d.get("issue"), str):
            d["issue"] = d["issue"].strip().lower()
        if d.get("language") not in _LANGUAGES:
            d["language"] = "en"
        zc = d.get("zone_confidence")
        d["zone_confidence"] = zc if isinstance(zc, dict) else {}
        return d

    def clean(self) -> "ParsedComplaint":
        """Drop hallucinated zones/issues and re-derive zone_id. Idempotent."""
        seen, keep = set(), []
        for z in self.zone_ids:
            if z in VALID_ZONES and z not in seen:
                seen.add(z)
                keep.append(z)
        if not keep and self.zone_id in VALID_ZONES:
            keep = [self.zone_id]                    # never act on a hallucinated zone
        self.zone_ids = keep
        self.zone_id = keep[0] if keep else None
        self.zone_confidence = {k: round(float(v), 3)
                                for k, v in self.zone_confidence.items() if k in seen}
        if self.issue not in ISSUES:
            self.issue = "other"
        return self


# ==========================================================================
# Stage 1 data: normalization
# ==========================================================================

# Emoji kept as sentiment/thermal hints; everything else is stripped to a space.
EMOJI_HINTS: dict = {
    "\U0001F975": "hot",     # hot face
    "\U0001F525": "hot",     # fire
    "☀": "hot",         # sun
    "\U0001F624": "hot",     # steam from nose
    "\U0001F613": "hot",     # sweat
    "\U0001F630": "hot",
    "\U0001F4A6": "humid",   # sweat droplets
    "\U0001F976": "cold",    # cold face
    "❄": "cold",        # snowflake
    "\U0001F9CA": "cold",    # ice
    "\U0001F927": "stuffy",  # sneezing
    "\U0001F643": "sarcasm", # upside-down
    "\U0001F642": "sarcasm",
}

# Chat shorthand -> words. Token-level, so "ur" expands but "your" is untouched.
CONTRACTIONS: dict = {
    "u": "you", "ur": "your", "pls": "please", "plz": "please", "thx": "thanks",
    "thnx": "thanks", "rn": "right now", "asap": "urgent", "temp": "temperature",
    "aircon": "ac", "a/c": "ac", "ac's": "ac", "hvac": "ac", "abt": "about",
    "b4": "before", "cud": "could", "shud": "should", "sm": "some", "smth": "something",
}

# Words that must NEVER be fuzzy-corrected: they are real English words, so an
# edit-1 neighbour of a lexicon term is a coincidence, not a typo. Without this
# guard "meeting" fuzzes to "melting" and "thanks" fuzzes to "thand".
FUZZ_STOPWORDS: frozenset = frozenset("""
about after again against alone along already also always another anyone
anything around because before being below better between both bring broken
called cannot coffee coming computer could degrees desk display doing during
either every everyone feeling feels first focus forecast friday getting going
great hands having hello inside issue keyboard laptop later lunch machine
maybe meeting minutes monday money monitor morning mouse moving needs never
night nothing office others outside people person phone please printer problem
projector really reception right room rooms saturday screen second seems
sending service should since small something someone speaker started sticky
sunday swamped system
table taking thanks their there thermostat these thing things think third those
three through thursday today together tomorrow tonight trying tuesday under until
using visitor visitors waiting wanted watch water wearing wednesday weather
weekend where which while window windows without working would writing wrong
""".split())


@dataclass
class Normalized:
    """Output of stage 1. `text` is what every later stage matches against."""
    raw: str = ""
    text: str = ""            # lowercased, apostrophe-free, runs of 3+ chars -> 2
    squeezed: str = ""        # same but runs of 3+ -> 1 ("sooo" -> "so")
    padded: str = ""          # " " + text + " ", for space-anchored phrases
    tokens: list = field(default_factory=list)      # list[str]
    offsets: list = field(default_factory=list)     # char index of each token in text
    emoji_hints: list = field(default_factory=list)
    caps_ratio: float = 0.0
    exclaims: int = 0
    elongated: bool = False   # "sooo", "!!!" style emphasis was present


_APOS = re.compile(r"['‘’ʼ]")
_RUN3 = re.compile(r"(.)\1{2,}")
_TOKEN = re.compile(r"[a-z0-9]+")


def _strip_symbols(s: str) -> tuple[str, list]:
    hints, out = [], []
    for ch in s:
        base = EMOJI_HINTS.get(ch)
        if base is not None:
            hints.append(base)
            out.append(" ")
        elif unicodedata.category(ch) in ("So", "Sk", "Cf", "Cs"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out), hints


def normalize(text: str) -> Normalized:
    """Stage 1. Fold a raw chat message into a canonical matchable form.

    INPUT: raw user text (any unicode).
    OUTPUT: a Normalized record. Emoji become thermal hints and are removed;
      apostrophes are deleted ("can't" -> "cant"); runs of 3+ identical chars
      collapse to 2 in .text and to 1 in .squeezed, so "lobbby" -> "lobby" and
      "sooo" -> "so"; chat shorthand is expanded token-wise.
    SIDE EFFECTS: none.
    ERROR STATES: none; a None-ish input is coerced to "".
    """
    raw = text or ""
    letters = [c for c in raw if c.isalpha()]
    caps = sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0
    body, hints = _strip_symbols(unicodedata.normalize("NFKC", raw))
    body = _APOS.sub("", body.lower())
    body = re.sub(r"[^\w\s+/-]", " ", body)          # keep + and / (zone joins, a/c)
    words = [CONTRACTIONS.get(w, w) for w in body.split()]
    flat = " ".join(words)
    elongated = bool(_RUN3.search(flat)) or raw.count("!") >= 2
    t2 = _RUN3.sub(r"\1\1", flat)
    t1 = _RUN3.sub(r"\1", flat)
    toks, offs = [], []
    for m in _TOKEN.finditer(t2):
        toks.append(m.group(0))
        offs.append(m.start())
    return Normalized(raw=raw, text=t2, squeezed=t1, padded=f" {t2} ", tokens=toks,
                      offsets=offs, emoji_hints=hints, caps_ratio=caps,
                      exclaims=raw.count("!"), elongated=elongated)


# ==========================================================================
# Stage 2 data: language detection
# ==========================================================================

# Hindi/Urdu transliteration seen in Indian office chat. One line per word.
HINGLISH_LEXICON: frozenset = frozenset("""
garmi garam thand thandi sardi ghutan ghut dum pasina chhut hawa lag lagi rahi
raha rahe hai hain ho gaya gaye gayi kar karo karna kam zyada jyada bahut thoda
yaar bhai arre acha theek thik abhi phir bhi mein me ka ki ke pe par se nahi
naa haan chip chipchip mausam baahar andar seedha seedhi muh mooh sunn dono
badha badhao tez chalu band pankha khidki kirpaya kripya
""".split())

# Tamil transliteration ("Tanglish"). Same one-line-per-word rule.
TAMIL_LEXICON: frozenset = frozenset("""
romba konjam semma jaasthi jaasti adhigam kuluru kulir kuliru soodu sudu
irukku iruku irukkum inga inge anga la le ah aa pannunga pannu panunga kammi
kaatru kaathu kaathadi vendam venum illa illai mudiyala mudiyathu neenga naan
enna sari nalla ponga
""".split())

INDIC_LEXICON: frozenset = HINGLISH_LEXICON | TAMIL_LEXICON

# Tokens too short/ambiguous to count as evidence of an Indic message on their
# own ("me", "la", "ah" are also English/noise). They still parse, they just do
# not vote on the language label.
_WEAK_INDIC: frozenset = frozenset({"me", "la", "le", "ah", "aa", "ka", "ki", "ke",
                                    "pe", "se", "ho", "hai", "naa", "no", "ta"})


def detect_language(norm: Normalized) -> str:
    """Stage 2. Label the message "en", "hinglish" or "mixed".

    INPUT: a Normalized record.
    OUTPUT: one of contracts.LANGUAGES. Ratio of Indic-lexicon tokens to content
      tokens: 0 -> "en", >= 0.55 -> "hinglish", otherwise "mixed". Short
      ambiguous particles ("me", "la") never trigger the label on their own.
    SIDE EFFECTS: none.
    ERROR STATES: none; an empty message is "en".
    """
    toks = [t for t in norm.tokens if not t.isdigit()]
    if not toks:
        return "en"
    strong = sum(1 for t in toks if t in INDIC_LEXICON and t not in _WEAK_INDIC)
    if strong == 0:
        return "en"
    indic = sum(1 for t in toks if t in INDIC_LEXICON)
    return "hinglish" if indic / len(toks) >= 0.55 else "mixed"


# ==========================================================================
# Stage 3 data: retraction / all-clear
# ==========================================================================

RETRACTIONS: tuple = ("fine now", "better now", "fixed now", "all good", "sorted now",
                      "resolved", "ok now", "okay now", "back to normal", "perfect now",
                      "sorted out", "no issues now", "comfortable now",
                      "theek ho gaya", "thik ho gaya", "ab theek", "ab thik",
                      "sari aayiduchu", "ok ah irukku")
# Contrast markers mean the discomfort is ongoing ("all good but cabin is hot").
ONGOING: tuple = ("but ", "still ", "except", "abhi bhi", "phir bhi", "however")

# The other half of the all-clear family, and the one the phrase list above
# cannot express: a NEGATED DISCOMFORT STATE. "no longer stuffy", "not cold any
# more", "stopped being humid" and "the heat is gone" all cancel a complaint
# without containing a single positive all-clear phrase, so the pattern has to be
# <negator> x <state>, composed against the issue lexicon rather than enumerated.
# Building the state half from INTENT_TABLE (see _negated_state_re) means every
# lexicon entry added later gets its retraction form for free.
#
# Only DISCOMFORT words may enter that vocabulary. Negating a comfort word is a
# COMPLAINT ("not comfortable now"), never an all-clear, which is why the state
# half is the issue lexicon and not a general adjective list. "inferred" rules
# are skipped for the same reason: "I don't need my jacket" is about a garment.
NEG_ENDED: tuple = ("no longer", "stopped being", "stopped feeling",
                    "dont feel", "doesnt feel", "not feeling")
# Bare negation needs a temporal tail: "not cold" alone can be a correction
# ("not cold, freezing"), while "not cold now" is unambiguously an all-clear.
NEG_PLAIN: tuple = ("not", "isnt", "arent", "aint", "wasnt")
NEG_TAIL: tuple = ("any more", "anymore", "now")

_RETRACTIONS = RETRACTIONS     # legacy private aliases (kept: cheap, avoids churn)
_ONGOING = ONGOING
_NEG_STATE_RE = None           # built on first use by _negated_state_re()


def _negated_state_re():
    """Compiled <negator> x <discomfort state> pattern, built once and cached.

    Deliberate forward reference: the state vocabulary IS the stage-5 intent
    lexicon (INTENT_TABLE, defined below), and reading it lazily at first call
    keeps one source of truth for "what a discomfort word is". A second hand-kept
    list here would silently drift the day someone adds a lexicon entry.

    INPUT: none. OUTPUT: a compiled re.Pattern matched against Normalized.padded.
    SIDE EFFECTS: populates the module-level cache. ERROR STATES: none.
    """
    global _NEG_STATE_RE
    if _NEG_STATE_RE is None:
        states: set = set()
        for rule in INTENT_TABLE:
            if rule.kind == "inferred":
                continue
            states.update(w.rstrip("$") for w in rule.words)
            states.update(rule.phrases)
        st = "(?:%s)\\w*" % "|".join(sorted((re.escape(s) for s in states),
                                            key=len, reverse=True))
        fill = r"(?:\s+(?:so|too|that|very|really|as|super|much|even|feeling))*"
        gap = r"(?:\s+[a-z0-9]+){0,3}?"          # "not hot in the lobby now"
        _NEG_STATE_RE = re.compile(
            rf"\b(?:{'|'.join(NEG_ENDED)}){fill}\s+{st}\b"
            rf"|\b(?:{'|'.join(NEG_PLAIN)}){fill}\s+{st}{gap}"
            rf"\s+(?:{'|'.join(NEG_TAIL)})\b"
            rf"|\b{st}\s+gone\b"
            rf"|\b{st}{gap}\s+(?:is|are|has)\s+gone\b")
    return _NEG_STATE_RE


def detect_retraction(text: str) -> bool:
    """Stage 3. True when the message cancels a complaint instead of filing one.

    INPUT: raw text (this is a public entry point called directly by app.py, so
      it re-normalizes internally rather than taking a Normalized).
    OUTPUT: bool. Either a positive all-clear phrase (RETRACTIONS) or a negated
      discomfort state (_negated_state_re) wins, unless a contrast marker says
      the discomfort continues ("no longer hot but still stuffy").
    SIDE EFFECTS: none.
    ERROR STATES: none.
    """
    t = normalize(text).padded
    if any(m in t for m in ONGOING):
        return False
    return any(p in t for p in RETRACTIONS) or bool(_negated_state_re().search(t))


# ==========================================================================
# Stage 4 data: zones
# ==========================================================================

_LETTER_ZONE = {"a": "zone_a", "b": "zone_b", "c": "zone_c", "d": "zone_d", "e": "zone_e"}

# Aliases that name a piece of furniture rather than a room. They only resolve a
# zone when nothing stronger is in the message, otherwise "a draft on my desk in
# the lobby" would file against the open office.
WEAK_ALIASES: frozenset = frozenset({"desk", "manager"})

_ZONE_PHRASES: list = []   # (phrase, zone_id) multi-word, exact match only
_ZONE_WORDS: list = []     # (word, zone_id) single word, fuzzy-eligible
for _z in ZONES:
    for _a in _z.aliases:
        if " " in _a:
            _ZONE_PHRASES.append((_a, _z.id))
            _ZONE_PHRASES.append((_a.replace(" ", ""), _z.id))   # "boardroom"
        else:
            _ZONE_WORDS.append((_a, _z.id))
_ZONE_PHRASES.sort(key=lambda p: -len(p[0]))    # longest phrase wins first


def _lev_le(a: str, b: str, k: int) -> bool:
    """Levenshtein(a, b) <= k, short-circuited on length. Plain DP; words are tiny."""
    la, lb = len(a), len(b)
    if abs(la - lb) > k:
        return False
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (a[i - 1] != b[j - 1]))
        if min(cur) > k:
            return False
        prev = cur
    return prev[lb] <= k


def fuzzy_tolerance(term: str) -> int:
    """Edit budget for a lexicon term: 0 for <=4 chars, 1 for 5-9, 2 for 10+.

    INPUT: a single lexicon word.
    OUTPUT: int edit budget. Short words get 0 because at 4 chars almost every
      other 4-letter word is one edit away ("cold"/"cord"/"bold"). Two edits are
      only affordable on long words: at 8 chars a budget of 2 already collides
      ("thermostat" -> "thermals", "steaming" -> "sweating").
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    n = len(term)
    return 0 if n <= 4 else (1 if n <= 9 else 2)


def fuzzy_hit(token: str, term: str) -> bool:
    """True when `token` is a plausible typo of lexicon word `term`.

    INPUT: a normalized input token and a single-word lexicon term.
    OUTPUT: bool. Terms are matched against token PREFIXES too, so the stem
      "freez" catches "friezing". Real English words (FUZZ_STOPWORDS) are never
      fuzzed, and terms of <= 4 chars require an exact prefix.
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    tol = fuzzy_tolerance(term)
    if tol == 0 or len(token) < 4 or token in FUZZ_STOPWORDS:
        return False
    lo, hi = max(1, len(term) - tol), min(len(token), len(term) + tol)
    return any(_lev_le(token[:k], term, tol) for k in range(lo, hi + 1))


def extract_zones(norm: Normalized) -> tuple[list, dict]:
    """Stage 4. All zones named in the message, in order of first mention.

    INPUT: a Normalized record.
    OUTPUT: (zone_ids, zone_confidence). zone_ids is deduped and ordered by
      position, so zone_ids[0] is the zone the message leads with. Confidence:
      0.95 phrase alias, 0.9 explicit "room b"/"zone d", 0.9 word alias, 0.72
      fuzzy alias, 0.6 bare leading letter, 0.5 furniture-only fallback.
      Ids outside VALID_ZONES can never be produced — the vocabulary is built
      from sim.twin.ZONES.
    SIDE EFFECTS: none.
    ERROR STATES: none; no zone mentioned returns ([], {}).
    """
    hits: dict = {}          # zone_id -> (position, confidence)

    def note(zid, pos, conf):
        old = hits.get(zid)
        if old is None or pos < old[0] or conf > old[1]:
            hits[zid] = (min(pos, old[0]) if old else pos, max(conf, old[1] if old else 0))

    t = norm.text
    for phrase, zid in _ZONE_PHRASES:
        p = t.find(phrase)
        if p >= 0:
            note(zid, p, 0.95)
    for word, zid in _ZONE_WORDS:
        if word in WEAK_ALIASES:
            continue
        for tok, off in zip(norm.tokens, norm.offsets):
            if tok == word or tok.startswith(word):
                note(zid, off, 0.9)
                break
            if fuzzy_hit(tok, word):
                note(zid, off, 0.72)
                break
    for m in re.finditer(r"\b(?:room|zone|cabin)\s*([a-e])\b", t):
        note(_LETTER_ZONE[m.group(1)], m.start(), 0.9)
    m = re.match(r"\s*([b-e])\b", t)             # "b is stuffy again" (never "a")
    if m:
        note(_LETTER_ZONE[m.group(1)], 0, 0.6)
    if not hits:                                  # furniture-only fallback
        for word, zid in _ZONE_WORDS:
            if word in WEAK_ALIASES:
                for tok, off in zip(norm.tokens, norm.offsets):
                    if tok == word:
                        note(zid, off, 0.5)
                        break
    ordered = sorted(hits.items(), key=lambda kv: kv[1][0])
    return [z for z, _ in ordered], {z: round(c, 3) for z, (_p, c) in ordered}


def _find_zone(t: str) -> str | None:
    """Legacy single-zone helper (kept for callers that predate zone_ids)."""
    ids, _ = extract_zones(normalize(t))
    return ids[0] if ids else None


# ==========================================================================
# Stage 5 data: intent
# ==========================================================================

@dataclass(frozen=True)
class IntentRule:
    """One issue and the surface forms that signal it.

    phrases: matched as substrings of the normalized text (multi-word ok).
    words:   matched per token as a PREFIX and fuzzy-eligible (typo tolerant).
             A trailing "$" makes a term exact-token-only — no prefix, no fuzz.
             That is for short idiom-prone words: "swamp" must not fire on
             "swamped with work", which prefix matching would happily do.
    kind:    "explicit" (says the thing), "metaphor" (sauna/icebox) or
             "inferred" (a garment implies cold). Feeds confidence scoring.
    """
    issue: str
    phrases: tuple = ()
    words: tuple = ()
    kind: str = "explicit"


# Ordered MOST SPECIFIC FIRST. "cold air blowing on me" is a draft complaint,
# not a temperature one, so drafty must be evaluated before too_cold.
INTENT_TABLE: tuple = (
    IntentRule("drafty",
               phrases=("hawa lag", "blowing directly", "air blowing", "vent blast",
                        "blowing on me", "blowing right", "wind tunnel", "draught"),
               words=("draft", "drafty")),
    IntentRule("stuffy",
               phrases=("cant breathe", "no air", "some air", "more air", "need air",
                        "dum ghut", "ghut rah", "saans", "no ventilation",
                        "poor ventilation", "air is dead", "kaatru illa"),
               # A smell IS the canonical ventilation complaint: bad air is
               # reported by nose long before anyone says "stuffy". Exact-only
               # ("$"), because at 5 chars "smell" is one edit from "small" and
               # "stink" is one edit from "sticky" — which is a HUMID word, so
               # fuzzing these would steal humidity complaints.
               words=("stuffy", "stale", "suffocat", "airless", "ghutan", "stagnant",
                      "musty", "smell$", "smells$", "smelly$", "smelling$",
                      "stink$", "stinks$", "stinking$", "stinky$", "odour", "odor",
                      "reek$", "reeks$")),
    IntentRule("humid",
               phrases=("chip-chip", "chipchip", "chip chip", "sticky air",
                        "steam room"),
               # Metaphors, symmetric with sauna/oven for heat and icebox for
               # cold — humidity had none, so every figurative damp room read as
               # "no comfort keyword matched".
               words=("humid", "sticky", "muggy", "clammy", "damp", "moisture",
                      "eeram", "swamp$", "swampy$", "rainforest", "tropical")),
    IntentRule("too_cold",
               phrases=("ac kam", "ac band", "ac reduce", "kam karo", "kammi pannunga",
                        "kammi pannu", "ice age", "ice box", "sunn ho", "too much ac",
                        "ac too high", "walk-in freezer", "see my breath",
                        "see your breath", "seeing my breath"),
               words=("freez", "cold", "chilly", "thand", "sardi", "icebox", "arctic",
                      "antarctica", "shiver", "brr", "kuluru", "kulir", "numb",
                      "kammi", "refrigerat")),
    IntentRule("too_cold",                       # sarcasm / indirect: what you wear
               phrases=("winter coat", "space heater", "hot water bottle",
                        "hot tea to survive"),
               words=("jacket", "sweater", "sweatshirt", "hoodie", "gloves", "mittens",
                      "scarf", "blanket", "shawl", "thermals", "muffler", "beanie",
                      "parka", "cardigan"),
               kind="inferred"),
    IntentRule("too_hot",
               phrases=("ac tez", "ac badha", "ac increase", "increase the ac",
                        "sun off", "feels like 3", "jaasti pannunga", "semma heat",
                        "turn up the ac", "ac full"),
               words=("hot", "warm", "boiling", "baking", "sauna", "oven", "furnace",
                      "garmi", "garam", "pasina", "sweat", "burning", "melting",
                      "heat", "roasting", "swelter", "stifling", "tandoor", "bhatti",
                      "soodu", "sudu", "scorching", "sizzling")),
)

# A draft is "moving air" + "aimed at me". Composing the two lexicons catches
# phrasings no phrase list would ("AC ki hawa direct desk pe aa rahi hai").
AIR_NOUNS: tuple = ("hawa", "air", "ac", "vent", "blower", "fan", "kaatru", "kaathu",
                    "breeze")
DIRECTION_MARKERS: tuple = ("seedha", "seedhi", "direct", "directly", "straight",
                            "right on", "on my face", "face pe", "muh pe", "mooh pe",
                            "on me", "full blast", "blowing", "blows", "blast",
                            "pe aa rahi", "pe aa raha", "par aa rahi", "desk pe",
                            "desk par", "at my face", "on my neck", "on my back")

# The mirror image of the draft rule: air AIMED at me is a draft, air ABSENT is
# stuffiness. Same composition, so one rule covers the negation of every language
# in the lexicon ("no fresh air", "hawa nahi aa rahi", "kaathu illa") instead of a
# phrase per language. Adjacency is required — an air noun anywhere plus a stray
# "no" would make "no idea what the AC is doing" a ventilation complaint.
AIR_ABSENT_RE = re.compile(
    r"\b(?:air|airflow|hawa|kaatru|kaathu|ventilation|breeze)"
    r"(?:\s+[a-z]+)?\s+(?:illa|illai|nahi|nahin)\b"
    r"|\b(?:no|zero)(?:\s+[a-z]+)?\s+(?:air|airflow|hawa|kaatru|kaathu|ventilation)\b")

# Cooling reported DEAD or ABSENT is a HEAT complaint. Somebody who says "the AC
# isn't running" is not requesting a setpoint, they are telling you the room is
# hot — and the same sentence arrives as Hinglish ("AC chal hi nahi raha") or as
# sarcasm ("is the AC even on?"). Composed subject x predicate like the draft
# rule, so the phrasings do not have to be enumerated.
#
# The predicate list is deliberately narrow around "on": it needs a copula
# ("isn't on") or "even on", never a bare "on". Otherwise "don't turn on the AC"
# — a request from somebody who is COLD — would file a heat complaint, and a
# wrong-direction action is the worst failure this parser can produce.
COOLING_NOUNS: tuple = ("ac", "cooling", "cooler", "blower", "fan", "compressor")
COOLING_DEAD: tuple = (
    r"\b(?:not|isnt|arent|aint|dont|doesnt|didnt|never)\s+(?:even\s+)?"
    r"(?:work|works|working|run|runs|running|cool|cools|cooling|blow|blowing)\b",
    r"\b(?:isnt|arent|aint|wasnt)\s+(?:even\s+)?on\b",
    r"\beven\s+on\b",
    r"\bstopped\s+(?:working|running|cooling|blowing)\b",
    r"\bchal\w*(?:\s+(?:hi|bhi|to|hai|raha|rahi|rahe))*\s+nahi\b",
    r"\bnahi\s+chal\w*",
    r"\b(?:band|bandh)\s+(?:hai|h|he|pada)\b",
    r"\bno\s+(?:cooling|ac)\b",
    r"\b(?:ac|cooling|compressor)\s+(?:is\s+)?(?:dead|kaput|gone)\b",
)
COOLING_DEAD_RE = re.compile("|".join(COOLING_DEAD))

# Thermal words locked inside a fixed idiom are not about the room, and office
# chat is full of them. Masking is POSITIONAL, not a veto: only the idiom's own
# span is hidden from intent matching, so "it's hot at my hot desk" still reads as
# a heat complaint from the first "hot".
THERMAL_IDIOMS: tuple = ("warm blooded", "cold blooded", "hot blooded",
                         "warm hearted", "cold hearted", "hot desk", "hot desking",
                         "hot take", "hot topic", "hot seat", "hot mess", "hot fix",
                         "hotfix", "hot reload", "hot swap", "hot key", "hotkey",
                         "hot lead", "hot potato", "cold call", "cold calling",
                         "cold email", "cold start", "cold shoulder", "cold feet",
                         "cold turkey", "warm welcome", "warm regards", "warm lead",
                         "cool story", "cold hard")
_IDIOM_RE = re.compile("|".join(sorted((p.replace(" ", r"[\s-]+")
                                        for p in THERMAL_IDIOMS),
                                       key=len, reverse=True)))

# Subject nouns that make a heat/cold word NOT a comfort complaint. "oven",
# "heater" and "thermostat" are deliberately absent: "feels like an oven" is the
# most common metaphor for a hot room, and a thermostat reading IS about the room.
APPLIANCE_NOUNS: frozenset = frozenset({
    "coffee", "kettle", "projector", "printer", "laptop", "computer", "monitor",
    "screen", "server", "router", "wifi", "microwave", "toaster", "dispenser",
    "fridge", "freezer", "lift", "elevator", "cpu", "charger", "cable", "plug",
    "food", "tea", "soup", "plate", "mug", "cup", "pizza",
})
APPLIANCE_PHRASES: frozenset = frozenset({
    "coffee machine", "coffee maker", "water cooler", "vending machine",
    "tea machine", "water dispenser", "hot plate",
})
# Tokens that are never the subject of a clause, so the subject scan walks past.
SUBJECT_SKIP: frozenset = frozenset("""
the a an this that these those my our your their its his her some any no every
really very quite so too just still again pretty super damn bloody is are was
were be been being feels feeling getting got gets seems looks running run in at
on of for from near by with inside indoors here there now today tonight yaar
bhai arre hey ok okay please and but or also we i im it they you he she who
someone everyone anyone can could would should do does did dont cant why what
how when where lot bit
""".split())

# Statements about the sky, not the room. An outdoor marker vetoes the complaint
# unless the message also anchors itself indoors.
OUTDOOR_MARKERS: frozenset = frozenset({"outside", "outdoors", "outdoor", "weather",
                                        "forecast", "mausam", "baahar", "bahar",
                                        "streets", "traffic", "monsoon", "heatwave"})
# "<temperature word> out" is the English weather construction ("chilly out",
# "boiling out there"). Compositional so it covers words no marker list has.
_OUT_ADJ = ("hot", "cold", "chilly", "warm", "cool", "freezing", "boiling", "humid",
            "muggy", "sticky", "sunny", "baking", "roasting")
OUTDOOR_RE = re.compile(r"\b(?:%s)\s+out\b" % "|".join(_OUT_ADJ))
INDOOR_MARKERS: tuple = ("in here", "in the office", "at my desk", "indoors", "inside",
                         "in this room", "andar")

# A request to change the AC with no direction we can resolve. Real intent, but
# unanswerable -> requires_clarification instead of a guessed setpoint move.
CONTROL_VERBS: tuple = ("increase", "decrease", "raise", "lower", "adjust", "change",
                        "turn up", "turn down", "badha", "kam karo", "tez", "kammi",
                        "pannunga", "karo do")

_SEV3 = ("dying", "unbearable", "cant work", "burning up", "freezing", "urgent",
         "bahut zyada", "romba jaasthi", "romba jaasti", "semma", "cant focus",
         "third time", "again and again", "ridiculous", "insane")
_SEV2 = ("really", "very", "so ", "too ", "bahut", "extremely", "zyada", "!!",
         "romba", "quite", "super", "way too", "seriously")
_SEV_DAMPEN = ("slightly", "kinda", "kind of", "a bit", "a little", "thoda", "konjam",
               "somewhat", "mildly")
AMBIGUITY_MARKERS: tuple = (" or ", "maybe", "not sure", "cant tell", "cant decide",
                            "idk", "no idea", "either", "or is it", "dunno")

# Legacy shape of the keyword table, rebuilt from INTENT_TABLE so the two can
# never drift. Kept because it documents the cascade at a glance.
_KEYWORDS = [(r.issue, list(r.phrases) + [w.rstrip("$") for w in r.words])
             for r in INTENT_TABLE]


@dataclass
class Intent:
    """Output of stage 5."""
    issue: str | None = None
    kind: str = "explicit"
    evidence: list = field(default_factory=list)     # list[(term, how, char_pos)]
    families: set = field(default_factory=set)       # every issue that matched
    veto: str = ""                                   # non-empty = not a complaint
    control_request: bool = False
    ambiguous: bool = False
    first_pos: int = 0


def _idiom_spans(text: str) -> list:
    """(start, end) of every fixed thermal idiom in `text`. See THERMAL_IDIOMS."""
    return [(m.start(), m.end()) for m in _IDIOM_RE.finditer(text)]


def _match_rule(norm: Normalized, rule: IntentRule, masked: list = ()) -> list:
    """(term, how, char_pos) for every surface form of `rule` present in `norm`.

    char_pos is where the evidence sits in the normalized text; the appliance
    subject check needs it, so a fuzzy hit reports its TOKEN's offset, never 0.
    `masked` is a list of (start, end) spans to ignore — idiom occurrences, so
    "hot desk" contributes no heat evidence while a second, unmasked "hot" still
    does. A term ending in "$" is exact-token-only (see IntentRule).
    """
    def hidden(pos: int) -> bool:
        return any(a <= pos < b for a, b in masked)

    hits = []
    for p in rule.phrases:
        i = norm.text.find(p)
        while i >= 0 and hidden(i):              # skip masked occurrences only
            i = norm.text.find(p, i + 1)
        if i >= 0:
            hits.append((p, "exact", i))
        elif p in norm.squeezed:
            hits.append((p, "exact", 0))         # only matched after squeezing
    for w in rule.words:
        exact_only = w.endswith("$")
        term = w[:-1] if exact_only else w
        for tok, off in zip(norm.tokens, norm.offsets):
            if hidden(off):
                continue
            if tok == term or (not exact_only and tok.startswith(term)):
                hits.append((term, "exact", off))
                break
        else:
            if exact_only:
                continue
            for tok, off in zip(norm.tokens, norm.offsets):
                if not hidden(off) and fuzzy_hit(tok, term):
                    hits.append((term, "fuzzy", off))
                    break
    return hits


def _clause_of(norm: Normalized, pos: int) -> tuple:
    """(tokens, offsets) of the clause containing char index `pos`."""
    bounds = [0]
    for m in re.finditer(r"[,;.!?]| but ", norm.text):
        bounds.append(m.end())
    bounds.append(len(norm.text) + 1)
    lo = max(b for b in bounds if b <= pos)
    hi = min(b for b in bounds if b > pos)
    return [(t, o) for t, o in zip(norm.tokens, norm.offsets) if lo <= o < hi]


def _subject_is_appliance(norm: Normalized, pos: int) -> bool:
    """Is the grammatical subject of the clause an appliance rather than a space?

    Walks the clause left-to-right, skipping determiners/copulas/adverbs, and
    takes the first real noun as the subject. "the microwave in the pantry is
    burning hot" -> subject "microwave" -> not a comfort complaint, even though a
    zone alias ("pantry") sits between the subject and the heat word.
    """
    clause = [(t, o) for t, o in _clause_of(norm, pos) if o <= pos]
    for i, (tok, _o) in enumerate(clause):
        if tok in SUBJECT_SKIP or tok.isdigit():
            continue
        two = f"{tok} {clause[i + 1][0]}" if i + 1 < len(clause) else ""
        return two in APPLIANCE_PHRASES or tok in APPLIANCE_NOUNS
    return False


def detect_intent(norm: Normalized) -> Intent:
    """Stage 5. Which comfort issue (if any) the message is about.

    INPUT: a Normalized record.
    OUTPUT: an Intent. The cascade runs most-specific-first (drafty, stuffy,
      humid, too_cold, too_hot) over INTENT_TABLE, plus four compositional rules
      (moving-air x aimed-at-me -> drafty; air x absent -> stuffy; cooling x
      reported-dead -> too_hot; emoji hint -> issue when nothing else matched)
      and three vetoes: outdoor scope, appliance subject, and a hot+cold
      collision which yields ambiguous=True instead of a coin flip. Thermal
      idioms ("hot desk") are masked out of the lexicon match before any of it.
    SIDE EFFECTS: none.
    ERROR STATES: none; a message with no signal returns Intent(issue=None).
    """
    out = Intent()
    per_issue: dict = {}
    masked = _idiom_spans(norm.text)
    for rule in INTENT_TABLE:
        hits = _match_rule(norm, rule, masked)
        if hits:
            cur = per_issue.setdefault(rule.issue, [rule.kind, []])
            cur[1].extend(hits)
            if rule.kind == "explicit":
                cur[0] = "explicit"
            out.families.add(rule.issue)

    # compositional draft rule
    if "drafty" not in per_issue:
        air = [a for a in AIR_NOUNS if a in norm.tokens]
        aim = [d for d in DIRECTION_MARKERS if d in norm.padded]
        if air and aim:
            pos = max(norm.text.find(air[0]), 0)
            per_issue["drafty"] = ["composite",
                                   [(f"{air[0]}+{aim[0]}", "composite", pos)]]
            out.families.add("drafty")

    # compositional "no air" rule: absence of airflow is stuffiness
    if "stuffy" not in per_issue:
        m = AIR_ABSENT_RE.search(norm.text)
        if m:
            per_issue["stuffy"] = ["composite",
                                   [(m.group(0).strip(), "composite", m.start())]]
            out.families.add("stuffy")

    # compositional "the cooling is dead" rule: absent cooling is a heat report
    if "too_hot" not in per_issue:
        cool = [(c, norm.offsets[norm.tokens.index(c)])
                for c in COOLING_NOUNS if c in norm.tokens]
        m = COOLING_DEAD_RE.search(norm.text)
        if cool and m:
            per_issue["too_hot"] = ["composite",
                                    [(f"{cool[0][0]}+{m.group(0).strip()}",
                                      "composite", cool[0][1])]]
            out.families.add("too_hot")

    # emoji-only fallback
    if not per_issue and norm.emoji_hints:
        m = {"hot": "too_hot", "cold": "too_cold", "humid": "humid", "stuffy": "stuffy"}
        for h in norm.emoji_hints:
            if h in m:
                per_issue[m[h]] = ["inferred", [(f"emoji:{h}", "inferred", 0)]]
                out.families.add(m[h])
                break

    out.control_request = any(v in norm.padded for v in CONTROL_VERBS)
    if not per_issue:
        # A change request we cannot aim ("konjam increase pannunga") is a real
        # comfort intent with an unresolvable direction: ask, do not guess.
        out.ambiguous = out.control_request
        return out

    for rule in INTENT_TABLE:                    # cascade order decides the winner
        if rule.issue in per_issue:
            out.issue = rule.issue
            out.kind, out.evidence = per_issue[rule.issue]
            break
    out.first_pos = min(e[2] for e in out.evidence) if out.evidence else 0

    # Veto 1: the message is about the weather, not the building.
    outdoor = any(w in OUTDOOR_MARKERS for w in norm.tokens) or \
        bool(OUTDOOR_RE.search(norm.text))
    if outdoor and not any(m in norm.padded for m in INDOOR_MARKERS):
        out.veto = "outdoor_scope"
        return out
    # Veto 2: the hot thing is an appliance, not the room.
    if _subject_is_appliance(norm, out.first_pos):
        out.veto = "appliance_subject"
        return out
    # Collision: both temperature directions asserted and nothing more specific.
    if {"too_hot", "too_cold"} <= out.families and out.issue in ("too_hot", "too_cold"):
        out.issue = "other"
        out.ambiguous = True
    return out


# ==========================================================================
# Stage 6: severity
# ==========================================================================

def detect_severity(norm: Normalized, intent: Intent) -> int:
    """Stage 6. 1 = mild, 2 = clear discomfort, 3 = urgent.

    Rule table (applied in order, result clamped to 1..3):
      start at 1
      +1  any _SEV2 intensifier ("really", "so", "bahut", "romba", "way too")
      +1  SHOUTING: >= 8 letters and more than half of them uppercase
      +1  emphasis: 2+ exclamation marks, or elongation ("sooo hot")
      +1  corroboration: 3+ distinct evidence terms for the same issue
      =3  any _SEV3 marker ("unbearable", "cant work", "urgent", "semma")
      =1  any dampener ("slightly", "thoda", "konjam") and no _SEV3 marker

    INPUT: Normalized + the Intent from stage 5. OUTPUT: int 1..3.
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    if any(w in norm.padded for w in _SEV3):
        return 3
    sev = 1
    if any(w in norm.padded for w in _SEV2):
        sev += 1
    if norm.caps_ratio > 0.5 and sum(c.isalpha() for c in norm.raw) >= 8:
        sev += 1
    if norm.elongated:
        sev += 1
    if len({e[2] for e in intent.evidence}) >= 3:
        sev += 1
    if any(w in norm.padded for w in _SEV_DAMPEN):
        sev = 1
    return max(1, min(3, sev))


# ==========================================================================
# Stage 7: context resolution
# ==========================================================================

def resolve_context(norm: Normalized, zones: list, intent: Intent) -> tuple[list, Intent]:
    """Stage 7. Reconcile the zone and intent stages against each other.

    INPUT: Normalized, zone_ids from stage 4, Intent from stage 5.
    OUTPUT: (zone_ids, intent), possibly adjusted:
      - a vetoed outdoor statement keeps no zone (it named none by definition,
        but a stray furniture alias is dropped rather than acted on);
      - a hedged message ("maybe", "or", "cant tell") is marked ambiguous so
        confidence scoring can push it below the clarification threshold.
    SIDE EFFECTS: mutates and returns the Intent it was given (cheap, local).
    ERROR STATES: none.
    """
    if intent.veto == "outdoor_scope":
        zones = []
    if intent.issue and any(m in norm.padded for m in AMBIGUITY_MARKERS) \
            and len(intent.evidence) < 2:
        intent.ambiguous = True
    return zones, intent


# ==========================================================================
# Stage 8: confidence
# ==========================================================================

_KIND_BASE = {"exact": 0.72, "composite": 0.66, "fuzzy": 0.60, "inferred": 0.55}


def score_confidence(norm: Normalized, zones: list, zconf: dict,
                     intent: Intent, severity: int) -> float:
    """Stage 8. How much the controller should trust this parse (0..1).

    Signal strength (how the intent matched) sets the base; zone certainty,
    corroborating evidence and explicit intensity add; hedging, an unresolvable
    zone and a hot/cold collision subtract.

      base            0.72 exact | 0.66 composite | 0.60 fuzzy | 0.55 inferred
      + zone          + 0.18 * best zone confidence      (or -0.10 if no zone)
      + corroboration + 0.05 per extra INDEPENDENT signal, capped at +0.10
                        (independent = a different word in the message, so two
                        spellings of one lexicon entry are not two votes)
      + intensity     + 0.05 when severity >= 2
      - hedging       - 0.15 when ambiguity markers are present
      - collision     - 0.20 when hot and cold were both asserted
      clamped to 0.05 .. 0.98

    INPUT: the outputs of stages 1-6. OUTPUT: float. SIDE EFFECTS: none.
    ERROR STATES: none; an Intent with no issue scores the "no signal" floor.
    """
    if intent.issue is None:
        return 0.35 if intent.ambiguous else 0.6
    c = _KIND_BASE.get(intent.evidence[0][1] if intent.evidence else "exact",
                       _KIND_BASE["exact"])
    if intent.kind == "inferred":
        c = min(c, _KIND_BASE["inferred"])
    c += 0.18 * max(zconf.values()) if zones else -0.10
    c += min(0.10, 0.05 * (len({e[2] for e in intent.evidence}) - 1))
    if severity >= 2:
        c += 0.05
    if any(m in norm.padded for m in AMBIGUITY_MARKERS):
        c -= 0.15
    if {"too_hot", "too_cold"} <= intent.families:
        c -= 0.20
    return round(max(0.05, min(0.98, c)), 3)


# ==========================================================================
# The rules parser (the whole pipeline, offline and deterministic)
# ==========================================================================

CLARIFY_BELOW = 0.45


def rules_parse(text: str) -> ParsedComplaint:
    """Run the full offline pipeline over one message.

    INPUT: raw text.
    OUTPUT: a cleaned ParsedComplaint. Deterministic: no randomness, no clock,
      no network — the same string always produces the same record.
    SIDE EFFECTS: none.
    ERROR STATES: none; any message is parseable (worst case it is simply not a
      comfort complaint).
    """
    norm = normalize(text)
    lang = detect_language(norm)
    base = {"language": lang, "normalized_text": norm.text}
    zones, zconf = extract_zones(norm)

    if detect_retraction(text):
        return ParsedComplaint(is_comfort_complaint=False, zone_ids=zones,
                               zone_confidence=zconf, issue="other", severity=1,
                               confidence=0.7,
                               reasoning="Rules: retraction/all-clear detected.",
                               **base).clean()

    intent = detect_intent(norm)
    zones, intent = resolve_context(norm, zones, intent)

    if intent.veto:
        why = {"outdoor_scope": "Rules: outdoor/weather statement, not a room complaint.",
               "appliance_subject": "Rules: the hot/cold subject is an appliance, not the space."}
        return ParsedComplaint(is_comfort_complaint=False, zone_ids=zones,
                               zone_confidence=zconf, issue="other", severity=1,
                               confidence=0.8, reasoning=why[intent.veto], **base).clean()

    if intent.issue is None:
        conf = score_confidence(norm, zones, zconf, intent, 1)
        return ParsedComplaint(
            is_comfort_complaint=False, zone_ids=zones, zone_confidence=zconf,
            issue="other", severity=1, confidence=conf,
            requires_clarification=intent.ambiguous,
            reasoning=("Rules: comfort change requested but no direction given."
                       if intent.ambiguous else "Rules: no comfort keyword matched."),
            **base).clean()

    sev = detect_severity(norm, intent)
    conf = score_confidence(norm, zones, zconf, intent, sev)
    clarify = conf < CLARIFY_BELOW or not zones or intent.ambiguous
    terms = ", ".join(sorted({e[0] for e in intent.evidence}))[:80]
    return ParsedComplaint(
        is_comfort_complaint=True, zone_ids=zones, zone_confidence=zconf,
        issue=intent.issue, severity=sev, confidence=conf,
        requires_clarification=clarify,
        reasoning=f"Rules[{intent.kind}]: matched {terms}.", **base).clean()


# ==========================================================================
# LLM providers (unchanged contract: any failure degrades to the rules path)
# ==========================================================================

def _extract_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in LLM output")
    return json.loads(m.group(0))


def _finish_llm(data: dict, text: str) -> ParsedComplaint:
    """Fill the fields an LLM has no way to know, then clean()."""
    out = ParsedComplaint(**data)
    norm = normalize(text)
    out.normalized_text = out.normalized_text or norm.text
    if not out.language or out.language == "en":
        out.language = detect_language(norm)
    if out.is_comfort_complaint and not out.zone_ids:
        out.requires_clarification = True
    return out.clean()


def _call_anthropic(text: str) -> ParsedComplaint:
    import httpx

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"},
        json={"model": os.environ.get("LLM_MODEL", "claude-haiku-4-5"),
              "max_tokens": 400,
              "system": build_system_prompt(ZONES),
              "messages": [{"role": "user", "content": text}]},
        timeout=15,
    )
    r.raise_for_status()
    return _finish_llm(_extract_json(r.json()["content"][0]["text"]), text)


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
    return _finish_llm(_extract_json(r.json()["choices"][0]["message"]["content"]), text)


def parse(text: str, force_rules: bool = False):
    """Returns (ParsedComplaint, source, latency_ms).

    INPUT: raw text; force_rules=True skips every network call.
    OUTPUT: (ParsedComplaint, "llm"|"rules", int ms).
    SIDE EFFECTS: one outbound HTTPS request when a provider key is set.
    ERROR STATES: none propagate — any provider/parse/validation error falls
      back to the offline rules pipeline, which cannot fail.
    """
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

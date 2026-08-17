"""Occupant privacy, data retention and AI disclosure — the trust layer.

Why this module exists: FeelsLike asks people to tell a building how their body
feels, in their own words, from their own account. That is personal data by any
reading of the DPDP Act 2023 or the GDPR, and a comfort system that quietly
hoards it will be switched off by the first works council that reads the logs.
So the four occupant-facing promises are implemented here as functions, not
written on a slide:

  1. "We don't keep your contact details."  -> scrub_pii() removes emails, phone
     numbers, employee ids and @handles from the text BEFORE it is stored.
  2. "Your name isn't attached to the control loop." -> anonymize_author()
     replaces the handle with a per-process salted pseudonym.
  3. "It's deleted after a day." -> RetentionPolicy actually drops the records.
  4. "You can see it and delete it." -> export_records() / redact_record().

Plus AIDisclosure, which answers the one question an occupant is entitled to ask
about any text box: did my sentence leave this building? Parsing runs locally on
the rules engine by default; when an LLM key is set the text is posted to a
third-party API. That MUST be surfaced, never inferred by the user from a
latency badge. is_external() fails CLOSED — an unrecognised source is reported
as external, because the failure mode of over-disclosing is embarrassment and
the failure mode of under-disclosing is a broken promise.

Scope limits, stated honestly:
- Redaction is regex-based. It catches the structured identifiers people
  actually paste into chat. It is not a named-entity recogniser and will not
  remove "ask Priya on the third floor".
- Pseudonyms are unlinkable only to someone without the salt. Anyone with the
  running process's salt and a guess at a handle can confirm the guess. This is
  pseudonymisation, not anonymisation, and is labelled as such.
- Nothing here encrypts anything. Storage is an in-memory list in backend.app.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

from backend.contracts import to_dict
from sim.twin import ZONES

# --------------------------------------------------------------------------
# Defaults — the app enables anonymous mode + a retention window in one line:
#     cfg = replace(privacy.DEFAULTS, anonymous_mode=True, retention_hours=6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PrivacyConfig:
    """Deployment-level privacy switches. Frozen: change by dataclasses.replace.

    anonymous_mode  - replace every author handle with a salted pseudonym.
    scrub           - run scrub_pii over complaint text before it is stored.
    retention_hours - sim-hours a record survives; 0 disables retention.
    disclose_ai     - attach an AIDisclosure sentence to every stored record.
    salt_seed       - int for a reproducible pseudonym salt (tests/demos),
                      None for a fresh cryptographic salt each process.
    """
    anonymous_mode: bool = False
    scrub: bool = True
    retention_hours: float = 24.0
    disclose_ai: bool = True
    salt_seed: int | None = None


DEFAULTS = PrivacyConfig()
ANONYMOUS_MODE = DEFAULTS.anonymous_mode
SCRUB_PII = DEFAULTS.scrub
RETENTION_HOURS = DEFAULTS.retention_hours
PSEUDONYM_PREFIX = "occupant"

# Authors that are software, not people. Pseudonymising them destroys signal
# (the dashboard filters on "comfort-memory") and protects nobody.
RESERVED_AUTHORS = frozenset({"comfort-memory", "system", "anonymous", "feelslike", ""})

# Where parse() ran. Mirrors backend.parser's own vocabulary; see AIDisclosure.
LOCAL_SOURCES = frozenset({"rules", "memory", "local"})
EXTERNAL_SOURCES = frozenset({"llm"})


# --------------------------------------------------------------------------
# 1. PII scrubbing
# --------------------------------------------------------------------------

# Each pattern is tried in this order and matches never overlap (one finditer
# pass over an alternation), so an earlier row wins a contested span. The
# false-positive risk of every row is stated because a scrubber that eats real
# words is worse than no scrubber: it silently corrupts the complaint the
# controller has to act on.
_PII_PATTERNS: list = [
    # RFC-ish email. Deliberately not RFC 5322-complete (that regex is famous
    # and unreadable); this covers everything a human types in chat.
    # FP risk: LOW. Requires a dotted TLD after an @, which normal prose lacks.
    ("email", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
              r"(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
    # Indian mobile: optional +91/0091/91 country code, then 10 digits starting
    # 6-9 (TRAI's mobile range), optionally split 5+5 as people write them.
    # FP risk: MEDIUM. A bare 10-digit number starting 6-9 is indistinguishable
    # from a phone number; an order id like "9876543210" would be redacted.
    # Mitigated by the digit-boundary guards, so 12-digit ids do not match.
    ("phone_in", r"(?<![\w+])(?:(?:\+|00)?91[\s\-.]?)?[6-9]\d{4}[\s\-.]?\d{5}(?!\d)"),
    # Generic international/E.164: a leading + then 8-15 digits with separators.
    # FP risk: LOW. The mandatory leading + keeps it away from temperatures
    # ("+2 degC" has too few digits) and from setpoint deltas.
    ("phone_intl", r"(?<!\w)\+\d{1,3}[\s\-.]?(?:\d[\s\-.]?){6,13}\d(?!\d)"),
    # Employee/badge id: either a keyword-prefixed number (EMP1234, staff-889)
    # or an uppercase department-style code (HR-40218).
    # FP risk: MEDIUM for the second branch — any 2-4 uppercase letters,
    # a hyphen and 3+ digits matches, so a hypothetical "AHU-1204" equipment tag
    # would be redacted. Accepted: an over-redacted asset tag is recoverable
    # from the zone, a leaked staff id is not. Requires the hyphen precisely so
    # that unhyphenated tokens (a temperature, a zone alias) cannot match.
    ("employee_id", r"(?<!\w)(?:(?i:emp|empid|eid|badge|staff|payroll)[\s:#\-]?\d{3,8}"
                    r"|[A-Z]{2,4}-\d{3,8})(?!\w)"),
    # Chat handle. Runs after email so the local part of an address is not
    # re-matched, and the lookbehind blocks the tail of any @-containing token.
    # FP risk: MEDIUM — "@lobby" is a zone, not a person. Handled by the zone
    # shield below, which is the reason that shield exists.
    ("handle", r"(?<![\w.@])@[A-Za-z][A-Za-z0-9._\-]{1,29}(?<![.\-])"),
]

_PII_RE = re.compile("|".join(f"(?P<{k}>{v})" for k, v in _PII_PATTERNS))

_PLACEHOLDER = {
    "email": "[email]",
    "phone_in": "[phone]",
    "phone_intl": "[phone]",
    "employee_id": "[employee-id]",
    "handle": "[handle]",
}

# --- the shield: spans that may never be redacted --------------------------
# A temperature, a setpoint or a zone name is control-relevant content, not
# identity. Losing "26.5C" or "Lobby D" breaks the parser downstream.
# FP risk of the shield itself: it can protect a genuine identifier that
# happens to sit inside a zone alias ("@cafe" is shielded). Deliberate: the
# system's own vocabulary outranks the scrubber.
_TEMP_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:°|deg(?:rees?)?\.?)?\s*[cCfF](?![A-Za-z])"
    r"|\d+(?:\.\d+)?\s*°"
    r"|\d+(?:\.\d+)?\s*deg(?:rees?)?\b",
)

_ZONE_TERMS = sorted(
    {t.lower() for z in ZONES for t in ([z.id, z.name, z.id.replace("_", " ")] + list(z.aliases))},
    key=len, reverse=True,
)
# \b fails next to "_" and next to a leading "@", so the alias is wrapped in
# explicit non-word-ish guards instead.
_ZONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in _ZONE_TERMS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def protected_spans(text: str) -> list:
    """Character ranges scrub_pii must leave untouched: temperatures and zones.

    INPUT: raw text.
    OUTPUT: list of (start, end) half-open index pairs, unsorted, possibly
      overlapping. Empty list for text with no temperature or zone reference.
    SIDE EFFECTS: none.
    ERROR STATES: none; TypeError if text is not a str (re raises it).
    """
    spans = [m.span() for m in _TEMP_RE.finditer(text)]
    spans += [m.span() for m in _ZONE_RE.finditer(text)]
    return spans


def scrub_pii(text: str) -> tuple:
    """Redact contact details from occupant text, keeping the comfort content.

    Detects, in priority order: email addresses, Indian mobile numbers,
    international phone numbers, employee/badge ids, and @handles. Each becomes
    a bracketed placeholder ("[email]", "[phone]", "[employee-id]", "[handle]")
    so a human reading the feed can see that something was removed rather than
    reading a mangled sentence. Zone names and temperatures are shielded and
    are never redacted, even when they match a pattern (a Slack "@cafeteria"
    stays "@cafeteria" because the cafeteria is a room, not a person).

    INPUT: text (str). None/non-str raises TypeError from re.
    OUTPUT: (clean_text, found) where found is the list of KINDS redacted, in
      order of appearance, one entry per redaction: e.g. ["email", "handle"].
      It never contains the redacted values themselves — a log of the PII you
      just removed is still a log of PII.
    SIDE EFFECTS: none; the input string is not mutated (strings are immutable)
      and nothing is written anywhere.
    ERROR STATES: none for any str input. Text with no matches returns
      (text, []) with the identical string object semantics.
    """
    shield = protected_spans(text)
    out, found, cursor = [], [], 0
    for m in _PII_RE.finditer(text):
        s, e = m.span()
        if any(s < pe and ps < e for ps, pe in shield):
            continue                      # overlaps a zone name or a temperature
        kind = m.lastgroup
        out.append(text[cursor:s])
        out.append(_PLACEHOLDER.get(kind, "[redacted]"))
        found.append(kind)
        cursor = e
    if not found:
        return text, []
    out.append(text[cursor:])
    return "".join(out), found


# --------------------------------------------------------------------------
# 2. Pseudonymous authors
# --------------------------------------------------------------------------

_salt_lock = threading.Lock()
_salt: str | None = None


def session_salt(seed: int | None = None) -> str:
    """The process-wide pseudonym salt, generated once on first use.

    A salt that lived longer than the process would make pseudonyms linkable
    across restarts, which is exactly the tracking this module exists to
    prevent. A salt regenerated per call would make them useless (the same
    person would get a new name every message), so it is created once and
    cached.

    INPUT: seed — int for a deterministic salt (tests, reproducible demos), or
      None for 32 hex chars from secrets.token_hex. Ignored if a salt already
      exists; use reset_salt() to force a new one.
    OUTPUT: hex str.
    SIDE EFFECTS: caches the salt in a module global on first call. Thread-safe.
    ERROR STATES: none.
    """
    global _salt
    with _salt_lock:
        if _salt is None:
            _salt = (hashlib.blake2s(str(seed).encode(), digest_size=16).hexdigest()
                     if seed is not None else secrets.token_hex(16))
        return _salt


def reset_salt(seed: int | None = None) -> str:
    """Force a new session salt. Every previously issued pseudonym is orphaned.

    INPUT: seed as in session_salt().
    OUTPUT: the new hex salt.
    SIDE EFFECTS: overwrites the module global; existing pseudonyms in stored
      records can no longer be re-derived (which is a valid way to sever the
      link deliberately).
    ERROR STATES: none.
    """
    global _salt
    with _salt_lock:
        _salt = None
    return session_salt(seed)


def anonymize_author(author: str, salt: str | None = None, length: int = 4) -> str:
    """Stable per-session pseudonym for one occupant: "occupant-7f3a".

    Keyed BLAKE2s over the normalised handle. Same handle + same salt -> same
    pseudonym for the life of the process, so a conversation still reads as one
    person; different salt -> unrelated pseudonym, so nothing links across
    restarts or across deployments.

    Software authors in RESERVED_AUTHORS ("comfort-memory", "system",
    "anonymous", "") pass through unchanged — they are not people.

    COLLISION MATH, stated because it matters: length=4 hex is a 65,536-value
    space, so by the birthday bound a 100-occupant building has a ~7% chance
    that two people share a pseudonym, and 300 occupants ~50%. That is fine for
    a demo feed and NOT fine for anything that pays people; pass length=8 for a
    4-billion-value space if pseudonyms are ever used as a key.

    INPUT: author (str; leading "@", case and surrounding space are normalised
      away so "@Priya " and "priya" are the same person), optional salt
      (defaults to session_salt()), length in hex chars (1..32).
    OUTPUT: "occupant-<hex>" str, or the original string for a reserved author.
    SIDE EFFECTS: may create the process salt on first call.
    ERROR STATES: TypeError if author is not a str; ValueError if length < 1.
    """
    if not isinstance(author, str):
        raise TypeError(f"author must be str, got {type(author).__name__}")
    if length < 1:
        raise ValueError("length must be >= 1")
    norm = " ".join(author.strip().lstrip("@").split()).lower()
    if norm in RESERVED_AUTHORS:
        return author
    key = (salt if salt is not None else session_salt()).encode()[:32]
    digest = hashlib.blake2s(norm.encode("utf-8"), key=key, digest_size=16).hexdigest()
    return f"{PSEUDONYM_PREFIX}-{digest[:length]}"


# --------------------------------------------------------------------------
# 3. Record identity + retention
# --------------------------------------------------------------------------

_TIME_KEYS = ("t", "created_t", "sim_t", "timestamp")


def record_time(record: dict) -> float | None:
    """Sim-seconds timestamp of a feed record, or None if it carries no clock.

    INPUT: a feed-entry dict. The first of t / created_t / sim_t / timestamp
      that holds a real number wins; bools are rejected (bool is an int).
    OUTPUT: float sim-seconds, or None.
    SIDE EFFECTS: none.
    ERROR STATES: none; a non-dict returns None rather than raising, because
      this runs over user-shaped data on the delete path.
    """
    if not isinstance(record, dict):
        return None
    for k in _TIME_KEYS:
        v = record.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def record_id(record: dict) -> str:
    """Stable handle for one feed record, for export and single-record deletion.

    Uses record["id"] when the producer set one; otherwise derives
    "rec-<8 hex>" from the record's own content (author, text, sim_clock,
    action) so today's feed — which carries no ids — is still addressable
    without touching backend/app.py.

    CAVEAT: the content hash is UNSALTED, so it is stable across processes and
    exports (which is the point) but it also means someone holding a guess at a
    record's content can confirm it by recomputing the id. Ids are handles, not
    secrets.

    INPUT: a feed-entry dict.
    OUTPUT: str id.
    SIDE EFFECTS: none.
    ERROR STATES: none; a non-dict hashes its repr.
    """
    if isinstance(record, dict):
        rid = record.get("id")
        if isinstance(rid, str) and rid:
            return rid
        material = "|".join(str(record.get(k, "")) for k in
                            ("author", "text", "sim_clock", "action", "source"))
    else:
        material = repr(record)
    return "rec-" + hashlib.blake2s(material.encode("utf-8"), digest_size=4).hexdigest()


class RetentionPolicy:
    """Time-boxed storage: records older than `hours` of SIM time are dropped.

    The clock is sim-seconds (twin.t), the same clock the constraint store
    decays on, because that is the only clock the demo advances — at 960x a
    24 h window elapses in 90 real seconds, which is what makes retention
    demonstrable on stage rather than theoretical.

    UNDATED RECORDS ARE KEPT, not dropped. Today's feed entries carry only a
    display string ("Wed 14:35"), no numeric stamp; treating "no timestamp" as
    "infinitely old" would silently delete the newest messages in the feed.
    audit() reports how many records are in that state so the gap is visible
    instead of hidden.
    """

    def __init__(self, hours: float = RETENTION_HOURS):
        """INPUT: hours (float sim-hours; <= 0 disables expiry entirely).
        OUTPUT: policy object.  SIDE EFFECTS: none.
        ERROR STATES: TypeError if hours is not numeric."""
        self.hours = float(hours)
        self.seconds = self.hours * 3600.0

    def is_expired(self, record: dict, now_t: float) -> bool:
        """True when this record is older than the window.

        INPUT: record dict, now_t sim-seconds.
        OUTPUT: bool. Undated records and a disabled window return False.
        SIDE EFFECTS: none.  ERROR STATES: none.
        """
        if self.seconds <= 0:
            return False
        t = record_time(record)
        return t is not None and (now_t - t) > self.seconds

    def expired(self, records: list, now_t: float) -> list:
        """Ids of the records that must be deleted now.

        INPUT: list of feed dicts, now_t sim-seconds.
        OUTPUT: list[str] of record ids, in feed order.
        SIDE EFFECTS: none — this is the dry run; apply() does the deleting.
        ERROR STATES: none.
        """
        return [record_id(r) for r in records if self.is_expired(r, now_t)]

    def apply(self, records: list, now_t: float) -> list:
        """The records that survive the window.

        INPUT: list of feed dicts, now_t sim-seconds.
        OUTPUT: a NEW list holding the same record objects that are still in
          window (order preserved). The input list is not modified — the caller
          decides whether to rebind (feed = policy.apply(feed, t)) or to mutate.
        SIDE EFFECTS: none.
        ERROR STATES: none.
        """
        return [r for r in records if not self.is_expired(r, now_t)]

    def audit(self, records: list, now_t: float) -> dict:
        """Counts for the privacy panel: what would be kept, dropped, undated.

        INPUT: list of feed dicts, now_t sim-seconds.
        OUTPUT: dict(window_hours, total, kept, dropped, undated, oldest_age_min).
          oldest_age_min is None when nothing carries a timestamp.
        SIDE EFFECTS: none.  ERROR STATES: none.
        """
        times = [t for t in (record_time(r) for r in records) if t is not None]
        dropped = sum(1 for r in records if self.is_expired(r, now_t))
        return {
            "window_hours": self.hours,
            "total": len(records),
            "kept": len(records) - dropped,
            "dropped": dropped,
            "undated": len(records) - len(times),
            "oldest_age_min": round((now_t - min(times)) / 60.0, 1) if times else None,
        }


# --------------------------------------------------------------------------
# 4. Subject access: export and delete
# --------------------------------------------------------------------------

def export_records(feed: list, store, author: str | None = None,
                   scrub: bool = False, now_t: float | None = None) -> dict:
    """"Download my data": everything the system holds, as one JSON-safe dict.

    Covers both halves of an occupant's footprint — the raw messages in the feed
    AND the derived constraints in the store, because "we deleted your message
    but kept the setpoint it caused" is not deletion.

    INPUT: feed (list of entry dicts, newest first as backend.app keeps it),
      store (a ConstraintStore, or None to skip the constraint half), author
      (None = full export; a handle = only that person's records, matched
      against both the raw handle and its pseudonym so an export works in
      anonymous mode), scrub (True re-runs scrub_pii over stored text on the way
      out), now_t (sim-seconds for the age fields; defaults to the newest
      constraint's created_t, else 0.0).
    OUTPUT: dict with keys schema, exported_at_t, subject, counts, records,
      constraints, notes. Every value passes json.dumps with no encoder
      (contracts.to_dict does the flattening).
    SIDE EFFECTS: none. Neither the feed nor the store is modified; records are
      copied before any scrubbing.
    ERROR STATES: none. A store missing .items contributes an empty constraint
      list rather than raising, so an export never fails on the user.
    """
    items = list(getattr(store, "items", []) or [])
    if now_t is None:
        now_t = max((getattr(c, "created_t", 0.0) for c in items), default=0.0)

    pseudo = anonymize_author(author) if author else None

    def mine(a: str) -> bool:
        return author is None or a == author or (pseudo is not None and a == pseudo)

    records = []
    for r in feed:
        if not mine(str(r.get("author", ""))):
            continue
        out = dict(r)
        out["id"] = record_id(r)
        if scrub:
            out["text"], out["redacted"] = scrub_pii(str(out.get("text", "")))
        out["disclosure"] = AIDisclosure.describe(str(r.get("source", "")))
        out["external_ai"] = AIDisclosure.is_external(str(r.get("source", "")))
        records.append(to_dict(out))

    constraints = []
    for c in items:
        if not mine(str(getattr(c, "author", ""))):
            continue
        constraints.append(to_dict({
            "id": getattr(c, "id", None),
            "zone": getattr(c, "zone", ""),
            "issue": getattr(c, "issue", ""),
            "severity": getattr(c, "severity", 0),
            "confidence": getattr(c, "confidence", 0.0),
            "created_t": getattr(c, "created_t", 0.0),
            "age_min": round((now_t - getattr(c, "created_t", 0.0)) / 60.0, 1),
            "raw_offset": getattr(c, "raw_offset", 0.0),
            "vent_delta": getattr(c, "vent_delta", 0),
            "text": getattr(c, "text", ""),
            "author": getattr(c, "author", ""),
            "still_influencing_control": bool(getattr(c, "decay", lambda _t: 0.0)(now_t) > 0.02),
        }))

    return {
        "schema": "feelslike.export.v1",
        "exported_at_t": now_t,
        "subject": author or "*all*",
        "counts": {"records": len(records), "constraints": len(constraints)},
        "records": records,
        "constraints": constraints,
        "notes": [
            "Times are simulation seconds since Monday 00:00, not wall clock.",
            "Constraints are derived from the records; deleting a record with "
            "redact_record(feed, id, store) expires its constraint too.",
            "Nothing is stored outside this process — no database, no disk.",
        ],
    }


def redact_record(feed: list, entry_id: str, store=None, now_t: float | None = None) -> bool:
    """Delete one occupant record from the feed, in place. The forget button.

    INPUT: feed (list of entry dicts, mutated in place), entry_id (as returned
      by record_id / export_records), store (optional ConstraintStore — when
      given, constraints whose text and author match the deleted record are
      expired so the deletion reaches the control layer too), now_t
      (sim-seconds used to expire those constraints; defaults to the store's
      newest created_t so the expiry is never in the future).
    OUTPUT: True if a record was removed, False if the id was not found.
    SIDE EFFECTS: mutates `feed` (del by index) and, when store is passed,
      backdates matching Constraint.created_t past EXPIRY_S. Constraint objects
      stay in store.items — the comfort-memory miner reads that history — but
      their decay() is zero, so they no longer influence any setpoint.
    ERROR STATES: none. An unknown id, an empty feed or a store without .items
      all return False / do nothing rather than raising, because a delete
      request must never 500 on the person asking to be forgotten.
    """
    idx = next((i for i, r in enumerate(feed) if record_id(r) == entry_id), None)
    if idx is None:
        return False
    gone = feed.pop(idx)
    items = list(getattr(store, "items", []) or []) if store is not None else []
    if items:
        if now_t is None:
            now_t = max((getattr(c, "created_t", 0.0) for c in items), default=0.0)
        expiry = 2 * 3600.0
        for c in items:
            if (getattr(c, "text", None) == gone.get("text")
                    and getattr(c, "author", None) == gone.get("author")):
                c.created_t = now_t - expiry - 1.0
                c.text = "[redacted at occupant request]"
    return True


# --------------------------------------------------------------------------
# 5. AI disclosure
# --------------------------------------------------------------------------

class AIDisclosure:
    """Plain-language answer to "did my sentence leave the building?".

    The parser has two paths (backend.parser.parse): the offline rules pipeline,
    which runs in this process, and an LLM call to a third-party HTTPS endpoint.
    Which one ran is already in every feed entry as `source`; this class turns
    that token into a sentence a non-technical occupant can act on, and names
    the actual provider by reading the same environment variables parse() reads,
    in the same precedence order (Anthropic first, then any OpenAI-compatible
    base URL). API KEY VALUES ARE NEVER READ INTO THE OUTPUT — only the host.
    """

    LOCAL_TEXT = ("Parsed locally on this machine by the offline rules engine "
                  "— no data left the building.")

    @staticmethod
    def provider() -> str:
        """Human-readable name of the LLM endpoint parse() would use right now.

        INPUT: none (reads ANTHROPIC_API_KEY, OPENAI_API_KEY, LLM_BASE_URL,
          LLM_MODEL from the environment).
        OUTPUT: e.g. "Anthropic (api.anthropic.com, claude-haiku-4-5)" or
          "an external LLM provider" when no key is configured.
        SIDE EFFECTS: none; nothing is sent anywhere and no key value is
          returned, only the presence of a key and the endpoint host.
        ERROR STATES: none; a malformed LLM_BASE_URL degrades to its raw string.
        """
        if os.environ.get("ANTHROPIC_API_KEY"):
            model = os.environ.get("LLM_MODEL", "claude-haiku-4-5")
            return f"Anthropic (api.anthropic.com, {model})"
        if os.environ.get("OPENAI_API_KEY"):
            base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
            host = urlparse(base).netloc or base
            model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
            return f"{host} ({model})"
        return "an external LLM provider"

    @classmethod
    def is_external(cls, source: str) -> bool:
        """Did the text leave this machine? Fails CLOSED on anything unknown.

        INPUT: source token from a feed entry ("rules", "llm", "memory", ...).
        OUTPUT: False only for a recognised local source; True otherwise,
          including for "" and for tokens this module has never seen, because
          under-disclosing external AI use is the one failure that breaks the
          promise this module makes.
        SIDE EFFECTS: none.  ERROR STATES: none; non-str input is stringified.
        """
        return str(source).strip().lower() not in LOCAL_SOURCES

    @classmethod
    def describe(cls, source: str) -> str:
        """One sentence, for the UI, about where this text was processed.

        INPUT: source token from a feed entry.
        OUTPUT: str. "rules"/"memory" -> the local sentence; "llm" -> names the
          configured provider; anything else -> an explicit "unknown, treat as
          external" sentence rather than silence.
        SIDE EFFECTS: none.  ERROR STATES: none.
        """
        s = str(source).strip().lower()
        if s in ("rules", "local"):
            return cls.LOCAL_TEXT
        if s == "memory":
            return ("Generated on this machine from your building's own history "
                    "— no data left the building.")
        if s == "llm":
            return (f"Sent to {cls.provider()} for parsing — the complaint text "
                    f"left this machine.")
        return (f"Processing location unknown for source '{source}' — treat it as "
                f"external until confirmed.")


# --------------------------------------------------------------------------
# 6. One-line integration helper
# --------------------------------------------------------------------------

def apply_privacy(entry: dict, config: PrivacyConfig = DEFAULTS,
                  salt: str | None = None) -> dict:
    """Run a whole feed entry through the configured privacy switches.

    This is the single call backend.app would add to handle_complaint to turn
    this module on: `entry = privacy.apply_privacy(entry, cfg)`.

    INPUT: entry (feed dict with author/text/source), config (PrivacyConfig),
      salt (override for anonymize_author, for tests).
    OUTPUT: a NEW dict — the input is never mutated, so a caller that keeps the
      raw record for its own audit trail still can. Adds/updates: text
      (scrubbed), redacted (list of kinds, only when something was removed),
      author (pseudonymised), author_anonymous (bool), disclosure (sentence),
      external_ai (bool).
    SIDE EFFECTS: may create the process salt on first call.
    ERROR STATES: none; missing keys are treated as empty strings.
    """
    out = dict(entry)
    if config.scrub:
        clean, found = scrub_pii(str(out.get("text", "")))
        out["text"] = clean
        if found:
            out["redacted"] = found
    if config.anonymous_mode:
        author = str(out.get("author", ""))
        pseudo = anonymize_author(author, salt=salt)
        out["author"] = pseudo
        out["author_anonymous"] = pseudo != author
    if config.disclose_ai:
        src = str(out.get("source", ""))
        out["disclosure"] = AIDisclosure.describe(src)
        out["external_ai"] = AIDisclosure.is_external(src)
    return out

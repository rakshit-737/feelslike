"""Constraint store: complaints become decaying control constraints, with
conflict arbitration and human-readable explanations.

A complaint's influence decays exponentially (half-life 45 min) and expires
after 2 h unless re-reported — recency-weighted democracy, not a ticket queue.

Later additions (all strictly additive; every pre-existing caller — app.py,
memory.py, env.py — keeps its exact behaviour because the new fields default to
the old semantics):
  * one complaint can name several zones -> add_many() files one Constraint per
    zone sharing a complaint_id, so the UI can group them and an all-clear can
    find its siblings;
  * a constraint can be un-approved (human_approval safety mode). Unapproved
    constraints are RECORDED and VISIBLE but are excluded from the weighting in
    zone_adjustments(), so they influence nothing until an operator approves;
  * constraint_views() projects the live decay maths into the flat, json-safe
    shape backend.contracts.ConstraintView pins, which is what the
    explainability panel renders.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

from backend.contracts import OBJECTIVES, ConstraintView, new_id, to_dict

HALF_LIFE_S = 45 * 60.0
EXPIRY_S = 2 * 3600.0

# issue -> (setpoint offset per severity level {1,2,3}, vent delta)
ISSUE_EFFECTS = {
    "too_hot":  ({1: -0.8, 2: -1.3, 3: -1.8}, 0),
    "too_cold": ({1: +0.8, 2: +1.3, 3: +1.8}, 0),
    "stuffy":   ({1: -0.3, 2: -0.4, 3: -0.5}, +1),
    "humid":    ({1: -0.4, 2: -0.5, 3: -0.6}, +1),
    "drafty":   ({1: +0.3, 2: +0.4, 3: +0.5}, -1),
}

_ids = itertools.count(1)
_complaint_ids = itertools.count(1)      # deterministic, never uuid4


def _opposed(cs) -> bool:
    """True when a set of constraints pulls the setpoint both ways at once.
    (Same rule the original explain() used inline, factored out so
    zone_adjustments and explain cannot drift apart.)"""
    return len({1 if c.raw_offset > 0 else -1 for c in cs if c.raw_offset != 0}) > 1


@dataclass
class Constraint:
    id: int
    zone: str
    issue: str
    severity: int
    confidence: float
    created_t: float          # sim-seconds timestamp
    raw_offset: float
    vent_delta: int
    text: str = ""
    author: str = "anonymous"
    # --- additive fields; the defaults reproduce the original behaviour -----
    complaint_id: str = ""    # groups the sibling constraints of one utterance
    approved: bool = True     # False = filed but withheld from control
    anonymous: bool = False   # author was anonymised; UI must not show a handle
    rejected: bool = False    # operator said no. Distinguishes "denied" from
                              # "still waiting" — both have approved == False,
                              # but only the waiting ones are pending_approvals().

    @classmethod
    def from_issue(cls, zone, issue, severity, confidence, now_t, text="", author="anonymous",
                   complaint_id="", approved=True, anonymous=False):
        offs, vent = ISSUE_EFFECTS.get(issue, ({1: 0.0, 2: 0.0, 3: 0.0}, 0))
        sev = int(min(max(severity, 1), 3))
        return cls(next(_ids), zone, issue, sev, float(confidence), now_t,
                   offs[sev], vent, text, author,
                   str(complaint_id), bool(approved), bool(anonymous))

    # ---- projections -----------------------------------------------------
    def age_min(self, now_t: float) -> float:
        """Sim-minutes since this complaint was filed (never negative)."""
        return max(0.0, now_t - self.created_t) / 60.0

    def expires_in_min(self, now_t: float) -> float:
        """Sim-minutes of life left before EXPIRY_S; 0.0 once expired."""
        return max(0.0, (EXPIRY_S - max(0.0, now_t - self.created_t)) / 60.0)

    def counts(self) -> bool:
        """True when this constraint is allowed to influence control at all.

        INPUT: none. OUTPUT: bool. SIDE EFFECTS: none. ERROR STATES: none.
        Time-independent: decay() handles the time half of "does this count".
        """
        return self.approved and not self.rejected

    def view(self, now_t: float) -> dict:
        """ConstraintView-shaped, json-safe snapshot at now_t.

        INPUT: now_t sim-seconds. OUTPUT: dict with exactly the ConstraintView
        fields (id, issue, severity, confidence, weight, age_min,
        expires_in_min, raw_offset, author, text). An anonymous constraint
        reports author "anonymous" and never leaks the original handle.
        SIDE EFFECTS: none. ERROR STATES: none.
        """
        return to_dict(ConstraintView(
            id=self.id, issue=self.issue, severity=self.severity,
            confidence=round(self.confidence, 3),
            weight=round(self.weight(now_t), 3),
            age_min=round(self.age_min(now_t), 1),
            expires_in_min=round(self.expires_in_min(now_t), 1),
            raw_offset=self.raw_offset,
            author="anonymous" if self.anonymous else self.author,
            text=self.text,
        ))

    def decay(self, now_t: float) -> float:
        age = max(0.0, now_t - self.created_t)
        return 0.0 if age > EXPIRY_S else 0.5 ** (age / HALF_LIFE_S)

    def weight(self, now_t: float) -> float:
        return self.severity * self.confidence * self.decay(now_t)


class ConstraintStore:
    def __init__(self):
        self.items: list[Constraint] = []
        self.objective: str = "balanced"   # reported in decisions; see set_objective

    def add(self, c: Constraint) -> dict:
        self.items.append(c)
        return self.explain(c.zone, c.created_t)

    def active(self, now_t: float, zone: str | None = None, counting_only: bool = False):
        """Constraints that have not decayed away yet.

        counting_only=True additionally drops un-approved / rejected items, i.e.
        exactly the set that is allowed to move a setpoint. The default False
        keeps the original meaning ("filed and not stale"), which is what the
        dashboard's active_constraints badge and comfort-memory already count.
        """
        return [c for c in self.items
                if c.decay(now_t) > 0.02 and (zone is None or c.zone == zone)
                and (not counting_only or c.counts())]

    # ---- multi-zone entry point ------------------------------------------
    def add_many(self, zones, issue, severity, confidence, now_t, text="",
                 author="anonymous", complaint_id="") -> list:
        """File ONE complaint against SEVERAL zones.

        INPUT: zones (iterable of zone_id; duplicates collapsed, order kept),
          issue/severity/confidence/now_t/text/author as for Constraint.from_issue,
          complaint_id (blank -> a fresh deterministic "cmp-0001" id).
        OUTPUT: list[Constraint], one per zone, all sharing complaint_id, in the
          order the zones were given. Empty list for an empty zone iterable.
        SIDE EFFECTS: appends every constraint to self.items.
        ERROR STATES: none — an unknown zone string is stored verbatim and will
          simply never match a real zone in zone_adjustments().
        Note: unlike add(), this returns the constraints, not an explanation;
        call explain(zone, now_t) per zone if you need the sentences.
        """
        seen, ordered = set(), []
        for z in zones or []:
            if z and z not in seen:
                seen.add(z)
                ordered.append(z)
        if not ordered:
            return []
        cid = str(complaint_id) or new_id("cmp", _complaint_ids)
        out = []
        for z in ordered:
            c = Constraint.from_issue(z, issue, severity, confidence, now_t,
                                      text=text, author=author, complaint_id=cid)
            self.items.append(c)
            out.append(c)
        return out

    def by_complaint(self, complaint_id: str) -> list:
        """Every constraint filed by one utterance, in creation order.

        INPUT: complaint_id string. OUTPUT: list[Constraint] (empty for an
        unknown or blank id — blank never matches, because constraints created
        through add() legitimately carry "").
        SIDE EFFECTS: none. ERROR STATES: none.
        """
        cid = str(complaint_id)
        return [c for c in self.items if cid and c.complaint_id == cid]

    # ---- objective -------------------------------------------------------
    def set_objective(self, o: str) -> str:
        """Record which objective the operator selected.

        INPUT: an Objective string. OUTPUT: the objective now in force.
        SIDE EFFECTS: sets self.objective only. This does NOT change physics or
          arbitration by itself — the controller reads it and the decision
          engine reports it; storing it here just keeps one source of truth.
        ERROR STATES: none; an unknown string falls back to "balanced" rather
          than taking the controller down over a UI typo.
        """
        self.objective = o if o in OBJECTIVES else "balanced"
        return self.objective

    # ---- human approval --------------------------------------------------
    def pending_approvals(self) -> list:
        """Constraints filed but neither approved nor rejected yet.

        INPUT: none. OUTPUT: list[Constraint] in creation order. Time-independent
        on purpose: an operator must still see (and be able to dismiss) a request
        that decayed while it sat in the queue.
        SIDE EFFECTS: none. ERROR STATES: none.
        """
        return [c for c in self.items if not c.approved and not c.rejected]

    def approve(self, constraint_id: int) -> bool:
        """Let a withheld constraint start influencing control.

        INPUT: Constraint.id. OUTPUT: True if a constraint was flipped, False if
          the id is unknown. SIDE EFFECTS: sets approved=True, rejected=False.
          The decay clock is NOT reset — an approval 40 min later applies what is
          left of the complaint, not a fresh one.
        ERROR STATES: none.
        """
        for c in self.items:
            if c.id == constraint_id:
                c.approved, c.rejected = True, False
                return True
        return False

    def reject(self, constraint_id: int) -> bool:
        """Operator denies a constraint: it keeps its place in history (comfort
        memory still learns the complaint happened) but never moves a setpoint.

        INPUT: Constraint.id. OUTPUT: True if found, else False.
        SIDE EFFECTS: sets approved=False, rejected=True. created_t is left
          alone so the pattern miner keeps its timestamp.
        ERROR STATES: none.
        """
        for c in self.items:
            if c.id == constraint_id:
                c.approved, c.rejected = False, True
                return True
        return False

    # ---- projections for the explainability UI ---------------------------
    def constraint_views(self, zone: str, now_t: float) -> list:
        """The active constraints of one zone as ConstraintView-shaped dicts.

        INPUT: zone_id, now_t sim-seconds.
        OUTPUT: list[dict] (id, issue, severity, confidence, weight, age_min,
          expires_in_min, raw_offset, author, text), heaviest first so the UI's
          first row is the one actually driving the setpoint. Includes
          un-approved items — the panel needs to show what is waiting — so a
          consumer that wants "what moved the number" must filter on weight.
        SIDE EFFECTS: none. ERROR STATES: none; unknown zone -> [].
        """
        cs = self.active(now_t, zone)
        cs.sort(key=lambda c: (-c.weight(now_t), c.id))
        return [c.view(now_t) for c in cs]

    def stats(self, now_t: float | None = None) -> dict:
        """Counts by zone and issue, plus active/expired/pending/rejected totals.

        INPUT: now_t sim-seconds. When omitted, the newest created_t in the store
          is used as "now" (so stats() with no clock still classifies sensibly);
          an empty store reports zeros.
        OUTPUT: dict(now_t, total, active, expired, pending, rejected,
          by_zone{zone:{total,active}}, by_issue{issue:{total,active}}).
        SIDE EFFECTS: none. ERROR STATES: none.
        """
        if now_t is None:
            now_t = max((c.created_t for c in self.items), default=0.0)
        by_zone: dict = {}
        by_issue: dict = {}
        n_active = 0
        for c in self.items:
            live = c.decay(now_t) > 0.02
            n_active += 1 if live else 0
            for table, key in ((by_zone, c.zone), (by_issue, c.issue)):
                row = table.setdefault(key, {"total": 0, "active": 0})
                row["total"] += 1
                row["active"] += 1 if live else 0
        return {
            "now_t": now_t,
            "total": len(self.items),
            "active": n_active,
            "expired": len(self.items) - n_active,
            "pending": len(self.pending_approvals()),
            "rejected": sum(1 for c in self.items if c.rejected),
            "by_zone": by_zone,
            "by_issue": by_issue,
        }

    # ---- independent copy -------------------------------------------------
    def clone(self) -> "ConstraintStore":
        """Deep copy for what-if runs: mutating the clone never touches self.

        Every Constraint is rebuilt with dataclasses.replace, so the clone's
        items are distinct objects — approving, rejecting or clearing a zone on
        the clone leaves the live store untouched. Ids are preserved (the clone
        describes the SAME complaints), which is what lets a scenario's
        decisions be compared against the live ones.

        INPUT: none. OUTPUT: a new ConstraintStore. SIDE EFFECTS: none.
        ERROR STATES: none.
        """
        c = ConstraintStore()
        c.objective = self.objective
        c.items = [replace(x) for x in self.items]
        return c

    def clear_zone(self, zone: str, now_t: float) -> int:
        """All-clear from an occupant: expire the zone's active constraints now.
        Items stay in history (comfort-memory mines them) but stop influencing
        control. Returns how many were cleared."""
        cleared = 0
        for c in self.items:
            if c.zone == zone and c.decay(now_t) > 0.02:
                c.created_t = now_t - EXPIRY_S - 1.0
                cleared += 1
        return cleared

    def zone_adjustments(self, now_t: float) -> dict:
        """zone -> {setpoint_offset, vent_delta, n, conflict, pending, weight}.
        Opposing constraints are arbitrated by weighted mean (weight = severity
        x confidence x decay). The first three keys and their meanings are
        frozen (controller + dashboard read them); the rest are additive.

        Only constraints that count() — approved and not rejected — are weighted,
        so a complaint awaiting human approval appears in the UI but moves
        nothing. Zones whose constraints are ALL pending are omitted entirely,
        exactly as if nothing had been filed."""
        out = {}
        by_zone: dict[str, list[Constraint]] = {}
        pending: dict[str, int] = {}
        for c in self.active(now_t):
            if c.counts():
                by_zone.setdefault(c.zone, []).append(c)
            else:
                pending[c.zone] = pending.get(c.zone, 0) + 1
        for zone, cs in by_zone.items():
            wsum = sum(c.weight(now_t) for c in cs)
            if wsum <= 0:
                continue
            offset = sum(c.raw_offset * c.weight(now_t) for c in cs) / wsum
            vent = sum(c.vent_delta for c in cs if c.weight(now_t) > 0.25)
            out[zone] = {"setpoint_offset": round(offset, 2),
                         "vent_delta": max(-1, min(1, vent)), "n": len(cs),
                         "conflict": _opposed(cs),
                         "pending": pending.get(zone, 0),
                         "weight": round(wsum, 3)}
        return out

    def explain(self, zone: str, now_t: float) -> dict:
        """Human-readable arbitration summary for one zone.

        Returns the original four keys (zone, conflict, adjustment, summary)
        plus additive "arbitration" (a backend.contracts.ARBITRATION_MODES key)
        and "pending" (count of constraints awaiting approval in this zone)."""
        cs = [c for c in self.active(now_t, zone) if c.counts()]
        n_pending = len(self.active(now_t, zone)) - len(cs)
        adj = self.zone_adjustments(now_t).get(zone)
        conflict = _opposed(cs)
        parts = [f"{c.issue.replace('_', ' ')} (sev {c.severity}, "
                 f"{int((now_t - c.created_t) / 60)}m ago)" for c in cs]
        if not adj:
            text = ("No active constraints." if not n_pending else
                    f"{n_pending} request(s) awaiting approval — nothing applied.")
        elif conflict:
            text = (f"CONFLICT in zone: {' vs '.join(parts)} -> severity-weighted "
                    f"compromise: {adj['setpoint_offset']:+.1f} degC")
        else:
            text = (f"{len(cs)} active request(s): {', '.join(parts)} -> "
                    f"setpoint {adj['setpoint_offset']:+.1f} degC"
                    + (f", airflow {adj['vent_delta']:+d}" if adj["vent_delta"] else ""))
        arb = ("none" if not adj else
               "conflict_weighted_mean" if conflict else
               "single" if len(cs) == 1 else "weighted_mean")
        return {"zone": zone, "conflict": conflict, "adjustment": adj, "summary": text,
                "arbitration": arb, "pending": n_pending}

    def unmet_pressure(self, twin, minutes: float) -> float:
        """RL reward term: active complaints whose zone still feels wrong.
        Only counted constraints apply — an unapproved complaint cannot be
        'unmet' by a controller that was never allowed to act on it."""
        p = 0.0
        for c in self.active(twin.t, counting_only=True):
            T = twin.T.get(c.zone, 25.0)
            if (c.issue == "too_hot" and T > 25.5) or (c.issue == "too_cold" and T < 23.5):
                p += c.weight(twin.t) * minutes / 60.0
        return p

---
target: dashboard/index.html
total_score: 26
max_score: 40
na_heuristics: 
p0_count: 2
p1_count: 2
timestamp: 2026-08-14T18-51-17Z
slug: dashboard-index-html
---
# Critique — dashboard/index.html

Method: dual-agent (A: design-review subagent · B: detector subagent). Browser overlay unavailable (no browser automation); detector ran via CLI.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Live clock + latency tile good; backend death = silent freeze (tick() swallows errors, no stale cue) |
| 2 | Match System / Real World | 3 | Chip shows raw zone_b/too_hot ids while plan says "Conference Room B"; "vent" vs "fan" drift |
| 3 | User Control and Freedom | 2 | No pause, no click-to-clear constraint; input text destroyed before fetch resolves |
| 4 | Consistency and Standards | 3 | Good token system, but badge colors, temp ramp, legend chips hardcoded outside it; radius scale drifts |
| 5 | Error Prevention | 2 | No in-flight lock on Send (double-Enter duplicates constraints); input clears pre-success |
| 6 | Recognition Rather Than Recall | 3 | sev/conf scales unexplained; zone-id→room mapping left to viewer memory |
| 7 | Flexibility and Efficiency | 3 | Chips + Enter good; no demo-reset or keyboard speed control |
| 8 | Aesthetic and Minimalist Design | 3 | Restrained but uniformly quiet — hierarchy never peaks where the story does |
| 9 | Error Recovery | 2 | llm→rules fallback graceful; send() no try/catch — Wi-Fi flake eats typed text silently |
| 10 | Help and Documentation | 2 | 60×/240×/960× and sev/conf scales explained nowhere |
| **Total** | | **26/40** | **Acceptable** |

## Design Specificity Verdict

Authored in structure, generic in dress (~60/40). Product-shaped bones: 5-zone SVG floor plan, quick-chips = demo script, parse-source/latency badges. Visual language is default ops-dashboard; --warn/--crit tokens defined and never used (scaffold fingerprint).

Deterministic scan: clean — 1 advisory (em-dash overuse), confirmed false positive (11/13 dashes are no-data placeholders in tiles). No slop/contrast/quality hits.

## Priority Issues

- [P0] "Zero comfort violations" not on screen anywhere. /api/state ships viol_min for both twins; dashboard never reads it. Fix: comfort tile/strip "0 min vs 16,328", zero in --good, baseline in --crit.
- [P0] CONFLICT beat renders as visual "nothing new" — green applied badge + 12px italic muted text. explanation.conflict already in payload, unused. Fix: amber CONFLICT badge (--warn), tinted card border, pulse affected zone on floor plan.
- [P1] Headline saved-% is 12px sub-text; no literal racing meters. Fix: hero −26.6% (34–40px, --good) or paired racing bars with labeled gap; animate value transitions.
- [P1] Hostile input + failure handling: unsanitized innerHTML (feed XSS via "type anything" card), send() clears input before fetch w/ no try/catch, no in-flight lock, dead backend = silent freeze. Fix: escape text, restore input + retry note on failure, disable Send in flight, staleness pill.
- [P2] Dark mode half-committed: floor-plan ramp, legend chips, clarify badge hardcoded light. Fix: pin color-scheme: light for demo (or tokenize ramp).

## Persona Red Flags

- Alex (technical judge): XSS via feed innerHTML; optimistic speed buttons (state flips before fetch); hardcoded client MID=24.75.
- Jordan (non-technical judge): headline invisible at 12px; zone_b/sev/conf jargon; unoccupied zones drift deep red at 960× — reads "broken" against the comfort claim.
- Presenter (time pressure): failed send eats typed text; llm→rules failover tile never updates after first feed item; no demo-reset.

## Minor Observations

--warn/--crit unused; base_temp never rendered; chart x-axis labels thin to 1–2 at full week (quiet at climax); tooltip can overflow right edge; no aria-live on feed; .viz-root scaffold vestige.

## Questions to Consider

1. Energy priced three ways, comfort zero — pitching "cheaper" when the moat is "without sacrifice"?
2. Why is the baseline a gray line instead of a character? What would two buildings visibly competing look like?
3. What does the screen say 30 s after the pitch ends? Designed final scorecard for peak-end?

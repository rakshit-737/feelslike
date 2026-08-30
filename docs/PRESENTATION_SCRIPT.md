# FeelsLike — Judge Presentation Script
**Team Goldilocks · "Buildings that listen"**
Format: 8 minutes · One presenter (P) + one demo driver (D) · Word-for-word

---

## How to use this document

- **Bold text in brackets** = stage direction, do not read aloud.
- Plain text = say it, roughly as written. Don't memorise it verbatim; memorise the *beats* and the *numbers*.
- `D:` lines = what the demo driver does, silently, on cue.
- Timings are cumulative. If you're running long, the cut-first blocks are marked **[CUT IF SHORT]**.

**Never say a number that isn't in this script.** Every figure here came out of a real run.

---

## 0:00 — 0:50 · The Hook

**[Slide 1: title, or the space-heater-under-a-desk photo. Presenter stands away from the laptop.]**

Good morning. I want to start with the most common object in an air-conditioned office.

It's a space heater. Under a desk. In a building that is actively spending electricity to make that room cold.

That's not a joke about office politics — it's an engineering failure. Commercial buildings run their HVAC on a fixed schedule: twenty-two degrees, nine to six, every day, whether the room has forty people in it or nobody at all. And because the building can't hear anyone, it over-cools everything, just in case. HVAC is roughly forty to fifty percent of a commercial building's electricity bill. A very large share of that is spent cooling rooms that nobody is sitting in, and annoying the people who are.

The reason is simple. Buildings have no idea how anyone in them feels. The only feedback channel that exists is a facilities ticket that gets read three days later.

We're Team Goldilocks. We built FeelsLike — a building that listens.

---

## 0:50 — 1:40 · What it is, in one breath

**[Slide 2: the architecture diagram — occupant → parser → constraints → twin → dashboard.]**

Here's the whole system in one sentence: **somebody complains in plain English, we turn that sentence into a typed, time-limited engineering constraint, and a controller folds that constraint into the setpoint schedule of a live thermal simulation of the building.**

Four pieces.

One — a **digital twin**. A five-zone office, simulated with a lumped RC thermal model: heat flows between zones, in from the sun, in from the people, out to the outdoors, and the HVAC fights it with finite capacity. It steps every simulated minute.

Two — a **parser** that turns "it's stuffy in Conference Room B" into strict JSON: which zone, what issue, how severe, how confident.

Three — a **constraint engine**. Complaints aren't commands. They become weighted constraints that decay with a forty-five-minute half-life and expire after two hours — because you being cold at 2 p.m. shouldn't govern the building at 6 p.m.

Four — a **second, identical twin** running the dumb static schedule on bit-identical weather, with bit-identical occupancy. Same building, same day, same people. The only difference between the two is the controller.

That A/B pair is the whole evidentiary basis of this project. Every number I show you today is the gap between those two buildings.

**[Beat. Move to the laptop.]**

Let me stop describing it and just run it.

---

## 1:40 — 3:10 · Live Demo, Part 1: the building hears you

**[D: dashboard already open at the floor plan, simulation running at 60×. Do NOT start it now — it should already be warm.]**

**[P: point at the floor plan on screen.]**

Five zones. Live temperature, live setpoint, live vent stage, live occupancy. The meter on the right is energy — us versus the baseline building. They started at the same instant with the same weather.

Now — I'm an occupant. I don't file a ticket. I don't install an app. I type the sentence I'd have said out loud anyway.

**[D: type into the chat — `it's really stuffy in Conference Room B` — and hit send.]**

Watch three things happen in about a second.

**[Point at each as it appears.]**

First — the parsed JSON chip. Zone B. Issue: stuffy. Severity two. Confidence, and a latency badge. That badge matters: it tells you whether that parse came from the LLM or from our offline rules parser. If the venue Wi-Fi dies in the middle of this demo, that badge flips from `llm` to `rules` and **nothing else changes** — the whole system keeps working with no network at all.

Second — the constraint is now live in the store, with its clock already ticking down.

Third — look at zone B on the floor plan. The setpoint has moved and the vent stage has stepped up. That's not an animation. That's the controller reading the constraint and the twin simulating the thermal response.

**[Optional, if time allows — [CUT IF SHORT]]**

And here's the part that builds trust with anyone who's ever deployed an LLM.

**[D: type `the projector in room B is broken`.]**

Nothing happens to the building. It's marked *ignored*. It's not a comfort complaint, so it never becomes a constraint. And if I name a room that doesn't exist, we don't guess — we null the zone and ask a clarifying question. **The model is never allowed to invent a zone or write a setpoint directly.** It fills a schema; the schema is validated; the controller acts on validated fields only.

---

## 3:10 — 4:10 · Live Demo, Part 2: the argument every office has

Now the interesting case. Because in a real office, the person in the next chair disagrees with you.

**[D: type `Room B is freezing, I'm wearing a jacket`.]**

**[Wait for the CONFLICT badge.]**

There it is. Two live, opposing constraints in the same zone. A naive system does one of two things here: it obeys whoever complained last, or it throws up its hands.

We arbitrate. The compromise is a weighted mean of the opposing constraints — weighted by **severity, by the parser's confidence, and by recency** — and the system prints the reasoning on screen in a sentence a facilities manager can read.

And because every constraint decays, neither person wins forever. In ninety minutes this argument has half-faded on its own, and the building drifts back to its efficient schedule.

Every office on earth has this fight. Ours settles it transparently, in seconds, with the arithmetic shown.

---

## 4:10 — 5:30 · The race: does it actually save anything?

**[D: crank simulation speed to 960×. Let the meters run.]**

Everything so far is comfort. Now the part that pays for itself.

**[Point at the two racing meters as the gap opens.]**

Two buildings, one week, identical weather. Watch the gap open.

**[Slide 3: the results table. Read the numbers off it — don't improvise them.]**

Here are the measured results over seven simulated days.

The **static twenty-two degree schedule** — what buildings actually do today — burns **722 kilowatt-hours**, and racks up **sixteen thousand three hundred and twenty-eight comfort-violation minutes**. And here's the thing people find surprising: almost all of those violations are people being *too cold*. The building is over-cooling itself into discomfort while spending the most energy of anything on this table.

**FeelsLike** burns **530 kilowatt-hours** — **twenty-six point six percent less** — with **zero comfort violations**.

Now, the honest counter-example, because a judge is going to ask it and I'd rather answer it first. If you just turn the thermostat up to twenty-four degrees and react when it drifts, you save **thirty-one point seven percent** — *more* than us. So why not do that?

Because that thermostat breaks comfort for **four hundred and twenty-nine minutes**. It buys its extra five percent by making people miserable, and in a real building those people go buy space heaters — and you lose the savings anyway.

**That's our entire thesis: efficiency without sacrifice.** Anyone can save energy by making a building uncomfortable. The hard problem is saving energy while comfort gets *better*, and that's only possible if the building can hear what "uncomfortable" actually means to the people inside it.

**[Slide 4: money/carbon tiles — [CUT IF SHORT]]**

Scaled to a ten-thousand square-metre office, that ratio is on the order of eighteen to twenty-two lakh rupees a year and around a hundred and sixty tonnes of CO₂. I'll flag that as an extrapolation from our simulation, not a measured building.

---

## 5:30 — 6:45 · Under the hood, and where it's weak

**[Slide 5: NLP benchmark table.]**

I want to spend a minute on the parts that are still hard, because I think that's more useful to you than another feature.

**The parser.** We benchmark it on seventy cases. But the number I want to quote you is from a **blind probe** — sentences never used to develop the parser. And our probe *rotates*: we caught our held-out split inflating to a hundred percent after a rewrite, built a blind probe that scored fifty — and then, when we studied *that* probe's failures to write fixes, we declared it burned, archived it, and wrote a fresh one. The scorer now refuses to run a burned probe. What I quote is the fresh probe, measured exactly once.

On those twenty unseen sentences: **zone extraction is ninety percent** — multi-zone, typos, Hinglish and Tamil-English included, and both misses were an extra real zone, never an invented one. The **full exact triple** — right zone, right issue, complaint detected — is **forty-five percent for the rules parser and fifty-five for the LLM**.

That's a modest number and I'm quoting it deliberately — a harder honest number beats a softer stale one. What fails is what you'd expect: fresh metaphor ("greenhouse vibes"), inverted sarcasm ("sponsored by the penguin exhibit"), implied-heat Hinglish. Those are in our committed failure log, on purpose. The ten-point gap is also exactly *why* the LLM is the product and the rules parser is the safety net.

And critically — a parser miss is a **non-event**, not a disaster. Low confidence triggers a clarifying question. An unknown zone is nulled. A non-comfort message is ignored. The failure mode of this system is "it asks you to rephrase," not "it freezes the third floor."

**[Slide 6: RL learning curve — [CUT IF SHORT]]**

**Reinforcement learning.** We also trained a PPO agent for two million steps on a Gymnasium environment wrapped around the same twin — reward is negative energy, negative discomfort, negative unmet complaints.

It burns **512 kilowatt-hours** — twenty-nine percent savings, better than our shipped controller. But it accumulates **twenty-two violation-minutes**, and our rules-based controller sits at zero. So we made a deliberate call: **the demo ships the constraint-aware controller, and the RL agent is presented as trajectory, not as product.** The learning curve was still climbing when we stopped. We'd rather show you an honest zero than a slightly better number with an asterisk on it.

---

## 6:45 — 7:30 · Is this real engineering, or a demo?

**[Slide 7: architecture / production path.]**

Three things I'd want to know if I were sitting where you are.

**Is the twin real physics or a lookup table?** It's a standard lumped-parameter RC model — the same family used in building-simulation literature. Inter-zone coupling, solar gain by orientation, occupancy heat gain, finite cooling capacity, coefficient of performance three point four. Both twins update simultaneously — Jacobi, not sequential — so the order of the zones can't change the answer. The calibration path is fitting R and C from a week of real BMS logs.

**Is the comparison rigged?** It can't be. Both twins are seeded from the same value, they step inside the same loop body, and every stochastic input is derived from that seed. They cannot drift by even one step. We have forty-nine contract tests that fail loudly if anything breaks that property, and the run is deterministic — same seed, same kilowatt-hours, every time.

**What's the path to production?** The architecture doesn't change. The twin gets swapped for BACnet or Modbus writes — we've already built those as protocol seams, with simulated implementations, and we deliberately did *not* write fake network clients. The complaint channel already works over Slack, which is where these conversations happen anyway. And it's privacy-clean by construction: everything is aggregated to the zone level. We never need to know who was cold.

---

## 7:30 — 8:00 · Close

**[Step back from the laptop. Slower.]**

We've been building buildings that can measure temperature for a hundred years. Not one of them can tell the difference between twenty-two degrees and *comfortable* — because that difference lives in a person, and nobody ever gave the building a way to ask.

We gave it one. And the moment a building can hear the people inside it, it turns out it doesn't have to choose between the electricity bill and the person in the jacket. It saves twenty-seven percent and the complaints go to zero — not despite each other, but *because* of each other.

Buildings have been deaf for a century. We taught one to listen.

**[Hand the judges the card / gesture at the laptop.]**

The laptop is open and it's still running. Type anything you like into it — try to confuse it. We'll show you what it does when it fails, too.

Thank you.

---
---

# Appendix A · Q&A — answer these in two sentences, not ten

**"How do you stop the LLM hallucinating a setpoint?"**
It never writes a setpoint. It fills a validated JSON schema — zone, issue, severity, confidence — and unknown zones are nulled, low confidence triggers a clarifying question, and non-comfort messages are dropped. The controller only ever reads validated fields.

**"Forty-five percent parse accuracy sounds low."**
That's the blind-probe exact-triple on twenty sentences the parser has never seen — probe v2, because v1 got burned the moment we studied its failures, and we replaced it rather than re-quote it. Zone extraction on the same set is ninety percent with zero invented zones. We quote the blind number precisely because our tuned split reads a hundred percent and we don't trust it. A miss costs one clarifying question, not a wrong action.

**"Is the RL real?"**
Two million PPO steps, learning curve and ablation table available. It wins on energy — 512 kWh — and loses on comfort at twenty-two violation-minutes, so we didn't ship it. That's the whole reason it's presented as trajectory.

**"Two people disagree — who wins?"**
Neither, permanently. Severity × confidence × recency weighted compromise with the reasoning printed, and every constraint decays with a forty-five-minute half-life.

**"How real is the twin?"**
Lumped-parameter RC, literature-standard, with zone coupling, solar, occupancy and finite capacity. It's not calibrated to a real *building* — the path is fitting R and C from a week of BMS logs, and the fitting tool exists and is tested (`scripts/fit_rc.py`, recovers synthetic truth within 5%); [if the rig ran: "we fitted it to our physical test zone — here's the overlay"]. I'd want site data before quoting savings on a specific building.

**"Zero violations seems too clean."**
It's zero *in simulation*, where cooling capacity is adequate by construction. Say that out loud. The comparison is still valid because the baseline runs on the identical twin — it's the same idealisation on both sides.

**"What if the internet dies right now?"**
It already might have. Look at the parse-source badge — if it says `rules`, we've been running fully offline for the last two minutes and you didn't notice.

**"What's genuinely broken?"**
Our process is: every defect found gets a strict-xfail test documenting its precise mechanism, then a general fix, then it converts to a plain regression — never a memorized patch. As of the finals build the ledger is at zero open xfails, and the fixes are readable in the tests ("sweater" once prefix-matched "sweat"; "hotter than" once fuzzy-matched the Hinglish "thand"). The honest liabilities that remain are in the limitations register: the sim-to-real gap, humidity not billed to kWh, and the blind-probe 45/55% exact-triple parse — with the full failure log committed, and a probe that burns itself the moment we study it.

---

# Appendix B · The only numbers you may say on stage

| Claim | Number |
|---|---|
| Static baseline | 722 kWh · 16,328 violation-minutes |
| Reactive 24 °C thermostat | 493 kWh (−31.7%) · **429** violation-minutes |
| **FeelsLike (shipped)** | **530 kWh (−26.6%) · 0 violation-minutes** |
| PPO agent (not shipped) | 512 kWh (−29.0%) · 22 violation-minutes |
| Blind probe v2 (single-shot, 2026-08-30) — zone set | rules 90% · LLM 90% (misses = extra real zone, never invented) |
| Blind probe v2 — exact triple | rules 45% · LLM 55% (Groq gpt-oss-120b, auto mode) |
| Probe v1 (burned 2026-08-17 after its failures were studied) | last blind scores were 50/60; its 80% re-score is NOT blind — never quote it |
| Benchmark size | 70 cases + 20-case rotating blind probe |
| Test suite | 464 passing (49 contract + behavioural suites) · 0 open xfail defects |
| Calibration fit tool self-test | R, C, τ recovered within 0.5% of synthetic truth (both sensor grades) — *synthetic, not hardware* |
| Constraint decay | 45-min half-life, 2-hour expiry |
| Comfort band | 23.0 – 26.5 °C, only counted when a zone is occupied |
| HVAC share of commercial electricity | ~40–50% |
| Scale estimate (label it an estimate) | ~₹18–22 lakh/yr · ~160 tCO₂/yr per 10,000 m² |

---

# Appendix C · Pre-flight, 15 minutes before you present

1. `python -m scripts.demo_day` — confirm the table still prints 722.4 / 493.4 / 530.3. If it doesn't, use whatever it prints and update the slide.
2. `uvicorn backend.app:app --reload` — dashboard up, sim warm at 60×, at least a few minutes of history on the chart so the race line isn't empty.
3. Send one throwaway complaint and delete/reset — confirm the parse chip renders and the latency badge shows.
4. Check the parse-source badge. Know whether you're on `llm` or `rules` *before* you walk up, so you can say it confidently either way.
5. Backup video queued on two phones.
6. **Only demo panels that are verified.** All eleven tabs are built and their endpoints
   pass the API suite; walk each panel you plan to click ONCE in this pre-flight, and
   cut any that misbehaves. The guided Demo tab (START/NEXT) is the safest rail.
7. If the rig is present: `GET /api/hw/status` must say `connected: true` and the
   Twin tab's Physical-zone card must show a live curve. If it doesn't, present the
   no-rig variant — never debug hardware on stage.

---

# Appendix D · PROPOSED 3-minute finals cut (v3 — team tunes wording before rehearsal)

The 8-minute script above is the full version. Finals may only allow ~3 minutes +
Q&A. Rig beats are marked **[RIG]** with their no-rig fallback inline — rehearse both.

**0:00 — Hook + first complaint.** One sentence of hook ("buildings are deaf, so they
overcool everyone — HVAC is ~40–50% of a commercial building's electricity"). Then
immediately type *"it's really stuffy in Conference Room B"* → point at the parse
chip, the constraint, the setpoint/vent move. One breath on the `rules` badge =
works offline.

**0:30 — [RIG] The building is not only pixels.** "That zone isn't only simulated —
it's this box." Fan audibly spins up as the vent command reaches the ESP32; the
measured-temperature curve on the Twin tab's Physical-zone card bends. "Same
controller, same adapter seam a BACnet building would bind — this is the smallest
real building we could afford." *(No rig: run `python -m scripts.mock_node` beforehand —
the Twin tab card is then live end-to-end over real HTTP — and say the firmware and
conformance tests are in the repo, awaiting parts: built, not claimed.)*

**1:10 — Conflict.** *"Room B is freezing, I'm wearing a jacket"* → CONFLICT badge →
weighted compromise with the printed reasoning. "Every office has this argument;
ours settles it with arithmetic on screen, and it decays — nobody wins forever."

**1:40 — The race.** Crank to 960×. Read the table: **722 static / 16,328
viol-min · 493 reactive / 429 viol-min · 530 FeelsLike / 0 viol-min**. "The
thermostat that 'saves more' buys it with 429 minutes of discomfort. Efficiency
without sacrifice is the whole point." Money/carbon tiles, labeled extrapolation.

**2:15 — Why + honest limits.** Click one decision open in the Explain tab ("Why did
the system change this?" — the real audit record, no LLM prose). **[RIG]** Show the
calibration overlay (measured vs simulated step response) — "we fitted our twin's R
and C to physical hardware." *(No rig: show the fit tool's synthetic self-test and
say so.)* One breath on limitations: blind-probe 45/55% exact triple with 90% zone +
clarify-don't-guess; humidity tracked, not billed; zero violations is in-simulation.

**2:45 — Close.** "Buildings have been deaf for a hundred years. We taught one to
listen — and gave it hands." Hand over the laptop: *type anything, try to confuse it.*

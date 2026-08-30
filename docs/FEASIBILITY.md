# FeelsLike — Feasibility, Cost & Limitations

**Status of every number in this file:** `[verified]` = read from a primary or named
source on the access date; `[measured]` = produced by a run of this repo's code;
`[assumption]` = a labeled modeling choice; `[estimate]` = our judgment, range given.
Nothing here is presented as measured that is extrapolated. Prices move — re-check
before purchase; sources and access dates are listed in §6.

---

## 1. Bill of materials and unit economics

### 1.1 The demo rig (one physical zone, what we actually built/are building)

| Item | ₹ (incl. GST unless noted) | Status |
|---|---|---|
| ESP32 DevKit (38-pin) | 358–399 | `[verified]` quartzcomponents / robu, 2026-08-30 |
| DHT22 temp+RH module | 124 | `[verified]` robu |
| — or SHT31 breakout (better accuracy) | 379 | `[verified]` robu |
| D4184 MOSFET module ×2 (fan + heater) | ~140 (2 × ₹70 incl. GST) | `[verified]` electronicscomp (₹59 excl. GST) |
| 40 mm 5 V fan | 185 | `[verified]` robu (LED version; ~₹75 listings exist, unfetched) |
| 10 W wirewound resistor (heat source) | 21–23 | `[verified]` electronicscomp |
| Breadboard + jumpers | 81–151 | `[verified]` robu |
| Toggle switch, USB cable, box | 100–150 | `[estimate]` local market |
| **Rig total (DHT22 build)** | **≈ ₹1,050–1,250** | fits the ₹1,500 budget with margin |

### 1.2 A deployment sensor node (per zone, no-BMS retrofit path)

The demo fan and calibration heater are rig-only; a deployed node senses and
actuates through an IR blaster and/or a smart plug.

| Item | ₹ | Status |
|---|---|---|
| ESP32 DevKit | 358–399 | `[verified]` |
| SHT31 temp+RH | 379 | `[verified]` (DHT22 at ₹124 for cost-down, worse drift) |
| PIR occupancy (HC-SR501) | 62 | `[verified]` robu |
| IR blaster (5 mm IR LED + 2N2222 + resistor) | 15–20 | `[verified]` parts; ready modules `[estimate]` ₹40–80 |
| PSU, enclosure, wiring, mounting | 200 | `[estimate]` |
| **Node total** | **≈ ₹1,050 (≈ ₹1,100 with margin)** | |
| Optional: SCD40 CO₂ | +1,539 | `[verified]` robu — air-quality tier only |
| Optional: 16 A energy-monitoring smart plug (Tapo P110) | +949–989 | `[verified]` — for plug-controlled units + per-AC kWh metering |

### 1.3 One realistic 20-zone office floor (~2,000 m²)

| Line | ₹ | Status |
|---|---|---|
| 20 sensor+IR nodes | 21,000–22,000 | from §1.2 |
| Gateway: Raspberry Pi 4 (2 GB) | 6,449 | `[verified]` robu (Pi Zero 2 W ₹2,079 works; the building's existing PC works at ₹0) |
| Installation + commissioning | 10,000 | `[estimate]` ₹500/zone: mount, join Wi-Fi, name the zone, verify readings — the bring-up checklist in `hardware/README.md` is the procedure |
| Spares (10%) | 2,100 | `[estimate]` |
| **Total, 20 zones** | **≈ ₹39,500 (call it ₹40k)** | |

---

## 2. Two deployment paths, named precisely

### 2.1 No-BMS building (the majority of Indian offices)

Our nodes + IR blasters for split ACs + optional smart plugs. The adapter seam
(`backend/adapters.py`) binds as: `SensorAdapter` ← node readings over HTTP (exactly
what the rig does today); `HVACAdapter.write_setpoint` → IR codes for the AC's own
protocol; `write_vent` → fan/mode IR codes or a smart plug; `OccupancyAdapter` ← PIR.

**Honest about what this controls:** an IR blaster commands what the remote can —
setpoint, mode, fan, on/off — one-way, with no feedback that the command landed
(mitigation: the temperature trend IS the acknowledgment; a command that changes
nothing raises the same maintenance alert a broken actuator does). Split-AC
setpoints step in 1 °C, so the controller's ±1.8 °C offsets quantize. Central
plant (AHU dampers, chillers) is out of scope on this path.

### 2.2 BMS building (BACnet / Modbus)

The same three calls the controller already makes — `read_state`, `write_setpoint`,
`write_vent` — bind to: BACnet AV/AO (setpoint, priority array 8–16) + MSO (fan)
per zone, or Modbus holding registers (setpoint ×10, fan level; function 06/16).
Point list per zone: ZN-T, ZN-RH, ZN-CO2 (AI), ZN-OCC (BI), CLG-SP (AV, writable),
FAN-SPD (MSO, writable) — five reads, two writes, printed in the adapters module
docstring. Commissioning = a systems integrator maps those points and runs the same
adapter-conformance test our simulated and HTTP adapters pass
(`tests/test_hardware.py::test_http_adapters_pass_the_same_conformance_gate_as_sim`).

---

## 3. Payback math — every assumption labeled

**Measured basis `[measured]`:** 192.1 kWh saved over 7 simulated days on the 5-zone,
520 m² model (722.4 − 530.3 kWh), at zero comfort-violation minutes.
Reproduce: `python -m scripts.demo_day`.

**Assumptions:**
- **A1 `[assumption]`** Every week ≈ the simulated hot week → ×52.14. Annualized:
  **10,016 kWh/yr** saved on 520 m² = **19.3 kWh/m²/yr**. Context: Indian office EPI
  runs 200–400 kWh/m²/yr `[verified]`, HVAC ≈ 40–60% of office electricity
  `[verified]` (LBNL-2001147 / BEE ECO-III) — so this is ~5–10% of total building
  electricity, consistent with 26.6% of the HVAC share.
- **A2 `[assumption]`** A 20-zone floor scales linearly from the 5-zone model
  (2,080 m², same zone mix): **40,065 kWh/yr** at face value.
- **A3 `[verified]` tariff:** TANGEDCO **LT-V commercial**, TNERC Order 6/2025
  (eff. 2025-07-01): ₹6.65/kWh first 100 units, **₹10.45/kWh above** (every office),
  ToD: peak (6–10 am, 6–10 pm) +25% = ₹13.06, night −5%; +5% TN electricity tax not
  included below. We compute at the marginal ₹10.45.
- **A4 `[assumption]` sim-to-real discount:** the honest unknown. The model's zero
  violations and perfect thermostat flatter reality; we therefore show 100% / 50% /
  25% of simulated savings. **The 25% row is the number we'd defend for a real
  building today**; the calibration program (fit R/C against logged hardware,
  `scripts/fit_rc.py`) exists to shrink this discount with evidence.

**Payback (₹40k capex, 20-zone floor), in months:**

| Savings realized ↓ · Tariff → | ₹7.84 (−25%) | **₹10.45** | ₹13.06 (+25% = peak) |
|---|---|---|---|
| 100% of simulated (40,065 kWh/yr) | 1.5 | **1.1** | 0.9 |
| 50% (20,032 kWh/yr) | 3.0 | **2.3** | 1.8 |
| **25% (10,016 kWh/yr) — defended case** | 6.0 | **4.5** | 3.6 |

Even at a quarter of simulated savings and a −25% tariff, payback is ~6 months.
CO₂ `[verified factor]`: 0.710 kgCO₂/kWh (CEA CO₂ Baseline Database v21.0, FY 2024-25
weighted average — the sim's constant is confirmed current): 28.4 tCO₂/yr at face
value, 7.1 tCO₂/yr in the defended case, per floor.

**Pricing-data note `[assumption]`:** the simulation bills a flat ₹9/kWh (between the
LT-V slab rates; changing it would move the published ₹ figures, so it stays until
re-measured). The verified tariff is ToD — peak +25% spans 6–10 am, which overlaps
morning pre-cool. Exploiting that (shift pre-cool off-peak) is unbuilt future work
with a designed landing place: the `cost` row of `OBJECTIVE_WEIGHTS` plus a tariff
curve where the flat `TARIFF` constant sits today.

---

## 4. Limitations register (we present this, we don't bury it)

| # | Limitation | Consequence | Mitigation / status |
|---|---|---|---|
| 1 | Sim-to-real gap: RC model ignores inter-zone airflow, radiant asymmetry, stratification; thermostat is "perfect within capacity" | Simulated savings flatter reality | A4 discount rows above; hardware calibration program (`fit_rc.py`, tested on synthetic truth ±0.5–5%); "in simulation" said on stage |
| 2 | Humidity tracked, never billed to kWh | Latent load absent from the headline numbers | Documented modeling assumption protecting the frozen A/B comparison (`sim/twin.py` docstring); `humid_viol_min` reported separately. Known sub-issue: coil ADP approximation pins monsoon RH high |
| 3 | NLP exact triple 45% (rules) / 55% (LLM) on blind probe v2 (single-shot; the probe rotates — one burned the moment its failures are studied) | Half of messy messages don't parse perfectly | Zone set 90% with zero invented zones + clarify-don't-guess + severity/offset clamps bound the damage to one question, not a wrong action; committed failure log |
| 4 | Sensor drift and death | Bad readings could steer control | Health is inferred (stale/stuck/out-of-range/impossible-dew-point), dead zone falls back to schedule; calibration cadence `[estimate]`: annual RH check for SHT31 (typ. drift <0.25%/yr spec), DHT22 tier replaced not recalibrated at ₹124 |
| 5 | Actuator/relay wear; IR is one-way | Commands may not land | Fan MOSFETs are solid-state; IR non-ack mitigated by temperature-trend verification → maintenance alert on no-response |
| 6 | Network loss (nodes offline) | No fresh readings/commands | Node watchdog forces actuators off in ≤10 s; controller holds last safe scheduled state; zone degrades to today's dumb behaviour, never worse |
| 7 | Single gateway | One point of failure | Stateless nodes + a ₹2,079–6,449 cold spare; the building reverts to its existing schedule on gateway loss |
| 8 | 50-zone scale | One process, in-memory state | Fine for a floor (the sim steps 5 zones at 960× on a laptop); multi-floor needs a DB-backed store and per-floor processes — architecture supports it, unbuilt, `[estimate]` |
| 9 | ToD pricing not exploited | Savings understate the peak-tariff opportunity | §3 pricing note; deliberate — no unverifiable optimization shipped |
| 10 | Zero violations is an in-simulation result | Capacity adequacy is by construction | Same idealization on both sides of the A/B race; said aloud on stage |

---

## 5. What this changes about the pitch

One sentence for the deck: *"₹40,000 retrofits a 20-zone floor with no BMS; at a
deliberately discounted quarter of our simulated savings and the verified TANGEDCO
commercial tariff, it pays back in about 4–5 months — and every assumption behind
that sentence is labeled in the repo."*

## 6. Sources (accessed 2026-08-30)

- Parts: robu.in, electronicscomp.com (prices excl. GST, converted), quartzcomponents.com, amazon.in/Blinkit (Tapo P110 street price)
- Tariff: TNERC Tariff Order No. 6 of 2025 (eff. 1 Jul 2025) via pwrnxt.in breakdown; slab corroborated by tnebbillcalculator.com. Fixed charges `[estimate]` ₹80–120/kW/month (secondary sources; order PDF not fetched)
- Grid CO₂: CEA CO₂ Baseline Database User Guide v21.0 (Nov 2025), Table S — weighted average 0.710 tCO₂/MWh FY 2024-25 (primary PDF, cea.nic.in)
- HVAC share: LBNL-2001147 *A Guide for High-Performance Energy Efficient Buildings in India* (40–60% of office electricity), drawing on BEE/USAID ECO-III
- Office EPI: 200–400 kWh/m²/yr typical; 180 benchmark (ECO-III-derived secondary sources)

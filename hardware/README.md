# FeelsLike Hardware Node ("shoebox zone")

A single ESP32-based sensor+actuator node that turns one cardboard/acrylic box into a
physical zone for the FeelsLike digital twin. The node reads temperature and humidity,
POSTs them to the gateway, and applies the fan/heater commands the server returns.

Firmware lives in `firmware/feelslike_node/feelslike_node.ino`. The wire protocol it
speaks (`/api/hw/reading` request/response fields) is frozen; do not rename fields on
either side.

Status labels used below: **implemented** = exists in this repo; **server-side** =
behavior the gateway enforces per the frozen contract.

## Bill of materials

Prices intentionally omitted here; they belong in `docs/FEASIBILITY.md`.

| Qty | Item | Notes |
|-----|------|-------|
| 1 | ESP32 dev board | Any "ESP32 Dev Module"-compatible board with micro-USB |
| 1 | DHT22 **or** SHT31 breakout | SHT31 is the better sensor; DHT22 is the cheaper default. Pick one, set the matching `#define` |
| 2 | Logic-level MOSFET module | e.g. IRLZ44N module or D4184 module; one for fan, one for heater. Must switch fully at 3.3 V gate drive |
| 1 | 5 V fan, 40 mm | Ventilation actuator |
| 1 | Heat source | 10 W wirewound power resistor on a small heatsink, or a 12 V filament bulb. Must be rated for continuous operation at the supply voltage |
| 1 | Physical toggle switch | In series with the actuator 5 V supply line — see safety layer 0 |
| 1 | Breadboard | |
| — | Jumper wires | Male-male and male-female |
| 1 | Micro-USB cable | Power + flashing |
| 1 | Cardboard or acrylic box | The "shoebox zone" enclosure |

## Wiring

| ESP32 pin | Connects to | Notes |
|-----------|-------------|-------|
| GPIO 4 | DHT22 data | DHT22 build only. 10 kΩ pull-up from data to 3V3 if the breakout lacks one |
| GPIO 21 | SHT31 SDA | SHT31 build only (I2C) |
| GPIO 22 | SHT31 SCL | SHT31 build only (I2C) |
| 3V3 | Sensor VCC | DHT22 or SHT31 |
| GPIO 16 | Fan MOSFET module signal/gate input | 25 kHz PWM from firmware |
| GPIO 17 | Heater MOSFET module signal/gate input | Plain on/off, no PWM |
| GND | Sensor GND, both MOSFET module GNDs, actuator supply negative | Common ground is mandatory |

Actuator power path: 5 V rail (USB or external supply) → **physical toggle switch** →
fan positive and heater positive. Each load's negative goes to the drain of its MOSFET
module; the modules switch the low side to ground.

Rules that are not optional:

- Fan and heater draw power from the 5 V rail, **never** from a GPIO. GPIOs drive only
  the MOSFET gate inputs.
- ESP32 ground and actuator supply ground must be tied together. Without a common
  ground the MOSFETs will not switch reliably (or at all).
- The heater must be rated for continuous operation at the supply voltage and mounted
  clear of the cardboard walls (heatsink in free air, nothing touching it).

## The physical off switch (safety layer 0)

A toggle switch sits in series with the actuator 5 V supply line. When it is open,
no fan and no heater — regardless of what the firmware, the server, or a wiring
mistake does. It is a hardware kill that no software state can override. Flip it off
before touching anything inside the box.

## Safety model — three layers

| Layer | Where | Mechanism |
|-------|-------|-----------|
| 0 | Hardware | Physical toggle switch in the actuator supply line. No software can override an open switch |
| 1 | Firmware (implemented) | Boot-safe: both actuators off before anything else runs. Watchdog: no good server response (HTTP 200 + parseable `ok:true`) for `watchdog_s` seconds (default 10) → fan and heater forced off until the next good response. Heater pin only goes HIGH when the latest good response said `heater=true` and the watchdog is fresh. WiFi loss → actuators off, then reconnect |
| 2 | Server-side | Heater duty-cycle cap: at most 50% on-time over any 10-minute window; past the cap the server commands heater off no matter what the optimizer wants. Command envelope: fan clamped to 0–2 |

The layers are independent on purpose: layer 2 assumes the network works, layer 1
assumes the firmware runs, layer 0 assumes nothing.

## Flashing

1. Install Arduino IDE 2.x.
2. Boards Manager → install **esp32** by Espressif Systems.
3. Library Manager → install:
   - **DHT sensor library** (Adafruit) — DHT22 build, or **Adafruit SHT31** — SHT31 build
   - **ArduinoJson**
4. Open `firmware/feelslike_node/feelslike_node.ino`.
5. Set the three CHANGE-ME defines at the top: `WIFI_SSID`, `WIFI_PASS`, `GATEWAY_URL`.
   If using an SHT31, also swap the `SENSOR_*` define.
6. Tools → Board → **ESP32 Dev Module**. Select the board's COM port.
7. Upload. Open Serial Monitor at **115200** baud; you should see boot, WiFi connect,
   sensor readings, and POST results.

## Bring-up checklist

Do these in order. Do not skip ahead to the heater.

1. **Flash with actuators disconnected.** Verify sensor readings in the Serial Monitor
   and `[post] ... HTTP 200` lines (gateway must be running and reachable).
2. **Verify the gateway sees the node.** `GET /api/hw/status` on the gateway should
   show `shoebox-1` with a recent reading.
3. **Connect the fan only.** Command a fan level from the server side and verify the
   fan spins at levels 1 and 2 and stops at 0.
4. **Verify the watchdog.** Stop the gateway server. The fan must stop within
   `watchdog_s` seconds (default 10) and the Serial Monitor must log the trip.
   Restart the server and verify recovery.
5. **Connect the heater last.** Verify the server-side duty cap: leave the heater
   commanded on for more than 5 minutes and watch the server force it off before the
   50%-per-10-minutes window is exceeded. Keep a hand near the physical switch during
   this test.

## Hardware-day runbook (everything else is already proven in software)

The entire server side, calibration pipeline and demo moment are tested without
hardware — on the day, only the wiring and firmware flash are new. In order:

1. `python -m scripts.preflight` — all offline checks must PASS before touching parts.
2. **Rehearse first, solder second:** `uvicorn backend.app:app` +
   `python -m scripts.mock_node` — the dashboard's Physical-zone card comes alive,
   and a complaint on the rig zone must raise the mock fan. That is the exact
   behaviour the real node must reproduce; any difference later is firmware/wiring.
3. Flash and bring up the real node per the checklist above (actuators last).
4. `python -m scripts.preflight --server http://127.0.0.1:8000` — the INFO line
   must say the node is CONNECTED.
5. Calibration, one command (~65 min, fan off, quiet building):
   `python -m scripts.run_calibration --power <measured W>` — it drives the
   heater schedule (the server duty cap chopping it is expected; the fitter uses
   the recorded waveform and recovers R/C within ~10% under the cap — proven by
   `tests/test_calibration.py::test_end_to_end_bridge_log_fits_under_the_real_duty_cap`),
   saves the log, fits R/C, and writes the deck overlay
   (`evals/calibration_overlay.svg`). Measure `--power` (V²/R at the real supply
   voltage); do not guess it.
6. Team eyeballs the overlay → flip the `hardware` / `calibration` capability
   flags in `scripts/update_docs.py` → `python -m scripts.update_docs`.
7. Record the backup video with the rig working; then rehearse the no-rig variant.

If parts fail: `scripts.mock_node` IS the A5(b) fallback — sensing-and-actuation
behaviour demonstrated in software, presented as exactly that.

## What this node is not

There is no cooling plant and no setpoint actuation on this node. The fan is the
ventilation actuator; the heater is a calibration step-input and disturbance source,
not a comfort device. Setpoint writes to this zone are rejected at the adapter seam
(`HttpHVACAdapter.write_setpoint` returns `False` with the audited reason
"rig is vent-only") rather than pretended — a setpoint implies closed-loop
heating/cooling capacity the hardware does not have, and `capabilities()` says so
in machine-readable form (`supports_setpoint: false`).

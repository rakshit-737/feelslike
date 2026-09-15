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
| 1 | **L9110 / L9110S dual H-bridge** *(what this rig uses)* | One module covers both channels: A drives the fan, B the heater. 2.5–12 V supply, 800 mA per channel, ~1.2 V bridge drop. Set `#define DRIVER_L9110` |
| 2 | — *or* logic-level MOSFET module | e.g. IRLZ44N or D4184; one for fan, one for heater. Must switch fully at 3.3 V gate drive. Set `#define DRIVER_MOSFET` |
| 1 | DC fan, 40 mm | Ventilation actuator. Match the supply: a 12 V fan needs a 12 V rail — it will not start reliably on 5 V |
| 1 | Heat source | A single wirewound power resistor sized for ~2.5–3 W at the rail (10 Ω / 10 W on 5 V, 47 Ω / 10 W on 12 V), **or** ~20 ordinary ¼ W resistors in parallel across the rail — see the sizing rule below |
| 1 | Physical toggle switch | In series with the actuator supply line — see safety layer 0 |
| 1 | Fuse (0.5–1 A) | In the same supply line, ahead of the switch. Cheap insurance against a shorted heater |
| 2 | 2-pin screw terminal block | Lands the supply and the heater leads properly instead of trusting breadboard friction at ½ A |
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
| GPIO 16 | Fan driver input — L9110 **A-IA**, or MOSFET gate | PWM from firmware (10 kHz on L9110, 25 kHz on a MOSFET) |
| GPIO 17 | Heater driver input — L9110 **B-IA**, or MOSFET gate | Plain on/off, no PWM |
| GND | Sensor GND, driver GND, **L9110 A-IB and B-IB**, actuator supply negative | Common ground is mandatory |

**Bridge drop matters on a 5 V rail.** The L9110 loses roughly 0.5 V across itself at
these currents (more at higher current), so a 5 V supply puts about 4.4 V on the load.
A 5 V fan still starts; the heater delivers ~1.9 W instead of 2.5 W through a 10 Ω
resistor, which is still a usable step. If the fan is visibly weak at level 2, switch
that channel with a logic-level MOSFET instead (`#define DRIVER_MOSFET`) or raise the
rail. A USB-C PD charger does NOT give more than 5 V to plain wires — its 9/12 V modes
need a PD trigger board to negotiate them.

**L9110 wiring.** Tying `A-IB` and `B-IB` to GND turns each H-bridge channel into a
plain one-direction switch: `IA` high (or PWM) drives the load, `IA` low stops it. The
motor supply goes to the module's `VCC`/`+` and the loads to `OA`/`OB`. Nothing in the
firmware's safety path changes — only the wiring and the PWM frequency differ between
the two driver options.

Actuator power path: supply rail → **fuse** → **physical toggle switch** → driver module
`VCC`. Loads connect to the driver outputs, not to the rail directly.

Rules that are not optional:

- Fan and heater draw power from the actuator rail, **never** from a GPIO. GPIOs drive
  only the driver's logic inputs.
- ESP32 ground and actuator supply ground must be tied together. Without a common
  ground the driver will not switch reliably (or at all).
- Keep total current inside the driver's rating — 800 mA per channel on the L9110. Size
  the heater accordingly and measure it before trusting it.
- The heater must be rated for continuous operation at the supply voltage and mounted
  clear of the cardboard walls (heatsink in free air, nothing touching it).

**Heater sizing rule.** Aim for ~2.5–3 W: that moves a shoebox about 3 °C, which is a
clean step against a DHT22's ±0.5 °C accuracy. Power is V²/R, so on a 5 V rail use
10 Ω (2.5 W, 0.5 A), on 9 V use 27 Ω (3.0 W, 0.33 A) and on a 12 V rail use 47 Ω
(3.1 W, 0.26 A) — in every case a wirewound part rated 5 W or more, never an ordinary
¼ W resistor. The target is not sharp: anything landing between roughly 2 and 4 W is a
usable step, so on 12 V any value from 39 Ω to 68 Ω will do, which is what shop stock
tends to look like. If you are building the
heater from ordinary ¼ W resistors instead, put ~20 in parallel across the rail and keep
each one under half its rating: 220 Ω each on 5 V (0.11 W each, 2.3 W total), or 1 kΩ
each on 12 V (0.14 W each, 2.9 W total). Never put a single ¼ W resistor across the rail
— a lone 10 Ω on 5 V is 2.5 W in a part rated for 0.25 W, and it will burn. Whatever you
fit, measure the actual V and R and pass the real wattage to
`scripts/run_calibration.py --power`.

## The physical off switch (safety layer 0)

A toggle switch sits in series with the actuator supply line (5 V or 12 V, whichever
the fan needs). When it is open,
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
5. Copy `secrets.h.example` to `secrets.h` in the same folder and fill in `WIFI_SSID`,
   `WIFI_PASS` (a **2.4 GHz** network - the ESP32 cannot see 5 GHz) and `GATEWAY_URL` (the
   laptop's LAN IP, which changes with DHCP). `secrets.h` is git-ignored; never commit it.
   Also run the server with `--host 0.0.0.0`, or the node's POSTs are refused.
   Check the two hardware defines match what you actually fitted: `SENSOR_DHT22` /
   `SENSOR_SHT31`, and `DRIVER_L9110` / `DRIVER_MOSFET`. Each pair is guarded by an
   `#error`, so getting it wrong fails at compile time rather than on the bench.
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

## Ambient reference node (Arduino Uno + LM35)

A second, **sensor-only** node. It measures the room air around the rig and reaches the
server over a wired USB-serial link instead of Wi-Fi. That gives the rig a true ambient
reference, and it shows the adapter seam taking a second transport — the shape of the
RS-485/Modbus sensor buses real buildings use. It never receives commands and drives
nothing. Firmware: `firmware/ambient_node_uno/ambient_node_uno.ino` (no libraries needed).

It posts to `POST /api/hw/sensor`, **not** to `/api/hw/reading`: the rig's endpoint holds
exactly one actuator node and replies with fan/heater commands, so a second node there
would overwrite the rig's reading. Sensor-only nodes get an acknowledgement and nothing else.

**Parts:** Arduino Uno, LM35, 3 jumper wires, the Uno's USB cable.

**Wiring** (LM35 flat face toward you, legs pointing down):

| LM35 pin | Uno pin |
|---|---|
| left (+Vs) | 5V |
| middle (Vout) | A0 |
| right (GND) | GND |

If the LM35 turns hot to the touch, +Vs and GND are swapped — unplug immediately.

**Flash:** Arduino IDE → Board **Arduino Uno** → the Uno's own COM port (**not** the ESP32's)
→ Upload. Serial Monitor at 115200 should show one JSON line every 2 s. **Close the Serial
Monitor before starting the bridge** — only one program can hold a serial port.

**Run the bridge** (server running):

```
pip install -r requirements-hardware.txt
python -m scripts.serial_bridge            # auto-detects the Uno, or pass --port COMx
```

Readings appear at `GET /api/hw/sensors`, and as `ambient` inside `/api/hw/status` and the
`hardware` block of `/api/state`.

**Cross-calibrate before trusting a single number.** The Uno's internal 1.1 V reference
varies up to ±10 % from chip to chip, which is about ±3 °C at room temperature.

1. Put the LM35 right beside the DHT22 — box open, fan off, heater off.
2. Wait about 15 minutes for both to settle.
3. `python -m scripts.crosscal_ambient --minutes 10`
4. Set `VREF_V` in the Uno sketch to the suggested value, set `CROSS_CALIBRATED = true`,
   and upload again. Until then the dashboard labels the room reading *uncalibrated*.

The LM35 is linear through 0 °C, so a single gain correction (the reference) is the
physically right fix; the script also reports the offset left over as a check. The result
is only as good as the DHT22's own ±0.5 °C accuracy, and the script prints that uncertainty.
Then move the LM35 outside the box, where it measures the room.

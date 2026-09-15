# Hardware simulation (Phase 5)

> **SIMULATED HARDWARE IS NOT REAL HARDWARE.** These devices do not exist physically, the
> protocols are in-process simulations with no network, and nothing here claims to describe a
> particular commercial sensor. The one real device in the project remains the ESP32 bench rig
> (`backend/hardware.py`, source `hardware`).

Code: `backend/simhw.py`, `backend/app.py` (`/api/simhw*`, `LiveSim.set_simhw`),
`dashboard/scenario.js` (Hardware simulation panel). Tests: `tests/test_simhw.py`.

## 1. Pipeline (the Phase-4 path, unchanged)

```
 digital twin state (truth: zone T, RH, CO2 estimate, occupancy)
        │  sample every sampling_interval_s (wall clock)
        ▼
 SimulatedDevice   + bias + drift + gaussian noise × noise_level → saturate to range → round to precision
        │          faults: offline | stuck | drift | invalid | delay ; random dropouts (failure_probability)
        ▼
 ProtocolAdapter.encode → decode     SIMULATED MQTT (topic + JSON) · Modbus (scaled register)
        │                            · BACnet (analog-input present-value) · HTTPS (JSON body)
        ▼  (+ comm delay: delivered later, timestamped at sampling)
 LatestStore.ingest(source="sim", origin="simulated_hardware", sensor_id=device_id)
        ▼
 validation · freshness · comfort · demand · /api/latest · dashboard
```

While simulated hardware is **on**, the twin stops publishing its direct temperature, humidity,
CO₂ and occupancy readings (their old readings are removed), so a failed sensor really leaves the
value Unavailable or stale — the twin cannot silently fill the gap. Turning it **off** removes the
simulated devices' readings and the twin publishes directly again.

## 2. Device registry and mapping

20 devices = 4 sensor types × 5 modelled zones. Mapping is stored in the backend
(device → sensor type/metric → zone → floor → building), never inferred from labels.

| Type | Id | Metric | Default noise | Precision | Range | Simulated protocol |
|---|---|---|---|---|---|---|
| TEMP | `SIM-TEMP-ZONE-A` | temperature | ±0.2 °C | 0.1 | −40..85 °C | MQTT |
| HUM | `SIM-HUM-ZONE-A` | humidity | ±1 %RH | 0.1 | 0..100 % | MQTT |
| CO2 | `SIM-CO2-ZONE-A` | co2 | ±30 ppm | 1 | 0..5000 ppm | Modbus |
| OCC | `SIM-OCC-ZONE-A` | occupancy (+ derived occupancy %) | 0 | 1 | 0..500 | BACnet |

Registry fields: `device_id, sensor_type, metric, unit, zone_id, floor_id, building_id, protocol,
protocol_label ("SIMULATED MQTT"…), firmware_version ("sim-fw-1.0"), sampling_interval_s,
noise_sd, precision, range_lo/hi, bias, drift_per_hour, failure_probability, comm_delay_s,
fault, seq, dropped, simulated: true, source_label: "SIMULATED HARDWARE", status, last_seen,
age_s, quality, last_reading, rejected_value, pending_deliveries`.

Status: DISABLED · ONLINE (fresh) · DEGRADED (aging, or stuck/drift/delay fault) · STALE ·
INVALID (last reading rejected) · OFFLINE (offline fault, never seen, or > 30 min).

## 3. Characteristics and faults (bounded)

| Parameter | Range |
|---|---|
| sampling_interval_s | 1..300 |
| noise_sd | 0..200 (× global noise_level 0..3) |
| bias | −200..200 |
| drift_per_hour (per sim-hour) | −100..100 |
| failure_probability (per sample) | 0..0.5 |
| comm_delay_s | 0..600 |

| Fault | Effect | What the pipeline reports |
|---|---|---|
| offline | no samples | value ages → aging → **stale** (value kept, flagged); device OFFLINE; comfort names the stale input; system DEGRADED/STALE |
| stuck | repeats last value | fresh but frozen; device DEGRADED |
| drift | extra drift (0.5 °C, 2 %RH, 60 ppm, 1 person per sim-hour) | fresh, biased; device DEGRADED |
| invalid | out-of-range value | stored **invalid**, value null, rejected value kept; "Unavailable"; comfort dimension Unavailable |
| delay | +120 s delivery | readings arrive late with their sampling timestamp (age ≥ 120 s) |

A sensor saturated at its 85 °C range still fails the pipeline's 70 °C plausibility check and is
marked invalid — the pipeline stays authoritative. Faults can expire (`duration_s`).
`POST …/fault` samples that device once immediately so the effect is visible.

## 4. System response to sensor failure

- Temperature / humidity / CO₂ / occupancy: Unavailable (invalid) or stale — **never a fake zero**.
- Comfort: the affected dimension becomes Unavailable or flagged stale; the index renormalises over
  valid dimensions and lists the gap (Phase 3 rules).
- Telemetry health: DEGRADED / STALE DATA with reasons.
- **Controller: unchanged.** ConstraintAware reads the twin's own state, not sensor readings, so it
  keeps running normally. It has **no sensor-failure fallback**; that is a documented limitation,
  deliberately not added in this phase.

## 5. Security boundary (Phase 6 does the rest)

Device ids come only from this registry (anything else → 404); fault modes and parameters are
enumerated and bounded; payloads are plain data decoded by fixed adapters (no evaluation); ids and
values are re-validated by `LatestStore.ingest`; no credentials exist or are logged. Real MQTT /
Modbus / BACnet / HTTPS clients, device identity, authentication and encryption replace the
loopback adapters in Phase 6 without changing the pipeline.

"""SIMULATED HARDWARE (Phase 5). THIS IS NOT REAL HARDWARE.

A demonstration of how physical sensors would feed FeelsLike, using the Phase-4 pipeline
unchanged:

  digital twin (truth) ─> SimulatedDevice.sample()     noise, precision, range, bias, drift,
                                 │                     dropouts, faults
                                 ▼
                    ProtocolAdapter.encode ─> decode   SIMULATED MQTT / Modbus / BACnet / HTTPS
                                 │                     (in-process loopback; no network, no broker)
                                 ▼
                    LatestStore.ingest(source="sim", origin="simulated_hardware")
                                 ▼
                    comfort / demand / dashboard (exactly as for any other reading)

Readings keep source `sim` (they are simulated) with origin `simulated_hardware`, so they are
never confused with real hardware (`hardware`). Device ids come ONLY from the registry built
here; the API refuses any other id. The adapters are the Phase-6 seam: a real MQTT/Modbus/
BACnet client replaces the loopback without changing the pipeline.

While simulated hardware is ON, the twin stops publishing its direct temperature / humidity /
CO2 / occupancy readings, so a failed sensor really leaves the value Unavailable (the twin
cannot silently fill the gap). The controller still reads the twin's own state — it has no
sensor-failure fallback; that is a documented limitation, not something added here.
"""
from __future__ import annotations

import json
import random
import time
from collections import deque
from dataclasses import asdict, dataclass, field

from backend import latest as lv

ORIGIN = "simulated_hardware"
FAULTS = ("none", "offline", "stuck", "drift", "invalid", "delay",
          # Phase 6 security faults (only meaningful when a secure sink is attached)
          "bad_credentials", "replay", "spoof_zone", "malformed", "unknown_identity", "expired_identity")
SECURITY_FAULTS = FAULTS[6:]
FAULT_DRIFT_PER_HOUR = {"temperature": 0.5, "humidity": 2.0, "co2": 60.0, "occupancy": 1.0}
FAULT_DELAY_S = 120.0
# sensor type -> metric, unit, default noise sd, precision, measuring range, protocol
TYPES = {
    "TEMP": ("temperature", "°C", 0.2, 0.1, (-40.0, 85.0), "mqtt"),
    "HUM": ("humidity", "%", 1.0, 0.1, (0.0, 100.0), "mqtt"),
    "CO2": ("co2", "ppm", 30.0, 1.0, (0.0, 5000.0), "modbus"),
    "OCC": ("occupancy", "people", 0.0, 1.0, (0.0, 500.0), "bacnet"),
}
CONFIG_LIMITS = {"sampling_interval_s": (1.0, 300.0), "noise_sd": (0.0, 200.0), "bias": (-200.0, 200.0),
                 "drift_per_hour": (-100.0, 100.0), "failure_probability": (0.0, 0.5), "comm_delay_s": (0.0, 600.0)}


@dataclass
class SimulatedDevice:
    device_id: str
    sensor_type: str
    metric: str
    unit: str
    zone_id: str
    floor_id: str
    building_id: str
    protocol: str
    noise_sd: float
    precision: float
    range_lo: float
    range_hi: float
    sampling_interval_s: float = 5.0
    bias: float = 0.0
    drift_per_hour: float = 0.0
    failure_probability: float = 0.0
    comm_delay_s: float = 0.0
    firmware_version: str = "sim-fw-1.0"
    simulated: bool = True
    fault: str = "none"
    fault_until: float | None = None
    seq: int = 0
    last_sample_wall: float | None = None
    last_sim_t: float | None = None
    drift_c: float = 0.0
    last_value: float | None = None
    dropped: int = 0

    def public(self) -> dict:
        d = asdict(self)
        d["source_label"] = "SIMULATED HARDWARE"
        d["protocol_label"] = f"SIMULATED {self.protocol.upper()}"
        return d


# ------------------------------------------------------------------ protocol seam
class ProtocolAdapter:
    name = "base"
    simulated = True

    def encode(self, dev: SimulatedDevice, reading: dict):
        raise NotImplementedError

    def decode(self, dev: SimulatedDevice, payload) -> dict:
        raise NotImplementedError


class SimMQTT(ProtocolAdapter):
    name = "mqtt"

    def encode(self, dev, r):
        return {"topic": f"feelslike/{dev.building_id}/{dev.zone_id}/{dev.metric}",
                "payload": json.dumps({"v": r["value"], "ts": r["t_wall"], "seq": r["seq"], "dev": dev.device_id})}

    def decode(self, dev, p):
        body = json.loads(p["payload"])
        return {"value": body["v"], "t_wall": body["ts"], "seq": body["seq"]}


class SimModbus(ProtocolAdapter):
    name = "modbus"
    SCALE = 10

    def encode(self, dev, r):
        v = r["value"]
        return {"unit_id": 1, "register": 30001, "raw": None if v is None else int(round(v * self.SCALE)),
                "ts": r["t_wall"], "seq": r["seq"]}

    def decode(self, dev, p):
        return {"value": None if p["raw"] is None else p["raw"] / self.SCALE, "t_wall": p["ts"], "seq": p["seq"]}


class SimBACnet(ProtocolAdapter):
    name = "bacnet"

    def encode(self, dev, r):
        return {"object": "analog-input", "instance": abs(hash(dev.device_id)) % 4194303,
                "present_value": r["value"], "units": dev.unit, "ts": r["t_wall"], "seq": r["seq"]}

    def decode(self, dev, p):
        return {"value": p["present_value"], "t_wall": p["ts"], "seq": p["seq"]}


class SimHTTPS(ProtocolAdapter):
    name = "https"

    def encode(self, dev, r):
        return {"method": "POST", "path": "/telemetry", "body": {"value": r["value"], "t": r["t_wall"], "seq": r["seq"]}}

    def decode(self, dev, p):
        b = p["body"]
        return {"value": b["value"], "t_wall": b["t"], "seq": b["seq"]}


ADAPTERS = {a.name: a for a in (SimMQTT(), SimModbus(), SimBACnet(), SimHTTPS())}


def device_id(sensor_type: str, zone_id: str) -> str:
    return f"SIM-{sensor_type}-{zone_id.upper().replace('_', '-')}"


class SimHardware:
    def __init__(self, zones: list, floors: dict, building_id: str, seed: int = 0, clock=time.time):
        self.enabled = False
        self.noise_level = 1.0
        # Phase 6: when set, every decoded reading goes through this callable (the secure ingestion
        # pipeline) instead of straight into the store. sink(device, reading) -> stored record | None.
        self.sink = None
        self._now = clock
        self.rng = random.Random(f"simhw:{seed}")
        self.pending: deque = deque()
        self.devices: dict = {}
        for z in zones:
            for st, (metric, unit, sd, prec, rng_, proto) in TYPES.items():
                d = SimulatedDevice(device_id(st, z), st, metric, unit, z, f"F{floors.get(z, 1)}", building_id,
                                    proto, sd, prec, rng_[0], rng_[1])
                self.devices[d.device_id] = d

    # ------------------------------------------------------------------ control
    def set_fault(self, dev_id: str, mode: str, duration_s: float | None = None) -> SimulatedDevice:
        if dev_id not in self.devices:
            raise KeyError(dev_id)
        if mode not in FAULTS:
            raise ValueError(f"mode must be one of {FAULTS}")
        if duration_s is not None and not 1 <= float(duration_s) <= 86400:
            raise ValueError("duration_s must be between 1 and 86400")
        d = self.devices[dev_id]
        d.fault = mode
        d.fault_until = None if mode == "none" or duration_s is None else self._now() + float(duration_s)
        if mode == "none":
            d.drift_c = 0.0 if d.drift_per_hour == 0 else d.drift_c
        return d

    def configure(self, dev_id: str, **params) -> SimulatedDevice:
        if dev_id not in self.devices:
            raise KeyError(dev_id)
        d = self.devices[dev_id]
        for k, v in params.items():
            if k not in CONFIG_LIMITS:
                raise ValueError(f"unknown parameter {k!r}")
            lo, hi = CONFIG_LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
                raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
        for k, v in params.items():
            setattr(d, k, float(v))
        return d

    def clear_faults(self) -> None:
        for d in self.devices.values():
            d.fault, d.fault_until, d.drift_c = "none", None, 0.0

    # ------------------------------------------------------------------ sampling
    def tick(self, store, truth: dict, sim_t: float, occ_peak: dict, force: bool = False,
             only: str | None = None) -> int:
        """Sample due devices from the twin's truth, pass each reading through its simulated
        protocol, and ingest. Returns the number of readings delivered this call."""
        if not self.enabled:
            return 0
        now = self._now()
        n = 0
        while self.pending and self.pending[0][0] <= now:
            _, dev, reading = self.pending.popleft()
            n += self._ingest(store, dev, reading, occ_peak)
        for d in self.devices.values():
            if only is not None and d.device_id != only:
                continue
            if d.fault_until is not None and now >= d.fault_until:
                d.fault, d.fault_until = "none", None
            if not force and d.last_sample_wall is not None and now - d.last_sample_wall < d.sampling_interval_s:
                continue
            hours = 0.0 if d.last_sim_t is None else max(0.0, sim_t - d.last_sim_t) / 3600.0
            d.last_sample_wall, d.last_sim_t = now, sim_t
            if d.fault == "offline":
                continue
            if d.failure_probability and self.rng.random() < d.failure_probability:
                d.dropped += 1
                continue
            v = (truth.get(d.zone_id) or {}).get(d.metric)
            if v is None:
                continue
            d.drift_c += hours * (d.drift_per_hour + (FAULT_DRIFT_PER_HOUR[d.metric] if d.fault == "drift" else 0.0))
            x = float(v) + d.bias + d.drift_c + self.rng.gauss(0.0, d.noise_sd * self.noise_level)
            if d.fault == "stuck" and d.last_value is not None:
                x = d.last_value
            if d.fault == "invalid":
                x = d.range_hi * 10.0 + 1.0                   # out of the physical range
            else:
                x = min(d.range_hi, max(d.range_lo, x))        # a real sensor saturates at its range
                x = round(round(x / d.precision) * d.precision, 6)
                if d.metric == "occupancy":
                    x = int(round(x))
            d.last_value = x if d.fault != "invalid" else d.last_value
            d.seq += 1
            reading = {"value": x, "t_wall": now, "seq": d.seq, "sim_t": sim_t}
            adapter = ADAPTERS[d.protocol]
            decoded = adapter.decode(d, adapter.encode(d, reading))
            decoded["sim_t"] = sim_t
            delay = d.comm_delay_s + (FAULT_DELAY_S if d.fault == "delay" else 0.0)
            if delay > 0:
                self.pending.append((now + delay, d, decoded))
            else:
                n += self._ingest(store, d, decoded, occ_peak)
        return n

    def _ingest(self, store, d: SimulatedDevice, decoded: dict, occ_peak: dict) -> int:
        base = {"zone_id": d.zone_id, "floor_id": d.floor_id, "building_id": d.building_id,
                "metric": d.metric, "value": decoded["value"], "t_wall": decoded["t_wall"],
                "seq": decoded.get("seq"), "sim_t": decoded.get("sim_t"), "origin": ORIGIN}
        if self.sink is not None:
            rec = self.sink(d, decoded)
            if rec is None:
                return 0
        else:
            rec = store.ingest(base, "sim", d.device_id, d.device_id)
        if d.metric == "occupancy":
            pct = None if rec["value"] is None else round(100.0 * rec["value"] / (occ_peak.get(d.zone_id) or 1), 1)
            store.ingest({**base, "metric": "occupancy_pct", "value": pct, "origin": "derived"}, "derived",
                         f"DRV-{d.zone_id.upper().replace('_', '-')}-OCCPCT", d.device_id)
        return 1

    # ------------------------------------------------------------------ status
    def registry(self, store) -> list:
        now = self._now()
        out = []
        for d in self.devices.values():
            rs = store.query(sensor=d.device_id)
            r = rs[0] if rs else None
            if not self.enabled:
                status = "DISABLED"
            elif d.fault == "offline" or r is None:
                status = "OFFLINE"
            elif r["quality"] == "invalid":
                status = "INVALID"
            elif r["age_s"] > lv.OFFLINE_S:
                status = "OFFLINE"
            elif r["quality"] == "stale":
                status = "STALE"
            elif d.fault in ("stuck", "drift", "delay") or r["quality"] == "aging":
                status = "DEGRADED"
            else:
                status = "ONLINE"
            out.append({**d.public(), "status": status,
                        "last_seen": r["timestamp"] if r else None, "age_s": r["age_s"] if r else None,
                        "quality": r["quality"] if r else None, "last_reading": r["value"] if r else None,
                        "rejected_value": r.get("rejected_value") if r else None,
                        "pending_deliveries": sum(1 for _, dv, _ in self.pending if dv is d),
                        "_now": now})
        for o in out:
            o.pop("_now", None)
        return out

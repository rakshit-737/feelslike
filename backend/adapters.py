"""Integration seam: the five interfaces that stand between FeelsLike's control
logic and whatever is actually driving the building.

WHY THIS EXISTS. "Swap the twin for a real building" is either a true sentence
or a slide. It is only true if every place the controller touches the physical
world goes through a named interface with a fixed shape. Today exactly one
implementation of each interface exists — the simulated one, backed by
sim.twin.DigitalTwin. Tomorrow a BACnet or Modbus implementation registers under
the same name and nothing above this file changes.

NO REAL PROTOCOL CLIENT IS IMPLEMENTED HERE. There is no BACnet stack, no Modbus
master, no MQTT client, no HTTP call, and no new dependency. What follows is the
mapping a site engineer would implement against, written down so the shape of
the work is honest and estimable:

  HVACAdapter          BACnet: Analog Value / Analog Output objects for the zone
                       setpoint (present-value, priority array 8-16), Multi-State
                       Output for fan speed; BBMD or MS/TP for transport.
                       Modbus: holding registers (4x), setpoint scaled x10 in one
                       register, fan level in the next; function 06/16 to write.
                       MQTT: publish building/{site}/{zone}/setpoint/set, read
                       back .../setpoint/state; retained topics, QoS 1.

  OccupancyAdapter     BACnet Binary Input occupancy points from PIR sensors, or
                       a people-counter REST feed, badge-in turnstile events, or
                       WiFi/BLE association counts from the wireless controller.

  WeatherAdapter       Rooftop station mapped into the BMS as AI points, or a
                       forecast REST API (sim.weather.fetch_openmeteo is already
                       exactly this shape), or an ASHRAE TMY file replayed.

  SensorAdapter        BACnet Analog Input objects (zone temp, RH, CO2) or Modbus
                       input registers (3x); increasingly a LoRaWAN/Zigbee
                       gateway republishing to MQTT with per-device battery and
                       RSSI in the health payload.

  NotificationAdapter  Slack incoming webhook, Teams connector card, SMTP relay,
                       PagerDuty Events API, or the CMMS work-order endpoint.

A BMS point list for one zone therefore looks like: ZN-T (AI, degC), ZN-RH (AI,
%), ZN-CO2 (AI, ppm), ZN-OCC (BI), CLG-SP (AV, degC, writable), FAN-SPD (MSO,
1-3, writable). Five points and two writes per zone — that is the whole physical
integration surface of this product.

CONVENTIONS THAT HOLD ACROSS EVERY ADAPTER
- Reads raise KeyError for a zone that does not exist: asking for a point that
  is not on the bus is a programming error and must be loud.
- Writes never raise. They return False for an unknown zone, an out-of-range
  value or a rejected command, because a control loop that dies on one bad write
  stops controlling four healthy zones. Every write is appended to .writes.
- Nothing here holds a lock. backend.app owns the sim lock; adapters are called
  from inside it.
"""
from __future__ import annotations

import inspect
from typing import Protocol, runtime_checkable

from backend.privacy import scrub_pii
from sim.humidity import dew_point
from sim.twin import (
    CEILING_H,
    CP_AIR,
    DT,
    INFIL_ACH,
    RHO_AIR,
    VENT_UA,
    ZONE_BY_ID,
    ZONE_IDS,
    ZONES,
)
from sim.weather import outdoor_rh, solar_factor

# Command envelope every HVAC implementation must honour. A real site overrides
# these from the plant's own limits; they exist here so a bad write is rejected
# at the seam instead of inside the physics.
SETPOINT_MIN_C = 16.0
SETPOINT_MAX_C = 30.0
VENT_LEVELS = (0, 1, 2)

# CO2 estimate constants (see SimSensorAdapter.read).
CO2_OUTDOOR_PPM = 420.0      # ambient baseline, urban India 2024ish
CO2_PER_PERSON_LS = 0.0052   # L/s of CO2 exhaled, 1.2 met sedentary adult
CO2_MAX_PPM = 5000.0         # clamp; above this the estimate is meaningless


# ==========================================================================
# Protocols
# ==========================================================================

@runtime_checkable
class HVACAdapter(Protocol):
    """Write side of the plant: setpoints and fan levels for one zone at a time.

    Real counterpart: BACnet AV/AO + MSO objects, Modbus holding registers, or an
    MQTT command topic. See the module docstring for the point list.
    """

    def read_state(self, zone: str) -> dict:
        """Current commanded + measured state of one zone's HVAC.

        INPUT: zone id.
        OUTPUT: dict with at least temp_c, setpoint_c (commanded, None = off),
          vent (0|1|2), at_capacity (bool), capacity_w.
        SIDE EFFECTS: none (a real implementation performs a bus read).
        ERROR STATES: KeyError for an unknown zone; a real implementation may
          also raise its own transport timeout, which the caller must handle.
        """

    def write_setpoint(self, zone: str, celsius: float | None) -> bool:
        """Command a cooling setpoint. None means "HVAC off for this zone".

        INPUT: zone id, degC within capabilities()["setpoint_range_c"], or None.
        OUTPUT: True if the command was accepted, False if rejected.
        SIDE EFFECTS: stages/issues the command and appends to .writes.
        ERROR STATES: none — rejection is a False return, never an exception.
        """

    def write_vent(self, zone: str, level: int) -> bool:
        """Command a fan/ventilation level.

        INPUT: zone id, level in capabilities()["vent_levels"].
        OUTPUT: True if accepted, False if rejected.
        SIDE EFFECTS: stages/issues the command and appends to .writes.
        ERROR STATES: none.
        """

    def capabilities(self) -> dict:
        """What this plant can actually be asked to do.

        INPUT: none.
        OUTPUT: dict(protocol, writable, zones, setpoint_range_c, vent_levels,
          per_zone{...}). The controller reads this instead of hardcoding limits.
        SIDE EFFECTS: none.
        ERROR STATES: none.
        """


@runtime_checkable
class OccupancyAdapter(Protocol):
    """How many people are in a zone. Real counterpart: PIR, counters, badges."""

    def occupancy(self, zone: str) -> int:
        """People in the zone right now.

        INPUT: zone id.  OUTPUT: int >= 0.  SIDE EFFECTS: none.
        ERROR STATES: KeyError for an unknown zone.
        """

    def occupancy_pct(self, zone: str) -> float:
        """Occupancy as a percentage of that zone's design headcount.

        INPUT: zone id.  OUTPUT: float 0..(over 100 is legal when a room is
        oversubscribed).  SIDE EFFECTS: none.
        ERROR STATES: KeyError for an unknown zone.
        """


@runtime_checkable
class WeatherAdapter(Protocol):
    """Outdoor conditions. Real counterpart: rooftop station or forecast API."""

    def outdoor(self) -> dict:
        """Current outdoor conditions.

        INPUT: none.
        OUTPUT: dict(temp_c, rh_pct, solar_factor) where solar_factor is a 0..1
          clear-sky daylight fraction, 0 at night.
        SIDE EFFECTS: none.
        ERROR STATES: none from the sim implementation; a network implementation
          must return its last good reading rather than raising.
        """


@runtime_checkable
class SensorAdapter(Protocol):
    """Read side of the zone: the AI points. Separate from HVACAdapter because a
    building's sensors and its plant are usually different vendors on different
    buses, and because sensors fail independently of actuators."""

    def read(self, zone: str) -> dict:
        """One sensor sweep of a zone.

        INPUT: zone id.
        OUTPUT: dict(temp_c, rh_pct, co2_ppm) — any value may be None when that
          point is dead or absent. Implementations may add keys.
        SIDE EFFECTS: records the reading in the implementation's own history so
          health() has something to reason about.
        ERROR STATES: KeyError for an unknown zone.
        """

    def health(self, zone: str) -> dict:
        """Is this zone's instrumentation trustworthy?

        INPUT: zone id.
        OUTPUT: dict(zone, ok, faults[list of str], reads, last_read_t,
          stale_s). Faults are INFERRED from the reading history (staleness,
          zero variance, out-of-range, dew point above dry bulb), never simply
          echoed back from a known-fault flag.
        SIDE EFFECTS: none.
        ERROR STATES: KeyError for an unknown zone.
        """


@runtime_checkable
class NotificationAdapter(Protocol):
    """Outbound messages to humans. Real counterpart: Slack/Teams/SMTP/PagerDuty."""

    def notify(self, channel: str, text: str, meta: dict | None = None) -> bool:
        """Send one message.

        INPUT: channel (logical destination, e.g. "#facilities"), text, optional
          meta dict of structured context.
        OUTPUT: True if delivered/queued, False if dropped.
        SIDE EFFECTS: delivers the message; implementations SHOULD scrub PII
          first, since this is the one interface where occupant text leaves the
          system.
        ERROR STATES: none — a failed send returns False.
        """


PROTOCOLS: dict = {
    "hvac": HVACAdapter,
    "occupancy": OccupancyAdapter,
    "weather": WeatherAdapter,
    "sensor": SensorAdapter,
    "notify": NotificationAdapter,
}


# ==========================================================================
# Simulated implementations (the only ones that exist today)
# ==========================================================================

class SimHVACAdapter:
    """HVACAdapter backed by a DigitalTwin.

    Writes go into a command buffer that persists until changed, exactly like a
    real thermostat's setpoint register: the plant loop (twin.step) reads the
    buffer whenever it runs. commit() is that loop for callers who want the
    adapter to drive the twin directly; backend.app instead passes pending() to
    twin.step itself. Nothing is written to the twin by write_setpoint alone —
    that would let a UI action skip the physics.
    """

    def __init__(self, twin, max_writes: int = 500):
        """INPUT: twin (DigitalTwin), max_writes (audit-log ring size).
        OUTPUT: adapter.  SIDE EFFECTS: seeds the command buffer from the twin's
        last applied commands so a fresh adapter does not silently zero the
        plant.  ERROR STATES: AttributeError if twin is not a DigitalTwin."""
        self.twin = twin
        self.max_writes = int(max_writes)
        self.setpoints: dict = dict(getattr(twin, "last_setpoints", {}) or {})
        self.vents: dict = dict(getattr(twin, "last_vents", {}) or {})
        self.writes: list = []

    # -- protocol ---------------------------------------------------------
    def read_state(self, zone: str) -> dict:
        """See HVACAdapter.read_state. Adds the applied-vs-commanded split so a
        caller can see when the buffer has not reached the plant yet."""
        snap = self.twin.zone_snapshot(zone)      # raises KeyError if unknown
        return {
            "zone": zone,
            "temp_c": snap["temp_c"],
            "setpoint_c": self.setpoints.get(zone, snap["setpoint_c"]),
            "applied_setpoint_c": snap["setpoint_c"],
            "vent": int(self.vents.get(zone, snap["vent"])),
            "applied_vent": snap["vent"],
            "occ": snap["occ"],
            "rh_pct": snap["rh_pct"],
            "capacity_w": snap["capacity_w"],
            "at_capacity": snap["at_capacity"],
            "mode": "off" if self.setpoints.get(zone, snap["setpoint_c"]) is None else "cool",
        }

    def write_setpoint(self, zone: str, celsius: float | None) -> bool:
        """See HVACAdapter.write_setpoint. Rejects unknown zones, non-numeric
        values and anything outside [SETPOINT_MIN_C, SETPOINT_MAX_C]."""
        if zone not in ZONE_BY_ID:
            return self._log("setpoint", zone, celsius, False, "unknown zone")
        if celsius is None:
            self.setpoints[zone] = None
            return self._log("setpoint", zone, None, True, "hvac off")
        try:
            v = float(celsius)
        except (TypeError, ValueError):
            return self._log("setpoint", zone, celsius, False, "not a number")
        if not (SETPOINT_MIN_C <= v <= SETPOINT_MAX_C):
            return self._log("setpoint", zone, v, False,
                             f"outside {SETPOINT_MIN_C}-{SETPOINT_MAX_C} degC")
        self.setpoints[zone] = v
        return self._log("setpoint", zone, v, True, "")

    def write_vent(self, zone: str, level: int) -> bool:
        """See HVACAdapter.write_vent. Rejects unknown zones and levels outside
        VENT_LEVELS (the twin's FAN_W table has no entry for anything else, so
        an unvalidated write would KeyError deep inside the physics)."""
        if zone not in ZONE_BY_ID:
            return self._log("vent", zone, level, False, "unknown zone")
        try:
            v = int(level)
        except (TypeError, ValueError):
            return self._log("vent", zone, level, False, "not an integer")
        if v not in VENT_LEVELS:
            return self._log("vent", zone, v, False, f"not in {VENT_LEVELS}")
        self.vents[zone] = v
        return self._log("vent", zone, v, True, "")

    def capabilities(self) -> dict:
        """See HVACAdapter.capabilities. protocol == "simulation" is the honest
        marker that no bus is attached."""
        return {
            "protocol": "simulation",
            "vendor": "sim.twin.DigitalTwin",
            "writable": True,
            "zones": list(ZONE_IDS),
            "setpoint_range_c": [SETPOINT_MIN_C, SETPOINT_MAX_C],
            "vent_levels": list(VENT_LEVELS),
            "supports_heating": False,      # cooling-only plant, see twin.step
            "per_zone": {z.id: {"name": z.name, "area_m2": z.area,
                                "orientation": z.orientation,
                                "max_cool_w": z.max_cool,
                                "capacity_w": round(z.max_cool * self.twin.capacity_scale, 1)}
                         for z in ZONES},
        }

    # -- sim-only extras --------------------------------------------------
    def pending(self) -> tuple:
        """The command buffer, in the shape twin.step() wants.

        INPUT: none.  OUTPUT: (setpoints dict, vents dict) — copies, so the
        caller cannot mutate the buffer by accident.  SIDE EFFECTS: none.
        ERROR STATES: none."""
        return dict(self.setpoints), {z: int(self.vents.get(z, 0)) for z in ZONE_IDS}

    def commit(self, dt: float = DT) -> dict:
        """Run the plant one step against the current command buffer.

        INPUT: dt seconds.  OUTPUT: twin.step()'s dict.  SIDE EFFECTS: advances
        the twin — this is the only method here that changes physical state.
        ERROR STATES: whatever twin.step raises (nothing, for valid buffers)."""
        sps, vents = self.pending()
        return self.twin.step(sps, vents, dt)

    def _log(self, kind: str, zone: str, value, ok: bool, reason: str) -> bool:
        self.writes.append({"t": getattr(self.twin, "t", 0.0), "kind": kind,
                            "zone": zone, "value": value, "ok": ok, "reason": reason})
        if len(self.writes) > self.max_writes:
            del self.writes[:-self.max_writes]
        return ok


class SimOccupancyAdapter:
    """OccupancyAdapter backed by the twin's schedule model (+ its occ_scale)."""

    def __init__(self, twin):
        """INPUT: twin.  OUTPUT: adapter.  SIDE EFFECTS: none.
        ERROR STATES: none."""
        self.twin = twin

    def occupancy(self, zone: str) -> int:
        """See OccupancyAdapter.occupancy."""
        return int(self.twin.occupancy_now(zone))

    def occupancy_pct(self, zone: str) -> float:
        """See OccupancyAdapter.occupancy_pct. 100% = the zone's weekday peak."""
        return float(self.twin.zone_snapshot(zone)["occ_pct"])

    def forecast(self, zone: str, ahead_h: float) -> int:
        """Sim-only: scheduled occupancy ahead_h hours from now (pre-cool logic).

        A real deployment replaces this with a room-booking feed; there is no
        general way to forecast a PIR sensor.
        INPUT: zone id, ahead_h hours.  OUTPUT: int >= 0.  SIDE EFFECTS: none.
        ERROR STATES: KeyError for an unknown zone."""
        return int(self.twin.occupancy_now(zone, ahead_h=ahead_h))


class SimWeatherAdapter:
    """WeatherAdapter backed by sim.weather + sim.humidity, including the twin's
    live what-if offsets, so a heatwave scenario is visible through the seam."""

    def __init__(self, twin):
        """INPUT: twin.  OUTPUT: adapter.  SIDE EFFECTS: none.
        ERROR STATES: none."""
        self.twin = twin

    def outdoor(self) -> dict:
        """See WeatherAdapter.outdoor.

        solar_factor is reported as the clear-sky DAYLIGHT ENVELOPE (0 at night,
        1 at solar noon), which is orientation-independent; the per-facade
        factors the physics actually uses are in solar_by_orientation, because a
        single scalar cannot describe five differently-facing walls.
        """
        tw = self.twin
        t_out = tw.weather_fn(tw.t) + tw.outdoor_offset
        rh = min(100.0, max(0.0, outdoor_rh(tw.t, tw.seed) + tw.humidity_offset))
        h = tw.hour
        # solar_factor("N", h) is 0.25 * the daylight envelope by construction
        # (sim.weather.solar_factor), so /0.25 recovers the envelope itself.
        envelope = solar_factor("N", h) / 0.25
        return {
            "temp_c": round(t_out, 2),
            "rh_pct": round(rh, 1),
            "solar_factor": round(envelope * tw.solar_scale, 3),
            "dew_point_c": round(dew_point(t_out, rh), 2),
            "hour": round(h, 2),
            "solar_by_orientation": {o: round(solar_factor(o, h) * tw.solar_scale, 3)
                                     for o in ("N", "E", "S", "W")},
            "source": "simulation",
        }


class SimSensorAdapter:
    """SensorAdapter backed by the twin, with a fault-injection hook.

    CO2 is not a twin state variable. It is ESTIMATED here from the ASHRAE 62.1
    steady-state mass balance, Cs = Co + 1e6 * N * G / Q, using the SAME outdoor
    airflow the twin's moisture balance uses (ventilation VENT_UA*vent/CP_AIR
    plus INFIL_ACH infiltration), so the number moves with the real control
    action rather than being decorative. Every reading is labelled
    co2_estimated=True. Two honest caveats: the balance is steady-state, so it
    jumps instantly instead of ramping over a time constant; and the twin's
    ventilation is sized thermally, not for indoor air quality, so a full
    conference room reads high (>1500 ppm) — that is what 0.3 ACH plus a small
    fan actually does, not a modelling error.

    Fault injection exists so the maintenance monitor can be tested against
    something. It perturbs the reading for real; health() does NOT read the
    injected flag, it infers faults from the reading history alone, so a
    detector cannot cheat by asking the adapter what is broken.
    """

    FAULT_KINDS = ("none", "stuck", "offset", "dropout")

    def __init__(self, twin, estimate_co2: bool = True, history: int = 16,
                 stale_after_s: float = 900.0):
        """INPUT: twin; estimate_co2 (False -> co2_ppm is always None, the
        honest reading for a building with no CO2 points); history (readings
        kept per zone for health inference); stale_after_s (sim-seconds before
        a point counts as stale).
        OUTPUT: adapter.  SIDE EFFECTS: none.  ERROR STATES: none."""
        self.twin = twin
        self.estimate_co2 = bool(estimate_co2)
        self.history = int(history)
        self.stale_after_s = float(stale_after_s)
        self._log: dict = {z: [] for z in ZONE_IDS}
        self._faults: dict = {}

    # -- protocol ---------------------------------------------------------
    def read(self, zone: str) -> dict:
        """See SensorAdapter.read. Also returns co2_estimated and t."""
        snap = self.twin.zone_snapshot(zone)      # raises KeyError if unknown
        temp, rh = snap["temp_c"], snap["rh_pct"]
        fault = self._faults.get(zone)
        if fault:
            kind, val = fault["kind"], fault["value"]
            if kind == "dropout":
                temp = rh = None
            elif kind == "offset":
                temp = round(temp + val, 2)
            elif kind == "stuck":
                temp = fault.setdefault("held", temp)
        out = {
            "zone": zone,
            "temp_c": temp,
            "rh_pct": rh,
            "co2_ppm": self._co2(zone) if (self.estimate_co2 and temp is not None) else None,
            "co2_estimated": self.estimate_co2,
            "t": self.twin.t,
        }
        log = self._log.setdefault(zone, [])
        log.append((self.twin.t, temp, rh))
        if len(log) > self.history:
            del log[:-self.history]
        return out

    def health(self, zone: str) -> dict:
        """See SensorAdapter.health. Faults are inferred, never echoed.

        Detects: no_data (never read), dropout (last read returned nothing, or
        the last read is older than stale_after_s), stuck (the full history
        window is bit-identical — a live zone always drifts), out_of_range
        (temp outside -20..70 degC or RH outside 0..100), impossible (dew point
        above dry bulb, which no real air can do and which catches a mismatched
        temp/RH sensor pair).
        """
        if zone not in ZONE_BY_ID:
            raise KeyError(zone)
        log = self._log.get(zone, [])
        now = self.twin.t
        faults: list = []
        if not log:
            return {"zone": zone, "ok": False, "faults": ["no_data"], "reads": 0,
                    "last_read_t": None, "stale_s": None}
        last_t, last_temp, last_rh = log[-1]
        stale = now - last_t
        if last_temp is None or last_rh is None:
            faults.append("dropout")
        elif stale > self.stale_after_s:
            faults.append("stale")
        if last_temp is not None and not (-20.0 <= last_temp <= 70.0):
            faults.append("out_of_range")
        if last_rh is not None and not (0.0 <= last_rh <= 100.0):
            faults.append("out_of_range")
        temps = [x for _, x, _ in log if x is not None]
        if len(temps) >= max(4, self.history // 2) and len(set(temps)) == 1:
            faults.append("stuck")
        if last_temp is not None and last_rh is not None:
            if dew_point(last_temp, last_rh) > last_temp + 0.05:
                faults.append("impossible")
        return {"zone": zone, "ok": not faults, "faults": faults, "reads": len(log),
                "last_read_t": last_t, "stale_s": round(stale, 1)}

    # -- sim-only extras --------------------------------------------------
    def inject_fault(self, zone: str, kind: str = "none", value: float = 0.0) -> bool:
        """Break a sensor on purpose, to test a fault detector.

        INPUT: zone id; kind in FAULT_KINDS ("stuck" freezes the temp at its
          next reading, "offset" adds `value` degC, "dropout" returns None);
          value degC for "offset".
        OUTPUT: True if applied, False for an unknown zone or kind.
        SIDE EFFECTS: changes what read() returns from now on. "none" clears.
        ERROR STATES: none.
        """
        if zone not in ZONE_BY_ID or kind not in self.FAULT_KINDS:
            return False
        if kind == "none":
            self._faults.pop(zone, None)
        else:
            self._faults[zone] = {"kind": kind, "value": float(value)}
        return True

    def injected_faults(self) -> dict:
        """Ground truth for tests ONLY — deliberately absent from health().

        INPUT: none.  OUTPUT: dict zone -> {kind, value}.  SIDE EFFECTS: none.
        ERROR STATES: none."""
        return {z: dict(f) for z, f in self._faults.items()}

    def _co2(self, zone: str) -> float:
        """ASHRAE 62.1 steady-state: Cs = Co + 1e6 * N*G / Q, all flows in L/s."""
        z = ZONE_BY_ID[zone]
        vent = int(self.twin.last_vents.get(zone, 0))
        m_vent = VENT_UA * vent / CP_AIR                                # kg/s
        m_inf = INFIL_ACH * (z.area * CEILING_H) * RHO_AIR / 3600.0     # kg/s
        q_ls = (m_vent + m_inf) / RHO_AIR * 1000.0                      # L/s
        n = self.twin.occupancy_now(zone)
        if q_ls <= 0.0:
            return CO2_MAX_PPM
        ppm = CO2_OUTDOOR_PPM + 1e6 * (CO2_PER_PERSON_LS * n) / q_ls
        return round(min(ppm, CO2_MAX_PPM), 0)


class LogNotificationAdapter:
    """NotificationAdapter that records to an in-memory list instead of sending.

    This is what a Slack webhook implementation replaces. PII is scrubbed before
    the message is stored (scrub=True by default) because notifications are the
    one path where occupant text is meant to leave the system, and a redaction
    that only runs on the dashboard is not a redaction.
    """

    def __init__(self, scrub: bool = True, maxlen: int = 200):
        """INPUT: scrub (run backend.privacy.scrub_pii over text), maxlen (ring
        size).  OUTPUT: adapter.  SIDE EFFECTS: none.  ERROR STATES: none."""
        self.scrub = bool(scrub)
        self.maxlen = int(maxlen)
        self.records: list = []

    def notify(self, channel: str, text: str, meta: dict | None = None) -> bool:
        """See NotificationAdapter.notify. Returns False only for empty text or
        an empty channel — a message with no destination is a bug, not an event."""
        channel, text = str(channel or ""), str(text or "")
        if not channel.strip() or not text.strip():
            return False
        redacted: list = []
        if self.scrub:
            text, redacted = scrub_pii(text)
        self.records.append({"channel": channel, "text": text,
                             "redacted": redacted, "meta": dict(meta or {})})
        if len(self.records) > self.maxlen:
            del self.records[:-self.maxlen]
        return True

    def by_channel(self, channel: str) -> list:
        """Everything sent to one channel.
        INPUT: channel.  OUTPUT: list of record dicts.  SIDE EFFECTS: none.
        ERROR STATES: none."""
        return [r for r in self.records if r["channel"] == channel]

    def clear(self) -> int:
        """Drop the log.
        INPUT: none.  OUTPUT: how many records were dropped.
        SIDE EFFECTS: empties .records.  ERROR STATES: none."""
        n = len(self.records)
        self.records.clear()
        return n


# ==========================================================================
# Registry
# ==========================================================================

class AdapterRegistry:
    """Name -> implementation, so a real driver drops in with zero edits above.

    An entry is either a ready instance or a factory called with the twin. That
    distinction is what lets a BACnet adapter (constructed once from site
    config, no twin involved) and a Sim adapter (constructed per twin) live in
    the same table.
    """

    def __init__(self, seed: dict | None = None):
        """INPUT: seed — optional {name: impl_or_factory} to start from.
        OUTPUT: registry.  SIDE EFFECTS: none.  ERROR STATES: none."""
        self._impls: dict = dict(seed or {})

    def register(self, name: str, impl) -> None:
        """Bind a name to an implementation or a factory(twin) -> implementation.

        INPUT: name (one of PROTOCOLS' keys by convention, but any string is
          allowed so a site can add "meter" or "lighting"), impl (instance or
          callable).
        OUTPUT: None.
        SIDE EFFECTS: replaces any existing binding for that name.
        ERROR STATES: ValueError for an empty name; TypeError if impl is None.
        """
        if not name:
            raise ValueError("adapter name must be a non-empty string")
        if impl is None:
            raise TypeError("impl must be an instance or a factory, not None")
        self._impls[name] = impl

    def unregister(self, name: str) -> bool:
        """Remove a binding.
        INPUT: name.  OUTPUT: True if it existed.  SIDE EFFECTS: mutates the
        registry.  ERROR STATES: none."""
        return self._impls.pop(name, None) is not None

    def names(self) -> list:
        """Registered names, sorted for deterministic output.
        INPUT: none.  OUTPUT: list[str].  SIDE EFFECTS: none.
        ERROR STATES: none."""
        return sorted(self._impls)

    def build(self, twin) -> dict:
        """Instantiate every registered adapter for one twin.

        INPUT: twin (passed to any factory entries; ignored by instances).
        OUTPUT: dict name -> live adapter.
        SIDE EFFECTS: none on the registry; factories may do their own setup.
        ERROR STATES: propagates whatever a factory raises — a driver that
          cannot start must fail loudly at wiring time, not at the first write.
        """
        return {name: (impl(twin) if _is_factory(impl) else impl)
                for name, impl in self._impls.items()}


def _is_factory(impl) -> bool:
    """True when impl looks like a factory(twin) rather than a ready adapter.

    A class or a plain function is a factory; anything already carrying one of
    the protocol methods is an instance.
    INPUT: any object.  OUTPUT: bool.  SIDE EFFECTS: none.  ERROR STATES: none.
    """
    if isinstance(impl, type) or inspect.isfunction(impl):
        return True
    return not any(hasattr(impl, m) for m in
                   ("read_state", "occupancy", "outdoor", "read", "notify"))


DEFAULT_REGISTRY = AdapterRegistry({
    "hvac": SimHVACAdapter,
    "occupancy": SimOccupancyAdapter,
    "weather": SimWeatherAdapter,
    "sensor": SimSensorAdapter,
    "notify": lambda _twin: LogNotificationAdapter(),
})


def get_adapters(twin, registry: AdapterRegistry | None = None) -> dict:
    """The live adapter set for one twin. The single call the app needs.

    INPUT: twin (DigitalTwin); registry (defaults to DEFAULT_REGISTRY, so a
      deployment swaps drivers by registering over the same names before this
      is called).
    OUTPUT: dict with keys hvac, occupancy, weather, sensor, notify (plus any
      extra name the site registered), each a live adapter instance.
    SIDE EFFECTS: constructs adapters; SimHVACAdapter seeds its command buffer
      from twin.last_setpoints / twin.last_vents.
    ERROR STATES: propagates constructor errors from a registered factory.
    """
    return (registry or DEFAULT_REGISTRY).build(twin)


# ==========================================================================
# Conformance
# ==========================================================================

def protocol_methods(protocol) -> list:
    """Public method names a Protocol requires, sorted.

    INPUT: a typing.Protocol subclass.
    OUTPUT: list[str]. Walks the MRO so an extended protocol reports inherited
      methods too, and skips typing/object machinery.
    SIDE EFFECTS: none.
    ERROR STATES: TypeError if protocol has no __mro__.
    """
    skip = {"__mro__"}
    names: set = set()
    for klass in protocol.__mro__:
        if klass.__module__ in ("builtins", "typing"):
            continue
        for n, v in vars(klass).items():
            if not n.startswith("_") and n not in skip and callable(v):
                names.add(n)
    return sorted(names)


def _arity(fn, drop_self: bool) -> tuple:
    """(min positional args, max positional args) a callable accepts. max is
    None for *args. Bound methods have already dropped self."""
    params = list(inspect.signature(fn).parameters.values())
    if drop_self and params and params[0].name in ("self", "cls"):
        params = params[1:]
    positional = [p for p in params if p.kind in
                  (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return sum(1 for p in positional if p.default is p.empty), None
    return (sum(1 for p in positional if p.default is p.empty), len(positional))


def assert_conforms(impl, protocol) -> bool:
    """Validate an adapter against a Protocol before it is trusted with a plant.

    Stronger than isinstance() against a runtime_checkable Protocol, which only
    checks that the names exist: this also checks each method is callable and
    that its signature can actually be CALLED the way the protocol declares
    (impl may take fewer required args or more optional ones, never more
    required ones). That is the failure a real driver hits — a write_setpoint
    that quietly needs a priority argument.

    INPUT: impl (instance or class), protocol (typing.Protocol subclass).
    OUTPUT: True. Never returns False — a failure is an exception, so this is
      usable as a bare assertion in a wiring path.
    SIDE EFFECTS: none.
    ERROR STATES: TypeError listing EVERY problem found (missing methods, non-
      callables, arity mismatches) rather than only the first.
    """
    problems: list = []
    is_class = isinstance(impl, type)
    for name in protocol_methods(protocol):
        want = getattr(protocol, name)
        got = getattr(impl, name, None)
        if got is None:
            problems.append(f"missing method {name}()")
            continue
        if not callable(got):
            problems.append(f"{name} is {type(got).__name__}, not callable")
            continue
        try:
            need_min, need_max = _arity(want, True)
            have_min, have_max = _arity(got, is_class or inspect.isfunction(got))
        except (TypeError, ValueError):
            continue                      # builtins etc: presence is all we can check
        if have_min > need_min:
            problems.append(f"{name}() requires {have_min} args, protocol passes {need_min}")
        elif have_max is not None and have_max < need_min:
            problems.append(f"{name}() accepts at most {have_max} args, protocol passes {need_min}")
    if problems:
        raise TypeError(f"{type(impl).__name__ if not is_class else impl.__name__} does not "
                        f"conform to {protocol.__name__}: " + "; ".join(problems))
    return True

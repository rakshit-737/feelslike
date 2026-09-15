"""Forward a wired sensor node's USB-serial lines to the gateway.

    python -m scripts.serial_bridge                          # auto-detect the Uno
    python -m scripts.serial_bridge --port COM10
    python -m scripts.serial_bridge --gateway http://10.142.122.43:8000

WHAT THIS IS. The server side of the Uno ambient node
(hardware/firmware/ambient_node_uno). The Uno prints one JSON object per line;
this script reads each line and POSTs it to /api/hw/sensor. It is a transport
adapter and nothing else: it does not smooth, correct or invent values, it tags
each reading transport="usb-serial", and it drops lines it cannot parse rather
than guessing.

Why a script and not the server: the gateway stays free of serial-port code and
of pyserial, which is hardware tooling only (requirements-hardware.txt).

Opening the port resets the Uno, so the first reading arrives a few seconds
after start. Only one program can hold a serial port: close Arduino IDE's
Serial Monitor first.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

BAUD = 115200
TRANSPORT = "usb-serial"
# USB vendor ids of genuine Arduino boards and common CH340 clones. The team's
# ESP32 enumerates as a Silicon Labs CP210x (0x10C4) and is listed as excluded,
# so the bridge never grabs the rig's port by accident.
ARDUINO_VIDS = {0x2341: "Arduino LLC", 0x2A03: "Arduino SRL", 0x1A86: "WCH CH340 clone"}
EXCLUDED_VIDS = {0x10C4: "Silicon Labs CP210x (the ESP32 rig)"}


def parse_line(raw) -> dict | None:
    """One serial line -> a /api/hw/sensor payload, or None to skip it.

    INPUT: bytes or str as read from the port (may carry a CR/LF or noise).
    OUTPUT: the node's fields plus transport="usb-serial" and a default role of
      "ambient"; None for blank lines, '#' log lines, non-JSON, non-objects, or
      objects without a node_id.
    SIDE EFFECTS: none. ERROR STATES: none - a bad line is skipped, never raised.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    line = str(raw).strip()
    if not line or line.startswith("#"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not str(obj.get("node_id") or "").strip():
        return None
    out = dict(obj)
    out.setdefault("role", "ambient")
    out["transport"] = TRANSPORT
    return out


def find_port() -> str:
    """The single Arduino-looking serial port, else SystemExit with a listing."""
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    hits = [p for p in ports if p.vid in ARDUINO_VIDS]
    if len(hits) == 1:
        return hits[0].device
    listing = "\n".join(
        f"  {p.device}: {p.description}"
        + (f"  [excluded: {EXCLUDED_VIDS[p.vid]}]" if p.vid in EXCLUDED_VIDS else "")
        for p in ports) or "  (no serial ports)"
    if not hits:
        raise SystemExit("No Arduino found on USB. Plug the Uno in, or pass --port.\n" + listing)
    raise SystemExit("More than one Arduino-like port; pass --port.\n" + listing)


def post(gateway: str, payload: dict, timeout: float = 3.0) -> tuple:
    """POST one payload. OUTPUT: (HTTP status or None, short detail)."""
    req = urllib.request.Request(gateway.rstrip("/") + "/api/hw/sensor",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:160]
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)[:160]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=None, help="serial port (default: auto-detect the Uno)")
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = forever)")
    args = ap.parse_args()

    import serial  # hardware tooling; imported late so parse_line is testable without it

    port = args.port or find_port()
    print(f"[boot] bridging {port} @ {BAUD} -> {args.gateway}/api/hw/sensor  (Ctrl+C to stop)")
    t0 = time.time()
    lines = sent = skipped = failed = faults = 0
    ser = None
    try:
        while not args.seconds or time.time() - t0 < args.seconds:
            if ser is None:
                try:
                    ser = serial.Serial(port, BAUD, timeout=3)
                except serial.SerialException as e:
                    print(f"[port] cannot open {port}: {e} - retrying in 3 s (Serial Monitor open?)")
                    time.sleep(3)
                    continue
            try:
                raw = ser.readline()
            except serial.SerialException as e:
                print(f"[port] lost {port}: {e} - reopening")
                ser.close()
                ser = None
                continue
            if not raw:
                continue
            lines += 1
            payload = parse_line(raw)
            if payload is None:
                skipped += 1
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("#"):
                    print(f"[node] {text}")
                continue
            if payload.get("fault"):
                faults += 1
                print(f"[node] FAULT from {payload['node_id']}: {payload['fault']} "
                      f"(counts={payload.get('counts')})")
            code, detail = post(args.gateway, payload)
            if code == 200:
                sent += 1
                if sent % 15 == 1:
                    print(f"[sent] {payload['node_id']} temp_c={payload.get('temp_c')} "
                          f"seq={payload.get('seq')}")
            else:
                failed += 1
                print(f"[post] HTTP {code}: {detail}")
    except KeyboardInterrupt:
        pass
    finally:
        if ser is not None:
            ser.close()
    print(f"[done] {lines} lines, {sent} posted, {faults} fault reports, "
          f"{skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()

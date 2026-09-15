"""External data: a REAL live weather/air-quality feed and a HISTORICAL dataset.

Both exist so the Monitor tab can put real measurements next to the twin's
simulated ones, each labelled with its provenance. Neither one feeds the
physics: the twin keeps its seeded weather (sim/weather.py) so the frozen A/B
numbers and determinism are untouched. This is display + validation data.

1. LIVE FEED — Open-Meteo (https://open-meteo.com, CC-BY 4.0, no API key).
   Forecast API:    current + hourly dry-bulb / RH for the configured site,
                    past 7 days and next 24 h in one call.
   Air-quality API: current + hourly PM2.5 / PM10 / European AQI (CAMS model).
   Site defaults to VIT Chennai (12.84 N, 80.15 E); override with FL_LAT /
   FL_LON / FL_SITE env vars. Refreshed on a wall-clock background thread every
   REFRESH_S; a failed fetch keeps the last good payload and reports the error
   in "error" with available=false when nothing was ever fetched. httpx is
   already a runtime dependency (requirements.txt) — no new packages.
   HONESTY: hourly "forecast" rows past now are Open-Meteo's meteorological
   forecast, tagged kind="forecast"; rows before now are its reanalysis /
   observation blend, tagged kind="past". PM values come from the CAMS
   atmospheric model, not a ground monitor — the payload says so.

2. HISTORICAL DATASET — UCI "Occupancy Detection" (Candanedo & Feldheim 2016,
   CC-BY 4.0): 20 560 one-minute rows from an office in Mons, Belgium, Feb 2015:
   temperature, RH, light (lux), CO2 (ppm), humidity ratio, occupancy (0/1).
   Stored as data/uci_occupancy.csv (merged, chronological; see data/README.md
   for provenance and the fetch script). Served with the same window /
   downsample semantics as the sim telemetry so the two overlay cleanly, and
   it is the validation set for the CO2 estimator in backend/telemetry.py.

Stdlib + httpx. Thread-safe; readers get copies.
"""
from __future__ import annotations

import csv
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import httpx
except Exception:  # pragma: no cover - httpx is a runtime dep, but degrade loudly
    httpx = None

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATASET_FILE = DATA_DIR / "uci_occupancy.csv"

SITE_LAT = float(os.environ.get("FL_LAT", "12.84"))
SITE_LON = float(os.environ.get("FL_LON", "80.15"))
SITE_NAME = os.environ.get("FL_SITE", "VIT Chennai (Kelambakkam)")
REFRESH_S = 600.0            # Open-Meteo updates hourly; 10 min is polite
RETRY_S = 30.0               # after a failed fetch (offline, cold DNS, hotspot)
TIMEOUT_S = 8.0
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ATTRIBUTION = "Weather & air-quality data by Open-Meteo.com (CC BY 4.0)"

DATASET_WINDOWS = {"1h": 60, "6h": 360, "24h": 1440, "7d": 7 * 1440, "live": 10}


class ExternalFeed:
    """Cached Open-Meteo reader with a background refresher.

    INPUT: start() once (idempotent); refresh() for a synchronous fetch.
    OUTPUT: snapshot() — json-safe copy with available/error/fetched_at.
    SIDE EFFECTS: one daemon thread; outbound HTTPS to Open-Meteo only.
    ERROR STATES: never raises to callers; failures land in snapshot()["error"].
    """

    def __init__(self, lat: float = SITE_LAT, lon: float = SITE_LON,
                 site: str = SITE_NAME, enabled: bool | None = None):
        self.lat, self.lon, self.site = float(lat), float(lon), str(site)
        env = os.environ.get("FL_EXTERNAL", "1").strip().lower()
        self.enabled = (env not in ("0", "false", "off")) if enabled is None else bool(enabled)
        self._lock = threading.Lock()
        self._data: dict = {}
        self._error: str = ""
        self._fetched_at: float | None = None
        self._attempts = 0
        self._thread = None

    def start(self) -> None:
        if self._thread or not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="fl-external")
        self._thread.start()

    def _loop(self) -> None:
        while True:
            ok = self.refresh()
            time.sleep(REFRESH_S if ok else RETRY_S)   # a cold start retries soon

    def refresh(self) -> bool:
        """One synchronous fetch of both endpoints. Returns True on success."""
        if httpx is None:
            self._set_error("httpx not installed")
            return False
        self._attempts += 1
        try:
            with httpx.Client(timeout=TIMEOUT_S) as c:
                w = c.get(WEATHER_URL, params={
                    "latitude": self.lat, "longitude": self.lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
                    "hourly": "temperature_2m,relative_humidity_2m",
                    "past_days": 7, "forecast_days": 2, "timezone": "auto"}).json()
                a = c.get(AQ_URL, params={
                    "latitude": self.lat, "longitude": self.lon,
                    "current": "pm2_5,pm10,european_aqi,carbon_dioxide",
                    "hourly": "pm2_5,pm10,european_aqi",
                    "past_days": 7, "forecast_days": 1, "timezone": "auto"}).json()
            if "current" not in w or "current" not in a:
                raise ValueError("unexpected Open-Meteo payload shape")
            with self._lock:
                self._data = {"weather": w, "air": a}
                self._error = ""
                self._fetched_at = time.time()
            return True
        except Exception as e:  # noqa: BLE001 - network firewall
            self._set_error(f"{type(e).__name__}: {e}")
            return False

    def _set_error(self, msg: str) -> None:
        with self._lock:
            self._error = msg

    # ------------------------------------------------------------ readers
    def snapshot(self, hours_past: float = 168.0, hours_ahead: float = 24.0) -> dict:
        """The /api/external payload. Copies only."""
        with self._lock:
            data = dict(self._data)
            err, at, n = self._error, self._fetched_at, self._attempts
        out = {
            "source": "real", "provider": "open-meteo", "attribution": ATTRIBUTION,
            "site": {"name": self.site, "lat": self.lat, "lon": self.lon},
            "enabled": self.enabled, "available": bool(data),
            "fetched_at": at, "age_s": None if at is None else round(time.time() - at, 1),
            "refresh_s": REFRESH_S, "attempts": n, "error": err or None,
            "current": None, "hourly": [], "air_hourly": [],
            "note": ("Outdoor conditions at the configured site, fetched live over HTTPS. "
                     "Displayed beside the twin — the simulation keeps its own seeded weather. "
                     "PM / AQI values are CAMS model output, not a ground monitor."),
        }
        if not data:
            if not self.enabled:
                out["error"] = "disabled (FL_EXTERNAL=0)"
            return out
        w, a = data["weather"], data["air"]
        wc, ac = w.get("current", {}), a.get("current", {})
        out["current"] = {
            "time": wc.get("time"), "t_out": wc.get("temperature_2m"),
            "rh_out": wc.get("relative_humidity_2m"),
            "apparent_c": wc.get("apparent_temperature"), "weather_code": wc.get("weather_code"),
            "pm2_5": ac.get("pm2_5"), "pm10": ac.get("pm10"), "aqi": ac.get("european_aqi"),
            "co2_ppm": ac.get("carbon_dioxide"), "air_time": ac.get("time"),
            "timezone": w.get("timezone"),
        }
        now_iso = wc.get("time") or ""
        def rows(block, keys, rename):
            h = block.get("hourly", {})
            times = h.get("time", [])
            res = []
            for i, ts in enumerate(times):
                r = {"time": ts, "kind": "past" if ts <= now_iso else "forecast"}
                for k in keys:
                    v = (h.get(k) or [None] * len(times))[i]
                    r[rename.get(k, k)] = v
                res.append(r)
            return res
        out["hourly"] = _trim(rows(w, ["temperature_2m", "relative_humidity_2m"],
                                   {"temperature_2m": "t_out", "relative_humidity_2m": "rh_out"}),
                              now_iso, hours_past, hours_ahead)
        out["air_hourly"] = _trim(rows(a, ["pm2_5", "pm10", "european_aqi"],
                                       {"european_aqi": "aqi"}), now_iso, hours_past, hours_ahead)
        return out


def _trim(rows: list, now_iso: str, hours_past: float, hours_ahead: float) -> list:
    if not rows or not now_iso:
        return rows
    try:
        now = datetime.fromisoformat(now_iso)
    except ValueError:
        return rows
    lo, hi = now - timedelta(hours=hours_past), now + timedelta(hours=hours_ahead)
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["time"])
        except ValueError:
            continue
        if lo <= ts <= hi:
            out.append(r)
    return out


# ============================================================ dataset
class Dataset:
    """The UCI occupancy CSV, loaded once, served by window. Read-only."""

    COLS = ("temp_c", "rh_pct", "light_lux", "co2_ppm", "humidity_ratio", "occupied")

    def __init__(self, path: Path = DATASET_FILE):
        self.path = Path(path)
        self.rows: list = []
        self.error: str = ""
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open(newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        self.rows.append({
                            "time": r["timestamp"],
                            "temp_c": float(r["temp_c"]), "rh_pct": float(r["rh_pct"]),
                            "light_lux": float(r["light_lux"]), "co2_ppm": float(r["co2_ppm"]),
                            "humidity_ratio": float(r["humidity_ratio"]),
                            "occupied": int(float(r["occupied"])), "split": r.get("split", ""),
                        })
                    except (KeyError, ValueError):
                        continue
        except OSError as e:
            self.error = f"{type(e).__name__}: {e}"

    @property
    def available(self) -> bool:
        return bool(self.rows)

    def info(self) -> dict:
        return {
            "source": "historical", "name": "UCI Occupancy Detection (Candanedo & Feldheim, 2016)",
            "license": "CC BY 4.0", "url": "https://archive.ics.uci.edu/dataset/357/occupancy+detection",
            "path": str(self.path.relative_to(ROOT)) if self.path.is_relative_to(ROOT) else str(self.path),
            "available": self.available, "rows": len(self.rows), "error": self.error or None,
            "start": self.rows[0]["time"] if self.rows else None,
            "end": self.rows[-1]["time"] if self.rows else None,
            "cadence_s": 60, "columns": list(self.COLS),
            "note": ("One office room in Mons, Belgium, Feb 2015 — 1-minute readings of "
                     "temperature, RH, light, CO2 and a ground-truth occupancy label. "
                     "Used as the historical reference and as the validation set for the "
                     "CO2 estimator. It is NOT this building."),
        }

    def window(self, window: str = "24h", end: str | None = None,
               start: str | None = None, max_points: int = 360) -> dict:
        """Rows for a window ending at `end` (ISO, default: dataset end).

        INPUT: window key from DATASET_WINDOWS or "custom" with start/end.
        OUTPUT: {"points":[{time,temp_c,rh_pct,light_lux,co2_ppm,occupied}...],
                 "t_from","t_to","count_raw","source":"historical"}.
        """
        if not self.rows:
            return {"points": [], "t_from": None, "t_to": None, "count_raw": 0,
                    "source": "historical", "window": window}
        times = [r["time"] for r in self.rows]
        import bisect as _b
        hi = len(self.rows) if not end else _b.bisect_right(times, end)
        if window == "custom" and start:
            lo = _b.bisect_left(times, start)
        else:
            lo = max(0, hi - DATASET_WINDOWS.get(window, 1440))
        chunk = self.rows[lo:hi]
        n = len(chunk)
        pts = chunk
        if n > max_points:
            pts = []
            for b in range(max_points):
                a_, z_ = (b * n) // max_points, ((b + 1) * n) // max_points
                if z_ <= a_:
                    continue
                c = chunk[a_:z_]
                agg = {"time": c[-1]["time"], "n": len(c),
                       "occupied": round(sum(x["occupied"] for x in c) / len(c), 2)}
                for k in ("temp_c", "rh_pct", "light_lux", "co2_ppm"):
                    agg[k] = round(sum(x[k] for x in c) / len(c), 2)
                pts.append(agg)
        return {"points": [{k: v for k, v in p.items() if k not in ("humidity_ratio", "split")}
                           for p in pts],
                "t_from": chunk[0]["time"] if chunk else None,
                "t_to": chunk[-1]["time"] if chunk else None,
                "count_raw": n, "source": "historical", "window": window}

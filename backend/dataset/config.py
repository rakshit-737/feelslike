"""Central configuration for the historical dataset. Nothing else hard-codes these.

Precedence: dataclass defaults  <  data/dataset/config.json (if present)  <  CLI flags.
`public_dict()` is what the API may show: no file paths, no seeds of other users' runs
are hidden (the seed is part of reproducibility and is shown on purpose).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "dataset"
CONFIG_FILE = DATA_DIR / "config.json"
DEFAULT_DB = DATA_DIR / "feelslike_history.sqlite"

SEASONS = ("summer", "monsoon", "winter", "transition")
CLIMATES = ("delhi", "chennai")


@dataclass
class BuildingSpec:
    building_id: str
    building_type: str            # a backend.building.PROFILES key
    floors: int = 1               # each floor = one 5-zone twin floor plate
    name: str | None = None       # default: the profile's name
    pv_kwp: float = 0.0           # rooftop PV, kW peak (0 = none)


@dataclass
class QualityConfig:
    noise_enabled: bool = True
    temp_noise_c: float = 0.15
    rh_noise_pct: float = 1.5
    co2_noise_ppm: float = 15.0
    power_noise_frac: float = 0.02
    missing_data_rate: float = 0.005     # per reading
    outlier_rate: float = 0.001          # per reading
    drift_enabled: bool = True
    temp_drift_c_per_day_max: float = 0.02
    delay_rate: float = 0.002            # reading repeats the previous sample
    max_interpolate_gap: int = 3         # samples; longer gaps stay missing


@dataclass
class AnomalyConfig:
    enabled: bool = True
    rate_per_building_day: float = 0.08  # expected anomalies per building per day
    types: tuple = ("occupancy_spike", "hvac_failure", "temp_sensor_fault", "co2_spike",
                    "abnormal_energy", "communication_gap", "weather_disturbance")


@dataclass
class DatasetConfig:
    start: str = "2026-05-18"            # DATA_START (local date, 00:00)
    days: int = 42                       # DATA_END = start + days
    step_min: int = 5                    # TIME_STEP (sample interval); physics runs at 60 s
    seed: int = 42                       # RANDOM_SEED
    season: str = "auto"                 # auto (from month) or a fixed SEASONS key
    climate: str = "delhi"
    latitude: float = 28.61
    noise_level: float = 1.0             # NOISE_LEVEL multiplier on all sensor noise
    buildings: list = field(default_factory=lambda: [
        BuildingSpec("bldg-office-01", "office", floors=2, pv_kwp=15.0),
        BuildingSpec("bldg-mall-01", "mall", floors=1, pv_kwp=20.0),
        BuildingSpec("bldg-hospital-01", "hospital", floors=1, pv_kwp=10.0),
        BuildingSpec("bldg-hotel-01", "hotel", floors=1, pv_kwp=8.0),
        BuildingSpec("bldg-college-01", "college", floors=1, pv_kwp=12.0),
        BuildingSpec("bldg-dc-01", "data_center", floors=1, pv_kwp=5.0),
    ])
    holidays: list = field(default_factory=lambda: [
        # {date, name, occupancy_factor{type: factor}}; "*" = every type not listed
        {"date": "2026-01-26", "name": "Republic Day", "factor": {"*": 0.15, "mall": 1.2, "hospital": 0.85, "hotel": 1.1, "data_center": 1.0}},
        {"date": "2026-05-01", "name": "Labour Day", "factor": {"*": 0.4, "mall": 1.1, "hospital": 0.9, "hotel": 1.0, "data_center": 1.0}},
        {"date": "2026-06-26", "name": "Muharram", "factor": {"*": 0.2, "mall": 1.15, "hospital": 0.85, "hotel": 1.05, "data_center": 1.0}},
        {"date": "2026-08-15", "name": "Independence Day", "factor": {"*": 0.15, "mall": 1.2, "hospital": 0.85, "hotel": 1.1, "data_center": 1.0}},
        {"date": "2026-10-02", "name": "Gandhi Jayanti", "factor": {"*": 0.15, "mall": 1.2, "hospital": 0.85, "hotel": 1.1, "data_center": 1.0}},
        {"date": "2026-11-08", "name": "Diwali", "factor": {"*": 0.1, "mall": 1.4, "hospital": 0.8, "hotel": 1.2, "data_center": 1.0}},
        {"date": "2026-12-25", "name": "Christmas", "factor": {"*": 0.2, "mall": 1.3, "hospital": 0.85, "hotel": 1.2, "data_center": 1.0}},
    ])
    events: list = field(default_factory=lambda: [
        # {date, name, building_types[], start_hour, end_hour, factor} — special days
        {"date": "2026-06-06", "name": "Weekend sale", "building_types": ["mall"], "start_hour": 11, "end_hour": 22, "factor": 1.5},
        {"date": "2026-06-12", "name": "Convocation", "building_types": ["college"], "start_hour": 9, "end_hour": 14, "factor": 1.4},
        {"date": "2026-06-19", "name": "Company offsite (reduced occupancy)", "building_types": ["office"], "start_hour": 0, "end_hour": 24, "factor": 0.35},
    ])
    quality: QualityConfig = field(default_factory=QualityConfig)
    anomalies: AnomalyConfig = field(default_factory=AnomalyConfig)
    db_path: str = str(DEFAULT_DB)

    # ------------------------------------------------------------------ helpers
    @property
    def start_dt(self) -> datetime:
        return datetime.combine(date.fromisoformat(self.start), datetime.min.time())

    def validate(self) -> list:
        errs = []
        try:
            date.fromisoformat(self.start)
        except ValueError:
            errs.append("start must be YYYY-MM-DD")
        if not 1 <= int(self.days) <= 400:
            errs.append("days must be 1..400")
        if int(self.step_min) not in (1, 5, 10, 15, 30, 60):
            errs.append("step_min must be one of 1, 5, 10, 15, 30, 60")
        if self.season != "auto" and self.season not in SEASONS:
            errs.append(f"season must be auto or one of {SEASONS}")
        if self.climate not in CLIMATES:
            errs.append(f"climate must be one of {CLIMATES}")
        from backend.building import PROFILES
        ids = [b.building_id for b in self.buildings]
        if not ids or len(set(ids)) != len(ids):
            errs.append("buildings must be non-empty with unique building_id")
        for b in self.buildings:
            if b.building_type not in PROFILES:
                errs.append(f"{b.building_id}: unknown building_type {b.building_type!r}")
            if not 1 <= int(b.floors) <= 50:
                errs.append(f"{b.building_id}: floors must be 1..50")
        if self.noise_level < 0 or not 0 <= self.quality.missing_data_rate <= 0.5:
            errs.append("noise_level >= 0 and missing_data_rate 0..0.5")
        if self.anomalies.rate_per_building_day < 0:
            errs.append("anomaly rate must be >= 0")
        return errs

    def public_dict(self) -> dict:
        d = asdict(self)
        d.pop("db_path", None)
        d["anomalies"]["types"] = list(self.anomalies.types)
        return d


def from_dict(d: dict) -> DatasetConfig:
    """Build a config from a (partial) dict; unknown keys raise KeyError."""
    base = DatasetConfig()
    known = {f.name for f in fields(DatasetConfig)}
    unknown = set(d) - known
    if unknown:
        raise KeyError(f"unknown dataset config keys: {sorted(unknown)}")
    kw = dict(d)
    if "buildings" in kw:
        kw["buildings"] = [b if isinstance(b, BuildingSpec) else BuildingSpec(**b) for b in kw["buildings"]]
    if "quality" in kw and isinstance(kw["quality"], dict):
        kw["quality"] = replace(base.quality, **kw["quality"])
    if "anomalies" in kw and isinstance(kw["anomalies"], dict):
        a = dict(kw["anomalies"])
        if "types" in a:
            a["types"] = tuple(a["types"])
        kw["anomalies"] = replace(base.anomalies, **a)
    return replace(base, **kw)


def load_config(path: Path | None = None, overrides: dict | None = None) -> DatasetConfig:
    p = Path(path) if path else CONFIG_FILE
    d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    d.update(overrides or {})
    return from_dict(d)

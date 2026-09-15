"""Dataset storage (SQLite, stdlib) + indexed queries, aggregation, downsampling, export.

Why SQLite: the project has no database and needs none for live state; but the
history must be queried by building / floor / zone / time without loading ~10^5-10^6
rows into memory, and sqlite3 ships with Python (no new dependency). Parquet would
need pyarrow. The file is generated, git-ignored, and rebuilt by
`python -m scripts.generate_dataset`.

Sinks: SqliteStore (disk) and MemorySink (tests) share one interface.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from pathlib import Path

from backend.dataset import schema as S

TABLES = {"building_dim": S.BUILDING_DIM, "zone_dim": S.ZONE_DIM, "time_dim": S.TIME_DIM,
          "weather_obs": S.WEATHER, "zone_obs": S.ZONE_OBS, "building_obs": S.BUILDING_OBS,
          "raw_obs": S.RAW_OBS, "clean_obs": S.CLEAN_OBS, "anomalies": S.ANOMALIES}
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_zone_bzt ON zone_obs(building_id, zone_id, t)",
    "CREATE INDEX IF NOT EXISTS ix_zone_bft ON zone_obs(building_id, floor_id, t)",
    "CREATE INDEX IF NOT EXISTS ix_zone_bt ON zone_obs(building_id, t)",
    "CREATE INDEX IF NOT EXISTS ix_bldg_bt ON building_obs(building_id, t)",
    "CREATE INDEX IF NOT EXISTS ix_raw_bzt ON raw_obs(building_id, zone_id, t)",
    "CREATE INDEX IF NOT EXISTS ix_clean_bzt ON clean_obs(building_id, zone_id, t)",
]
EXPORT_MAX_ROWS = 50_000


class MemorySink:
    """Collects everything in lists (tests / small in-memory runs)."""

    def __init__(self):
        self.data = {k: [] for k in TABLES}
        self.meta = {}

    def write(self, table: str, rows: list) -> None:
        self.data[table].extend(rows)

    def finish(self, meta: dict) -> None:
        self.meta = meta


class SqliteStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = None

    # ------------------------------------------------------------------ writing
    def create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".building")
        if tmp.exists():
            tmp.unlink()
        self._w = sqlite3.connect(tmp)
        self._w.execute("PRAGMA journal_mode=OFF")
        self._w.execute("PRAGMA synchronous=OFF")
        for name, cols in TABLES.items():
            self._w.execute(f"CREATE TABLE {name} (" + ", ".join(f"{c} {ty}" for c, ty in cols) + ")")
        self._w.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self._tmp = tmp

    def write(self, table: str, rows: list) -> None:
        if not rows:
            return
        cols = [c for c, _ in TABLES[table]]
        self._w.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(r.get(c) for c in cols) for r in rows])

    def finish(self, meta: dict) -> None:
        for ix in INDEXES:
            self._w.execute(ix)
        self._w.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
        self._w.commit()
        self._w.close()
        self.close()
        if self.path.exists():
            self.path.unlink()
        self._tmp.replace(self.path)          # atomic-ish: readers never see a half-built file

    # ------------------------------------------------------------------ reading
    def available(self) -> bool:
        return self.path.is_file()

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def q(self, sql: str, args=()) -> list:
        with self._lock:
            return [dict(r) for r in self._c().execute(sql, args).fetchall()]

    def meta(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.q("SELECT key, value FROM meta")}

    def catalog(self) -> dict:
        """Cached per file version (mtime + size): the row counts scan whole tables."""
        st = self.path.stat()
        key = (st.st_mtime_ns, st.st_size)
        if getattr(self, "_cat_key", None) != key:
            self._cat, self._cat_key = self._catalog(), key
        return self._cat

    def _catalog(self) -> dict:
        span =self.q("SELECT MIN(t) AS t0, MAX(t) AS t1, COUNT(*) AS n FROM time_dim")[0]
        buildings = self.q("SELECT * FROM building_dim ORDER BY building_id")
        zones = self.q("SELECT * FROM zone_dim ORDER BY building_id, floor_id, zone_id")
        for b in buildings:
            b.pop("profile_json", None)
            b["floor_ids"] = sorted({z["floor_id"] for z in zones if z["building_id"] == b["building_id"]})
            b["zone_list"] = [z for z in zones if z["building_id"] == b["building_id"]]
        counts = {t: self.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
                  for t in ("zone_obs", "building_obs", "weather_obs", "raw_obs", "clean_obs", "anomalies")}
        return {"t_start": span["t0"], "t_end": span["t1"], "timestamps": span["n"],
                "buildings": buildings, "row_counts": counts, "meta": self.meta()}

    def series(self, building_id: str, t0: int, t1: int, interval_s: int, level: str = "building",
               floor_id: str | None = None, zone_id: str | None = None, metrics: list | None = None) -> list:
        """Aggregated zone-derived metrics: SUM/AVG across zones per instant, then
        AVG/SUM per time bucket (schema.AGG). Adds <metric>_max for total_power_kw."""
        metrics = [m for m in (metrics or S.NUMERIC_ZONE_METRICS) if m in S.AGG]
        inner = ", ".join(f"{S.AGG[m][0]}({m}) AS {m}" for m in metrics)
        outer = ", ".join(f"{S.AGG[m][1]}({m}) AS {m}" for m in metrics)
        if "total_power_kw" in metrics:
            outer += ", MAX(total_power_kw) AS total_power_kw_max"
        where, args = "building_id = ? AND t >= ? AND t < ?", [building_id, t0, t1]
        if level == "floor":
            where += " AND floor_id = ?"
            args.append(floor_id)
        elif level == "zone":
            where += " AND zone_id = ?"
            args.append(zone_id)
        sql = (f"SELECT (t / ?) * ? AS t, COUNT(*) AS samples, {outer} FROM "
               f"(SELECT t, {inner} FROM zone_obs WHERE {where} GROUP BY t) GROUP BY (t / ?) ORDER BY 1")
        return self.q(sql, [interval_s, interval_s] + args + [interval_s])

    def weather_series(self, t0: int, t1: int, interval_s: int) -> list:
        num = [c for c, ty in S.WEATHER if ty == "REAL"]
        cols = ", ".join(f"AVG({c}) AS {c}" for c in num if c != "rainfall_mm")
        return self.q(f"SELECT (t / ?) * ? AS t, {cols}, AVG(rainfall_mm) AS rainfall_mm FROM weather_obs "
                      f"WHERE t >= ? AND t < ? GROUP BY (t / ?) ORDER BY 1",
                      [interval_s, interval_s, t0, t1, interval_s])

    def building_series(self, building_id: str, t0: int, t1: int, interval_s: int) -> list:
        return self.q("SELECT (t / ?) * ? AS t, AVG(renewable_power_kw) AS renewable_power_kw, "
                      "AVG(grid_power_kw) AS grid_power_kw, AVG(forecast_demand_kw) AS forecast_demand_kw, "
                      "AVG(current_demand_kw) AS current_demand_kw, MAX(peak_demand_kw) AS peak_demand_kw, "
                      "AVG(peak_demand_risk) AS peak_demand_risk, SUM(grid_energy_kwh) AS grid_energy_kwh, "
                      "MAX(daily_energy_kwh) AS daily_energy_kwh "
                      "FROM building_obs WHERE building_id = ? AND t >= ? AND t < ? GROUP BY (t / ?) ORDER BY 1",
                      [interval_s, interval_s, building_id, t0, t1, interval_s])

    def quality_series(self, building_id: str, zone_id: str, t0: int, t1: int) -> list:
        return self.q("SELECT r.t, r.temperature_raw_c, c.temperature_clean_c, r.co2_raw_ppm, c.co2_clean_ppm, "
                      "r.humidity_raw_percent, c.humidity_clean_percent, r.power_raw_kw, c.power_clean_kw, "
                      "r.raw_flags, c.temperature_quality, c.co2_quality FROM raw_obs r JOIN clean_obs c "
                      "ON r.t = c.t AND r.building_id = c.building_id AND r.zone_id = c.zone_id "
                      "WHERE r.building_id = ? AND r.zone_id = ? AND r.t >= ? AND r.t < ? ORDER BY r.t",
                      [building_id, zone_id, t0, t1])

    def anomalies(self, building_id: str | None = None, t0: str | None = None, t1: str | None = None) -> list:
        where, args = "1=1", []
        if building_id:
            where += " AND (building_id = ? OR building_id = '*')"
            args.append(building_id)
        if t0:
            where += " AND end_time >= ?"
            args.append(t0)
        if t1:
            where += " AND start_time < ?"
            args.append(t1)
        rows = self.q(f"SELECT * FROM anomalies WHERE {where} ORDER BY start_time", args)
        for r in rows:
            r["params"] = json.loads(r.pop("params_json") or "{}")
        return rows

    def export_rows(self, layer: str, building_id: str, t0: int, t1: int, zone_id: str | None = None,
                    limit: int = EXPORT_MAX_ROWS) -> tuple:
        table = {"zone": "zone_obs", "raw": "raw_obs", "clean": "clean_obs", "building": "building_obs"}[layer]
        where, args = "building_id = ? AND t >= ? AND t < ?", [building_id, t0, t1]
        if zone_id and layer != "building":
            where += " AND zone_id = ?"
            args.append(zone_id)
        rows = self.q(f"SELECT * FROM {table} WHERE {where} ORDER BY t LIMIT ?", args + [limit + 1])
        return rows[:limit], len(rows) > limit


def to_csv(rows: list) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()

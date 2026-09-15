"""Timestamp features, seasons and the holiday / special-event calendar. Pure."""
from __future__ import annotations

from datetime import datetime

# India Meteorological Department seasons (north India): winter Dec-Feb, summer
# (pre-monsoon) Mar-May, monsoon Jun-Sep, post-monsoon transition Oct-Nov.
MONTH_SEASON = {12: "winter", 1: "winter", 2: "winter", 3: "summer", 4: "summer",
                5: "summer", 6: "monsoon", 7: "monsoon", 8: "monsoon", 9: "monsoon",
                10: "transition", 11: "transition"}


def season_for(ts: datetime, fixed: str = "auto") -> str:
    return MONTH_SEASON[ts.month] if fixed == "auto" else fixed


def day_part(hour: int) -> str:
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "night"


class Calendar:
    """Holidays and events from DatasetConfig. Adding a future date is config only."""

    def __init__(self, holidays: list, events: list):
        self.holidays = {h["date"]: h for h in holidays}
        self.events: dict = {}
        for e in events:
            self.events.setdefault(e["date"], []).append(e)

    def holiday(self, ts: datetime) -> dict | None:
        return self.holidays.get(ts.date().isoformat())

    def occupancy_factor(self, ts: datetime, building_type: str) -> tuple:
        """(multiplier, label) for this timestamp and building type."""
        f, label = 1.0, None
        h = self.holiday(ts)
        if h:
            fac = h.get("factor", {})
            f *= float(fac.get(building_type, fac.get("*", 0.2)))
            label = h["name"]
        for e in self.events.get(ts.date().isoformat(), []):
            if building_type in e.get("building_types", []) and \
                    e.get("start_hour", 0) <= ts.hour < e.get("end_hour", 24):
                f *= float(e.get("factor", 1.0))
                label = e["name"] if label is None else f"{label}; {e['name']}"
        return f, label


def time_features(ts: datetime, cal: Calendar, season: str, is_open: bool) -> dict:
    """Every TIME column, derived from the timestamp (never entered by hand)."""
    iso = ts.isocalendar()
    hol = cal.holiday(ts)
    return {
        "timestamp": ts.isoformat(timespec="minutes"),
        "date": ts.date().isoformat(),
        "time": ts.strftime("%H:%M"),
        "hour": ts.hour, "minute": ts.minute,
        "day_of_week": ts.weekday(), "day_of_month": ts.day, "month": ts.month,
        "week_of_year": iso[1],
        "is_weekend": int(ts.weekday() >= 5),
        "is_holiday": int(hol is not None),
        "holiday_name": hol["name"] if hol else None,
        "season": season,
        "day_part": day_part(ts.hour),
        "operating_hours": int(is_open),
    }

"""Synthetic weather for an Indian city in August + optional Open-Meteo hook.

Deterministic (seeded) so every controller comparison uses identical weather.
Swap in real data later with fetch_openmeteo() — same interface.
"""
from __future__ import annotations

import math


def _day_offset(day: int, seed: int = 0) -> float:
    """Deterministic per-day base-temperature wobble in [-1.5, +1.5] degC."""
    x = math.sin((day + 1) * 12.9898 + seed * 78.233) * 43758.5453
    return (x - math.floor(x)) * 3.0 - 1.5


def outdoor_temp(t_seconds: float, seed: int = 0) -> float:
    """Outdoor dry-bulb temp (degC) at sim time t (seconds since sim start)."""
    day = int(t_seconds // 86400)
    h = (t_seconds % 86400) / 3600.0
    base = 30.0 + _day_offset(day, seed)
    # Diurnal swing: min ~3 AM, max ~3 PM
    swing = 6.0 * math.sin(math.pi * (h - 9.0) / 12.0)
    # Small smooth intra-day wobble (clouds, breeze)
    wobble = 0.6 * math.sin(h * 2.1 + day)
    return base + swing + wobble


def solar_factor(orientation: str, h: float) -> float:
    """0..1 solar gain factor by facade orientation and hour of day."""
    if h < 6.5 or h > 18.5:
        return 0.0
    daylight = math.sin(math.pi * (h - 6.5) / 12.0)  # envelope
    peaks = {"E": 9.0, "S": 12.5, "W": 16.0, "N": None}
    peak = peaks.get(orientation)
    if peak is None:  # north facade: diffuse only
        return 0.25 * daylight
    return max(0.0, daylight * math.exp(-((h - peak) ** 2) / (2 * 2.6**2)))


def fetch_openmeteo(lat: float = 28.61, lon: float = 77.21, days: int = 7):
    """OPTIONAL: pull real hourly temps (Open-Meteo, free, no API key).

    Returns list of hourly temps you can feed to DigitalTwin via a custom
    weather_fn. Requires network; the demo never depends on it.
    """
    import httpx

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&hourly=temperature_2m&past_days={days}&forecast_days=1"
    )
    r = httpx.get(url, timeout=10)
    r.raise_for_status()
    return r.json()["hourly"]["temperature_2m"]

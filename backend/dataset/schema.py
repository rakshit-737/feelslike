"""The dataset schema: ONE place that names every column, its SQL type, its group and
how it aggregates. store.py builds the DDL from it; DATA_CONTRACTS.md §11 documents it.

Row convention: `ts` is the START of the sample interval (local time, ISO minutes),
`t` the same instant in unix seconds. Powers are means over [ts, ts+step); energies
are the energy of that interval; temperatures/RH/CO2 are the state at interval end;
occupancy is the headcount for the interval.
"""
from __future__ import annotations

# (column, sql type, group)
BUILDING_DIM = [("building_id", "TEXT PRIMARY KEY"), ("building_type", "TEXT"), ("building_name", "TEXT"),
                ("floors", "INTEGER"), ("zones", "INTEGER"), ("pv_kwp", "REAL"),
                ("occupancy_capacity", "INTEGER"), ("design_demand_kw", "REAL"), ("profile_json", "TEXT")]
ZONE_DIM = [("building_id", "TEXT"), ("floor_id", "TEXT"), ("zone_id", "TEXT"), ("twin_zone", "TEXT"),
            ("zone_role", "TEXT"), ("area_m2", "REAL"), ("occupancy_capacity", "INTEGER"),
            ("cooling_capacity_kw", "REAL"), ("heating_capacity_kw", "REAL"), ("design_demand_kw", "REAL")]
TIME_DIM = [("t", "INTEGER PRIMARY KEY"), ("ts", "TEXT"), ("date", "TEXT"), ("time", "TEXT"),
            ("hour", "INTEGER"), ("minute", "INTEGER"), ("day_of_week", "INTEGER"),
            ("day_of_month", "INTEGER"), ("month", "INTEGER"), ("week_of_year", "INTEGER"),
            ("is_weekend", "INTEGER"), ("is_holiday", "INTEGER"), ("holiday_name", "TEXT"),
            ("season", "TEXT"), ("day_part", "TEXT")]
WEATHER = [("t", "INTEGER PRIMARY KEY"), ("outdoor_temperature_c", "REAL"), ("outdoor_humidity_percent", "REAL"),
           ("outdoor_pressure_hpa", "REAL"), ("outdoor_wind_speed_mps", "REAL"), ("outdoor_wind_direction", "TEXT"),
           ("solar_irradiance_w_m2", "REAL"), ("cloud_cover_percent", "REAL"), ("rainfall_mm", "REAL"),
           ("weather_condition", "TEXT"), ("dew_point_c", "REAL"), ("heat_index_c", "REAL"),
           ("wet_bulb_temperature_c", "REAL")]

GROUPS = {
    "identity": ["building_id", "floor_id", "zone_id"],
    "occupancy": ["occupancy_count", "occupancy_percent", "occupancy_capacity", "expected_occupancy",
                  "occupancy_change", "occupancy_status"],
    "environment": ["indoor_temperature_c", "indoor_humidity_percent", "indoor_co2_ppm",
                    "indoor_air_quality_index", "temperature_setpoint_c", "heating_setpoint_c",
                    "humidity_setpoint_percent", "co2_limit_ppm"],
    "hvac": ["hvac_status", "hvac_mode", "cooling_demand_percent", "heating_demand_percent",
             "ventilation_demand_percent", "hvac_power_kw", "supply_air_temperature_c",
             "return_air_temperature_c", "fan_speed_percent", "damper_position_percent",
             "compressor_load_percent"],
    "energy": ["total_power_kw", "lighting_power_kw", "plug_load_kw", "ventilation_power_kw",
               "equipment_power_kw", "energy_consumption_kwh", "hvac_energy_kwh",
               "lighting_energy_kwh", "equipment_energy_kwh"],
    "demand": ["current_demand_kw", "hvac_demand_kw", "cooling_demand_kw", "heating_demand_kw",
               "ventilation_demand_kw", "occupancy_demand_factor"],
    "comfort": ["comfort_score", "temperature_comfort_score", "humidity_comfort_score",
                "co2_comfort_score", "pmv", "ppd", "thermal_comfort_status"],
    "anomalies": ["anomaly_ids"],
}
TEXT_COLS = {"building_id", "floor_id", "zone_id", "occupancy_status", "hvac_status", "hvac_mode",
             "thermal_comfort_status", "anomaly_ids", "ts"}
INT_COLS = {"occupancy_count", "occupancy_capacity", "occupancy_change", "t"}

ZONE_OBS = [("t", "INTEGER"), ("ts", "TEXT")] + [
    (c, "TEXT" if c in TEXT_COLS else "INTEGER" if c in INT_COLS else "REAL")
    for g in GROUPS.values() for c in g]

BUILDING_OBS = [("t", "INTEGER"), ("ts", "TEXT"), ("building_id", "TEXT"), ("operating_hours", "INTEGER"),
                ("special_day", "TEXT"), ("occupancy_count", "INTEGER"), ("occupancy_capacity", "INTEGER"),
                ("occupancy_percent", "REAL"), ("total_power_kw", "REAL"), ("hvac_power_kw", "REAL"),
                ("ventilation_power_kw", "REAL"), ("lighting_power_kw", "REAL"), ("plug_load_kw", "REAL"),
                ("equipment_power_kw", "REAL"), ("renewable_power_kw", "REAL"), ("grid_power_kw", "REAL"),
                ("energy_consumption_kwh", "REAL"), ("grid_energy_kwh", "REAL"), ("daily_energy_kwh", "REAL"),
                ("peak_demand_kw", "REAL"), ("current_demand_kw", "REAL"), ("forecast_demand_kw", "REAL"),
                ("hvac_demand_kw", "REAL"), ("cooling_demand_kw", "REAL"), ("heating_demand_kw", "REAL"),
                ("ventilation_demand_kw", "REAL"), ("occupancy_demand_factor", "REAL"),
                ("peak_demand_risk", "REAL"), ("demand_category", "TEXT"), ("comfort_score", "REAL")]

RAW_OBS = [("t", "INTEGER"), ("building_id", "TEXT"), ("zone_id", "TEXT"),
           ("temperature_raw_c", "REAL"), ("humidity_raw_percent", "REAL"), ("co2_raw_ppm", "REAL"),
           ("occupancy_raw_count", "INTEGER"), ("power_raw_kw", "REAL"),
           ("reading_delay_s", "INTEGER"), ("raw_flags", "TEXT")]
CLEAN_OBS = [("t", "INTEGER"), ("building_id", "TEXT"), ("zone_id", "TEXT"),
             ("temperature_clean_c", "REAL"), ("humidity_clean_percent", "REAL"), ("co2_clean_ppm", "REAL"),
             ("occupancy_clean_count", "INTEGER"), ("power_clean_kw", "REAL"),
             ("temperature_quality", "TEXT"), ("humidity_quality", "TEXT"), ("co2_quality", "TEXT"),
             ("occupancy_quality", "TEXT"), ("power_quality", "TEXT")]
ANOMALIES = [("anomaly_id", "TEXT PRIMARY KEY"), ("anomaly_type", "TEXT"), ("severity", "TEXT"),
             ("start_time", "TEXT"), ("end_time", "TEXT"), ("building_id", "TEXT"),
             ("affected_zone", "TEXT"), ("layer", "TEXT"), ("description", "TEXT"), ("params_json", "TEXT")]

# Aggregation: (across zones at one instant, across time within a bucket).
# Additive quantities SUM across zones (no double counting: each zone row is disjoint);
# intensive quantities AVERAGE. Energies SUM over time; everything else averages.
AGG = {}
for c in GROUPS["energy"] + GROUPS["demand"] + ["hvac_power_kw", "occupancy_count", "expected_occupancy",
                                                 "occupancy_capacity"]:
    AGG[c] = ("SUM", "SUM" if c.endswith("_kwh") else "AVG")
AGG["occupancy_demand_factor"] = ("AVG", "AVG")
for c in ["occupancy_percent", "indoor_temperature_c", "indoor_humidity_percent", "indoor_co2_ppm",
          "indoor_air_quality_index", "temperature_setpoint_c", "cooling_demand_percent",
          "heating_demand_percent", "ventilation_demand_percent", "fan_speed_percent",
          "comfort_score", "temperature_comfort_score", "humidity_comfort_score",
          "co2_comfort_score", "pmv", "ppd"]:
    AGG[c] = ("AVG", "AVG")
NUMERIC_ZONE_METRICS = list(AGG)

# data state of every served group (never "live"/"real"/"hardware": this is generated history)
DATA_STATE = {"weather": "simulated", "occupancy": "simulated", "environment": "simulated",
              "hvac": "simulated", "energy": "simulated", "demand": "derived", "comfort": "derived",
              "forecast_demand_kw": "predicted", "raw": "simulated_sensor_raw", "clean": "derived"}

"""FeelsLike historical dataset: generation -> validation -> cleaning -> features -> store.

Module map (one responsibility each; the pipeline is wired in generator.py):
  config.py     the ONE central configuration (DatasetConfig) + JSON/CLI loading
  calendar.py   timestamp features, seasons, holiday/event calendar
  weather.py    seasonal temporal weather model + psychrometric derivations
  occupancy.py  building-type occupancy model (reuses backend.building profiles)
  loads.py      lighting / plug / equipment / PV loads and HVAC sizing
  hvac.py       BMS schedule controller + HVAC observables
  comfort.py    explainable comfort score + ISO 7730 PMV/PPD (stated assumptions)
  anomalies.py  rare, configurable anomaly schedule with metadata
  quality.py    RAW sensor layer (noise/drift/missing/delay/outliers), validation, CLEAN layer
  generator.py  runs the existing sim/twin.py physics per floor and emits rows
  store.py      SQLite (stdlib) storage, indexed queries, aggregation, downsampling, export

Every generated value is SIMULATED history. Nothing here is live, real or hardware.
"""

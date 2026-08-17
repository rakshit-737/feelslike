"""Shared fixtures for the behavioural suites.

Two things every suite here needs that neither pytest nor the source provides on
its own:

1. OFFLINE DETERMINISM. backend/parser.py calls load_dotenv() at import time and
   this repo's .env carries a real OPENAI_API_KEY, so parse() would make a live
   HTTPS call — slow, network-dependent, and a different answer every run. We
   import the parser FIRST (so load_dotenv has already fired) and only THEN strip
   the keys. That ordering is the only one that sticks: popping before the import
   would simply let load_dotenv put them straight back. Tests therefore exercise
   the offline rules pipeline, which is the path that must work with no Wi-Fi.
2. A FROZEN LIVE SIM. backend/app.py starts a daemon thread at import that steps
   the physics at 240x. Anything asserting on live state needs the building to
   hold still, so the client fixture drops the speed to its floor (1 sim-second
   per real second — one physics step every five real minutes) and time is driven
   explicitly through sim.advance() instead.

Nothing here mutates a source file, and no fixture reaches into a private
attribute of a subsystem it does not own.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                 # `pytest tests/` from the repo root
    sys.path.insert(0, str(ROOT))

import backend.parser  # noqa: E402  imported for its load_dotenv side effect

for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

MORNING_H = 10.0        # zones a/c/d/e occupied, conference room b idle until 10:00
SETTLE_MIN = 60         # sim-minutes of steady 25 degC / vent 1 before a reading


def pytest_configure(config):
    """Register the markers these suites use so a run is warning-free."""
    config.addinivalue_line("markers", "slow: multi-day simulation runs")
    config.addinivalue_line(
        "markers",
        "defect: documents a KNOWN source defect; xfail(strict) so the day it is "
        "fixed the test fails loudly and tells you to drop the marker")


def step(twin, minutes: int, setpoint: float | None = 25.0, vent: int = 1):
    """Drive a twin for `minutes` sim-minutes at a fixed command. Returns the twin.

    Importable by any suite: `from conftest import step` does not work under
    pytest's rootdir handling, so it is also exposed as the `stepper` fixture.
    """
    from sim.twin import ZONES
    for _ in range(int(minutes)):
        twin.step({z.id: setpoint for z in ZONES}, {z.id: vent for z in ZONES})
    return twin


@pytest.fixture
def stepper():
    """The step() helper, as a fixture (pytest cannot import conftest by name)."""
    return step


@pytest.fixture
def twin():
    """A fresh seed-7 twin parked at 10:00 on Monday. No steps taken yet."""
    from sim.twin import DigitalTwin
    t = DigitalTwin(seed=7)
    t.t = MORNING_H * 3600.0
    return t


@pytest.fixture
def warm_twin(twin):
    """The same twin after an hour of steady cooling.

    Why a settled twin matters: a brand-new twin starts every zone at 28 degC with
    outdoor moisture and has never applied a setpoint, so last_setpoints is empty
    and RH has not converged. Anything that reads humidity, capacity utilisation
    or a previous setpoint needs the building to have actually been running.
    """
    return step(twin, SETTLE_MIN)


@pytest.fixture
def store():
    """An empty ConstraintStore."""
    from backend.constraints import ConstraintStore
    return ConstraintStore()


@pytest.fixture(scope="session")
def client():
    """A TestClient over the real FastAPI app, with the sim thread effectively frozen.

    Session-scoped because backend.app owns exactly one module-level LiveSim and
    one daemon thread; building a second would race the first. Tests that need a
    clean building ask for `fresh_client` instead.
    """
    from fastapi.testclient import TestClient

    from backend.app import app, sim
    with sim.lock:
        sim.speed = 1.0                      # 1 sim-s per real-s: ~1 step / 5 min
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fresh_client(client):
    """`client` with the building rebuilt at the start hour first.

    POST /api/reset is the product's own reset, so this exercises the shipped path
    rather than reaching into LiveSim internals.
    """
    assert client.post("/api/reset").status_code == 200
    return client


@pytest.fixture
def live():
    """The module-level LiveSim, for driving time explicitly in API tests."""
    from backend.app import sim
    return sim

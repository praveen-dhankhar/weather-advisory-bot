"""Shared harness for the eval suite.

Mode A (default): recorded Open-Meteo fixtures + the deterministic stand-in LLM.
Nothing leaves the machine, and the results are identical on every run.
Mode B: `pytest --run-live` additionally runs the tests marked `live`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import llm, weather  # noqa: E402
from backend.fake_llm import fake_llm  # noqa: E402
from backend.graph import answer, build_graph  # noqa: E402
from backend.loader import get_policy  # noqa: E402
from backend.memory import Memory  # noqa: E402
from backend.weather import location_from_geocode, snapshot_from_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-live", action="store_true", default=False,
                     help="also run the tests that call the live Open-Meteo API")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="live API test; pass --run-live to include it")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def load_fixture(name: str) -> tuple[Any, Any, dict]:
    """(place, snapshot, raw payload) from a recorded/derived fixture file."""
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    place = location_from_geocode(payload["geocode"])
    snapshot = snapshot_from_payload(payload["forecast"], place, get_policy())
    return place, snapshot, payload


class Bot:
    """One isolated bot: its own memory, its own compiled graph, fixture weather."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.memory = Memory()
        self.graph = build_graph()
        self.snapshot = None
        self.llm_calls: list[str] = []

    def use(self, fixture: str) -> None:
        place, snapshot, _ = load_fixture(fixture)
        self.snapshot = snapshot
        self.monkeypatch.setattr(weather, "geocode", lambda *_a, **_k: place)
        self.monkeypatch.setattr(weather, "fetch_forecast", lambda *_a, **_k: snapshot)

    def use_llm(self, fn: Callable[[str, str], str]) -> None:
        llm.set_fake(fn)

    def count_llm_jobs(self) -> None:
        """Record which JOB each LLM call was for, so tests can assert on it."""
        def counting(system: str, user: str) -> str:
            self.llm_calls.append(system.splitlines()[0].replace("JOB:", "").strip())
            return fake_llm(system, user)

        llm.set_fake(counting)

    def ask(self, message: str, session_id: str = "test") -> dict:
        return answer(message, session_id=session_id, memory=self.memory, graph=self.graph)


@pytest.fixture(autouse=True)
def deterministic_llm():
    """Every test runs against the stand-in unless it installs its own."""
    llm.set_fake(fake_llm)
    yield
    llm.set_fake(None)


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> Bot:
    return Bot(monkeypatch)


@pytest.fixture(scope="session")
def policy():
    return get_policy()


def numbers_in(text: str) -> set[float]:
    import re

    return {float(m) for m in re.findall(r"\d+(?:\.\d+)?", re.sub(r"SOP-[A-Z0-9]+-\d+", " ", text))}


def sop_ids_in(text: str) -> set[str]:
    import re

    return set(re.findall(r"SOP-[A-Z0-9]+-\d+", text))

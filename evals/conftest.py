"""Shared harness for the eval suite.

Mode A (default): recorded Open-Meteo fixtures + the deterministic stand-in LLM.
Nothing leaves the machine, and the results are identical on every run.
Mode B: `pytest --run-live` additionally runs the tests marked `live`.

`--eval-report=PATH` (with the `=`) writes what actually happened - each case, what it checks, its pass
condition, what the bot did, and the result - from the run itself, so no result in
the report is typed by hand.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable

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
    parser.addoption("--eval-report", default=None,
                     help="write a markdown eval report (case, check, expected, observed, result)")


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
        self.turns: list[dict] = []
        self.fetches = 0

    def use(self, fixture: str) -> None:
        place, snapshot, _ = load_fixture(fixture)
        self.snapshot = snapshot

        def fetch(*_a: Any, **_k: Any) -> Any:
            self.fetches += 1
            return snapshot

        self.monkeypatch.setattr(weather, "geocode", lambda *_a, **_k: place)
        self.monkeypatch.setattr(weather, "fetch_forecast", fetch)

    def use_llm(self, fn: Callable[[str, str], str]) -> None:
        llm.set_fake(fn)

    def count_llm_jobs(self) -> None:
        """Record which JOB each LLM call was for, so tests can assert on it."""
        def counting(system: str, user: str) -> str:
            self.llm_calls.append(system.splitlines()[0].replace("JOB:", "").strip())
            return fake_llm(system, user)

        llm.set_fake(counting)

    def ask(self, message: str, session_id: str = "test") -> dict:
        out = answer(message, session_id=session_id, memory=self.memory, graph=self.graph)
        self.turns.append(out)
        return out


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


# --------------------------------------------------------------------------- #
# --eval-report: an eval report generated from the run, never written by hand
# --------------------------------------------------------------------------- #
_ROWS: list[dict[str, str]] = []


def _observed(bot: Any) -> str:
    if not bot or not bot.turns:
        return "-"
    last = bot.turns[-1]
    guard = last.get("guard_report") or {}
    text = (f"branch={last.get('branch')}; surfaced SOPs="
            f"{', '.join(last.get('matched_sop_ids') or []) or 'none'}")
    if guard:
        text += f"; guard ok={guard.get('ok')}"
        if guard.get("fell_back"):
            text += "; fell back to template"
        elif any("guard: retry passed" in line for line in last.get("trace") or []):
            text += "; passed on the stricter retry"
    return text + (f" ({len(bot.turns)} turns)" if len(bot.turns) > 1 else "")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Any:
    outcome = yield
    rep = outcome.get_result()
    if not (rep.when == "call" or (rep.when == "setup" and not rep.passed)):
        return
    doc = " ".join((getattr(item, "function", None).__doc__ or "").split())
    checks, _, expected = doc.partition("PASS =")
    if rep.skipped:
        detail = str(rep.longrepr[2] if isinstance(rep.longrepr, tuple) else rep.longrepr)
    elif rep.failed:
        detail = (rep.longreprtext.strip().splitlines() or ["?"])[-1][:400]
    else:
        detail = ""
    bot = getattr(item, "funcargs", {}).get("bot")
    weather_source = ("live Open-Meteo" if "live" in item.keywords and not (bot and bot.snapshot)
                      else "recorded weather")
    _ROWS.append({
        "case": item.nodeid.split("::", 1)[-1],
        "file": item.nodeid.split("::", 1)[0],
        "mode": f"{weather_source}, {'real LLM' if 'live_llm' in item.keywords else 'stand-in LLM'}",
        "checks": checks.replace("CHECKS", "", 1).strip(" :"),
        "expected": expected.strip(),
        "observed": _observed(bot),
        "result": rep.outcome.upper() if not (rep.when == "setup" and rep.failed) else "ERROR",
        "detail": detail,
    })


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = session.config.getoption("--eval-report")
    if not path or not _ROWS:
        return
    counts: dict[str, int] = {}
    for row in _ROWS:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    lines = [
        "# Eval report (generated)",
        "",
        f"Generated by `pytest {' '.join(session.config.invocation_params.args)}` on "
        f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')}. Every row below is "
        "this run's outcome; nothing is edited by hand. Regenerate it rather than editing it.",
        "",
        "**Totals:** " + ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items())),
        "",
    ]
    for index, row in enumerate(_ROWS, 1):
        lines += [f"### {index}. `{row['case']}` - {row['result']}", "",
                  f"- **File / mode:** `{row['file']}` - {row['mode']}"]
        if row["checks"]:
            lines.append(f"- **Checks:** {row['checks']}")
        if row["expected"]:
            lines.append(f"- **Expected:** {row['expected']}")
        lines.append(f"- **Observed:** {row['observed']}")
        if row["detail"]:
            lines.append(f"- **{'Skip reason' if row['result'] == 'SKIPPED' else 'Failure'}:** {row['detail']}")
        lines.append("")
    Path(path).write_text("\n".join(lines))

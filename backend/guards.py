"""Post-compose output validation, run on the model's DRAFT before code renders it.

All checks are mechanical:
  a) every SOP id cited is one of the surfaced ids, every surfaced id is cited, and
     the primary is cited first - the conflict rule survives the model;
  b) every {placeholder} names a reading the composer was offered;
  c) every other number in the draft is written in the surfaced SOPs' text, the
     place name or the period's clock times - so no weather figure can come from the
     model at all, correct or not; readings arrive only through placeholders.

Failure retries the composer once with a stricter prompt, then falls back to the
deterministic template. The model never gets a third chance to be creative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.loader import get_policy
from backend.models import GraphState
from backend.nodes import composer

SOP_ID_RE = re.compile(r"SOP-[A-Z0-9]+-\d+")
CLOCK_RE = re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?!\d)")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class GuardReport:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    bad_ids: list[str] = field(default_factory=list)
    bad_numbers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "bad_ids": self.bad_ids,
            "bad_numbers": self.bad_numbers,
        }

    def fail(self, problem: str) -> None:
        self.ok = False
        self.problems.append(problem)


def _clocks(text: str) -> list[str]:
    return [f"{int(h):02d}:{m}" for h, m in CLOCK_RE.findall(str(text))]


def allowed_numbers(*texts: str) -> set[float]:
    """Every bare figure a draft may contain: the numbers literally written in these
    texts, plus the hour of any clock time in them ("before 11:00" -> "before 11")."""
    out: set[float] = set()
    for text in map(str, texts):
        out |= {float(hour) for hour, _ in CLOCK_RE.findall(text)}
        out |= {float(n) for n in NUMBER_RE.findall(CLOCK_RE.sub(" ", text))}
    return out


def allowed_clocks(*texts: str) -> set[str]:
    return {clock for text in texts for clock in _clocks(text)}


def check_reply(
    draft: str,
    surfaced_ids: list[str],
    placeholders: Iterable[str],
    allowed: Iterable[float],
    clocks: Iterable[str] = (),
) -> GuardReport:
    report = GuardReport()
    draft = draft or ""
    allowed, clocks, placeholders = set(allowed), set(clocks), set(placeholders)

    cited = SOP_ID_RE.findall(draft)
    report.bad_ids = sorted({i for i in cited if i not in surfaced_ids})
    if report.bad_ids:
        report.fail(f"cited SOP ids that were not surfaced: {report.bad_ids}")
    uncited = [i for i in surfaced_ids if i not in cited]
    if uncited:
        report.fail(f"surfaced SOPs not cited: {uncited}")
    if cited and surfaced_ids and cited[0] != surfaced_ids[0]:
        report.fail(f"the primary SOP {surfaced_ids[0]} is not cited first (got {cited[0]})")

    unknown = sorted({p for p in composer.PLACEHOLDER_RE.findall(draft) if p not in placeholders})
    if unknown:
        report.fail(f"placeholders for readings that were not offered: {unknown}")

    text = composer.PLACEHOLDER_RE.sub(" ", SOP_ID_RE.sub(" ", draft))
    bad_clocks = sorted({c for c in _clocks(text) if c not in clocks})
    numbers = [n for n in NUMBER_RE.findall(CLOCK_RE.sub(" ", text)) if float(n) not in allowed]
    report.bad_numbers = sorted(set(numbers)) + bad_clocks
    if report.bad_numbers:
        report.fail(f"figures typed by the model with no source in the SOP text: {report.bad_numbers}")
    return report


def guard_inputs(state: GraphState) -> tuple[list[str], set[str], set[float], set[str]]:
    """(surfaced ids, offered placeholders, allowed numbers, allowed clock times)."""
    policy = get_policy()
    primary, secondary = composer.split(state["matched"], policy)
    sops = [primary, *secondary]
    window = state["window"]
    numbers = composer.relevant_numbers(state["matched"], window, policy)
    sop_text = [f"{s.advice} {s.cite_as} {s.title}" for s in sops]
    # reading labels such as "next 24 hours" are shown to the model, so their digits are fair;
    # the period's hours are allowed only as clock times ("17:00"), never as bare numbers
    labels = [state["weather"].place.label] + [composer.label(name, policy) for name in numbers]
    return ([s.id for s in sops], set(numbers), allowed_numbers(*sop_text, *labels),
            allowed_clocks(*sop_text, composer.period_text(window)))


def run(state: GraphState) -> dict[str, Any]:
    """Guard node: validate the draft, retry once stricter, then fall back to the template."""
    policy = get_policy()
    trace = list(state.get("trace") or [])
    ids, placeholders, numbers, clocks = guard_inputs(state)

    report = check_reply(state.get("draft", ""), ids, placeholders, numbers, clocks)
    if report.ok:
        trace.append("guard: passed")
        return {"reply": composer.finalize(state["draft"], state),
                "guard_report": report.as_dict(), "trace": trace}

    trace.append(f"guard: FAILED ({'; '.join(report.problems)}) - retrying with a stricter prompt")
    override = bool(state.get("situational_ids"))
    retry = composer.run(state, override=override, stricter=True)
    second = check_reply(retry.get("draft", ""), ids, placeholders, numbers, clocks)
    if second.ok:
        trace.append("guard: retry passed")
        return {"reply": composer.finalize(retry["draft"], state),
                "guard_report": second.as_dict(), "trace": trace}

    primary, secondary = composer.split(state["matched"], policy)
    fallback = composer.template_reply(state, primary, secondary, override)
    final = check_reply(fallback, ids, placeholders, numbers, clocks)
    trace.append(
        f"guard: retry also FAILED ({'; '.join(second.problems)}) - "
        f"fell back to the deterministic template (template guard ok={final.ok})"
    )
    return {
        "reply": composer.finalize(fallback, state),
        "guard_report": {**final.as_dict(), "fell_back": True, "first_failure": report.problems,
                         "retry_failure": second.problems},
        "trace": trace,
    }

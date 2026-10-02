"""Post-compose output validation.

Three checks, all mechanical:
  a) every SOP id cited in the reply is one of the matched ids;
  b) every number in the reply traces to the snapshot or to the SOP text;
  c) the reply either cites an SOP or states plainly that none applies.

Failure retries the composer once with a stricter prompt, then falls back to the
deterministic template. The model never gets a third chance to be creative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.loader import get_policy
from backend.models import GraphState
from backend.nodes import composer, fixed

SOP_ID_RE = re.compile(r"SOP-[A-Z0-9]+-\d+")
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


def _numbers_in(text: str) -> list[float]:
    return [float(m) for m in NUMBER_RE.findall(str(text))]


def allowed_numbers(*sources: Any) -> set[float]:
    """Every figure the reply is permitted to contain, from snapshot + SOP text."""
    out: set[float] = set()
    for source in sources:
        if source is None:
            continue
        if isinstance(source, str):
            out.update(_numbers_in(source))
        elif isinstance(source, dict):
            out |= allowed_numbers(*source.values())
        elif isinstance(source, (list, tuple, set)):
            out |= allowed_numbers(*source)
        elif isinstance(source, (int, float)) and not isinstance(source, bool):
            out.add(float(source))
    # a figure may legitimately be written rounded to the nearest whole number
    out |= {round(v) for v in list(out)}
    return out


def _is_allowed(value: float, allowed: Iterable[float]) -> bool:
    return any(abs(value - a) <= max(0.51, abs(a) * 0.02) for a in allowed)


def check_reply(
    reply: str,
    matched_ids: Iterable[str],
    allowed: Iterable[float],
    no_sop_sentence: str = fixed.NO_SOP_SENTENCE,
) -> GuardReport:
    report = GuardReport()
    matched = set(matched_ids)
    allowed = set(allowed)

    cited = SOP_ID_RE.findall(reply or "")
    for sop_id in cited:
        if sop_id not in matched:
            report.ok = False
            report.bad_ids.append(sop_id)
    if report.bad_ids:
        report.problems.append(f"cited SOP ids not in the matched set: {sorted(set(report.bad_ids))}")

    stripped = SOP_ID_RE.sub(" ", reply or "")
    for value in _numbers_in(stripped):
        if not _is_allowed(value, allowed):
            report.ok = False
            report.bad_numbers.append(str(value))
    if report.bad_numbers:
        report.problems.append(
            f"numbers with no source in the snapshot or SOP text: {sorted(set(report.bad_numbers))}"
        )

    if not cited and no_sop_sentence.lower() not in (reply or "").lower():
        report.ok = False
        report.problems.append("reply neither cites an SOP nor states that no SOP applies")
    return report


def build_allowed(state: GraphState) -> tuple[set[float], list[str]]:
    """Allowed figures and allowed ids for the current state."""
    policy = get_policy()
    matched = state.get("matched") or []
    ids = [m.sop_id for m in matched]
    texts = [policy.sops[i].advice + " " + policy.sops[i].cite_as + " " + policy.sops[i].title for i in ids]
    window = state.get("window")
    snapshot = state.get("weather")
    numbers = allowed_numbers(
        texts,
        window.values if window else None,
        window.times if window else None,
        snapshot.current if snapshot else None,
        snapshot.derived if snapshot else None,
    )
    return numbers, ids


def run(state: GraphState) -> dict[str, Any]:
    """Guard node: validate, retry once stricter, then fall back to the template."""
    policy = get_policy()
    trace = list(state.get("trace") or [])
    numbers, ids = build_allowed(state)

    report = check_reply(state.get("reply", ""), ids, numbers)
    if report.ok:
        trace.append("guard: passed")
        return {"guard_report": report.as_dict(), "trace": trace}

    trace.append(f"guard: FAILED ({'; '.join(report.problems)}) - retrying with a stricter prompt")
    override = bool(state.get("situational_ids"))
    retry = composer.run(state, override=override, stricter=True)
    second = check_reply(retry.get("reply", ""), ids, numbers)
    if second.ok:
        trace.append("guard: retry passed")
        return {"reply": retry["reply"], "guard_report": second.as_dict(), "trace": trace}

    primary, secondary = composer.split(state["matched"], policy)
    fallback = composer.template_reply(
        state, primary, secondary, composer.relevant_numbers(state["matched"], state["window"], policy),
        override,
    )
    final = check_reply(fallback, ids, numbers)
    trace.append(
        f"guard: retry also FAILED ({'; '.join(second.problems)}) - "
        f"fell back to the deterministic template (template guard ok={final.ok})"
    )
    return {
        "reply": fallback,
        "guard_report": {**final.as_dict(), "fell_back": True, "first_failure": report.problems,
                         "retry_failure": second.problems},
        "trace": trace,
    }

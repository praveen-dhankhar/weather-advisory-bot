"""match node: intent + weather -> SOP ids, in three passes.

1. deterministic   - tag/audience/time-window filter, then the generic condition
                     evaluator over numbers from the snapshot. No LLM.
2. fuzzy           - the LLM judges ONLY the fuzzy SOPs that survived the tag
                     filter, and must cite the field values it used. Code then
                     re-checks those values against the snapshot and drops the
                     match if they do not line up. The user's message is NOT in
                     this prompt: a rubric is judged on weather, so user text has
                     no way to talk a verdict into existence.
3. situational     - derived signals, evaluated by the same code path.

Every id the LLM returns is checked against the loaded SOP set. Unknown ids are
dropped and logged, which is why "cite SOP-999" cannot work.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from backend import conditions, llm, weather
from backend.loader import Policy, get_policy
from backend.models import (
    SOP,
    GraphState,
    Intent,
    MatchedSOP,
    Severity,
    WeatherSnapshot,
    WindowValues,
)

FUZZY_SYSTEM = """JOB: fuzzy-match
You decide whether each candidate rubric is satisfied by the weather numbers you
are given. You do not write advice, you do not invent numbers, and you do not add
candidates.

Return ONE JSON object, no prose, no code fences:
  {"matches": [{"id": "<candidate id>", "apply": true|false,
                "fields": {"<field name>": <value you read from WEATHER>}}]}

Rules:
  - Use ONLY ids from CANDIDATES. Any other id is discarded.
  - "fields" must quote values exactly as they appear in WEATHER. They are checked
    against the source data; a mismatch voids your answer for that candidate.
  - "apply" is the JSON boolean true or false.
  - If a value a rubric needs is missing, answer apply: false for it."""


# --------------------------------------------------------------------------- #
# Pass 0: tag / audience filter
# --------------------------------------------------------------------------- #
def _tag_overlap(sop: SOP, intent: Intent) -> Optional[int]:
    """Number of overlapping tags, or None if this SOP does not apply at all."""
    if sop.applies_to is None:
        return 0
    wanted_tags = set(sop.applies_to.activity_tags_any or [])
    wanted_aud = set(sop.applies_to.audience_any or [])
    if wanted_aud and not wanted_aud & set(intent.audience):
        return None
    if not wanted_tags:
        return 0
    overlap = wanted_tags & set(intent.activity_tags)
    return len(overlap) if overlap else None


def candidates(intent: Intent, policy: Policy) -> list[SOP]:
    """SOPs whose applies_to block admits this intent. Order is id-stable."""
    out = []
    for sop in sorted(policy.sops.values(), key=lambda s: s.id):
        if _tag_overlap(sop, intent) is not None:
            out.append(sop)
    return out


# --------------------------------------------------------------------------- #
# Pass 1: deterministic numeric
# --------------------------------------------------------------------------- #
def _window_for(sop: SOP, snapshot: WeatherSnapshot, intent: Intent, policy: Policy) -> WindowValues:
    clamp = tuple(sop.time_window.hours) if sop.time_window else None
    return weather.resolve_window(snapshot, intent.time_window, policy, clamp_hours=clamp)


def match_numeric(
    sops: list[SOP], snapshot: WeatherSnapshot, intent: Intent, policy: Policy
) -> tuple[list[MatchedSOP], list[str]]:
    matched, notes = [], []
    for sop in sops:
        if sop.kind != "numeric":
            continue
        window = _window_for(sop, snapshot, intent, policy)
        if not window.times:
            notes.append(f"{sop.id}: skipped, its time_window does not overlap '{intent.time_window}'")
            continue
        result = conditions.evaluate(sop.conditions, window.values)
        if result.ok:
            matched.append(
                MatchedSOP(
                    sop_id=sop.id,
                    severity=sop.severity,
                    matched_conditions=result.matched,
                    matched_tags=_tag_overlap(sop, intent) or 0,
                    evidence={k: v for k, v in result.evidence.items() if v is not None},
                    via="numeric",
                )
            )
        elif result.missing:
            notes.append(f"{sop.id}: not evaluated, missing {sorted(set(result.missing))}")
    return matched, notes


# --------------------------------------------------------------------------- #
# Pass 2: fuzzy (LLM judges, code verifies)
# --------------------------------------------------------------------------- #
def _numbers_for_prompt(window: WindowValues, policy: Policy) -> dict[str, Any]:
    return {
        name: {"value": value, "unit": policy.unit(name)}
        for name, value in sorted(window.values.items())
        if value is not None
    }


def _value_agrees(claimed: Any, actual: Any) -> bool:
    if isinstance(actual, list):
        return sorted(conditions._as_list(claimed)) == sorted(actual)
    try:
        return abs(float(claimed) - float(actual)) <= max(0.51, abs(float(actual)) * 0.02)
    except (TypeError, ValueError):
        return str(claimed) == str(actual)


def match_fuzzy(
    sops: list[SOP],
    snapshot: WeatherSnapshot,
    intent: Intent,
    policy: Policy,
) -> tuple[list[MatchedSOP], list[str]]:
    fuzzy = [s for s in sops if s.kind == "fuzzy"]
    if not fuzzy:
        return [], []

    window = weather.resolve_window(snapshot, intent.time_window, policy)
    numbers = _numbers_for_prompt(window, policy)
    prompt = (
        "CANDIDATES:\n"
        + "\n".join(f"  - id: {s.id}\n    rubric: {s.fuzzy_criteria.strip()}" for s in fuzzy)
        + f"\n\nPERIOD: {window.label} ({len(window.times)} hourly slot(s))"
        + f"\n\nWEATHER (the only source of numbers):\n{json.dumps(numbers, indent=2)}\n\nJSON:"
    )

    notes: list[str] = []
    try:
        raw = llm.chat_json(FUZZY_SYSTEM, prompt)
    except llm.LLMError as exc:
        return [], [f"fuzzy pass skipped: {exc}"]

    allowed = {s.id: s for s in fuzzy}
    matched: list[MatchedSOP] = []
    for item in raw.get("matches") or []:
        if not isinstance(item, dict):
            continue
        sop_id = str(item.get("id", ""))
        if sop_id not in allowed:
            notes.append(f"fuzzy: dropped id {sop_id!r} - not a candidate for this question")
            continue
        if item.get("apply") is not True:  # "false", "yes", 1: not a verdict
            continue
        cited = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        bad = [
            name
            for name, value in cited.items()
            if name not in window.values or not _value_agrees(value, window.values[name])
        ]
        if bad or not cited:
            notes.append(
                f"fuzzy: dropped {sop_id} - cited field values do not match the snapshot "
                f"({bad or 'no fields cited'})"
            )
            continue
        sop = allowed[sop_id]
        matched.append(
            MatchedSOP(
                sop_id=sop_id,
                severity=sop.severity,
                matched_conditions=len(cited),
                matched_tags=_tag_overlap(sop, intent) or 0,
                evidence={k: window.values[k] for k in cited},
                via="fuzzy",
            )
        )
    return matched, notes


# --------------------------------------------------------------------------- #
# Pass 3: situational signals
# --------------------------------------------------------------------------- #
def match_situational(
    sops: list[SOP], snapshot: WeatherSnapshot, intent: Intent, policy: Policy
) -> tuple[list[MatchedSOP], list[str]]:
    matched, notes = [], []
    window = weather.resolve_window(snapshot, intent.time_window, policy)
    values = {**window.values, **snapshot.derived}
    for sop in sops:
        if sop.kind != "situational":
            continue
        result = conditions.evaluate(sop.signals, values)
        if result.ok:
            matched.append(
                MatchedSOP(
                    sop_id=sop.id,
                    severity=sop.severity,
                    matched_conditions=result.matched,
                    matched_tags=_tag_overlap(sop, intent) or 0,
                    evidence={k: v for k, v in result.evidence.items() if v is not None},
                    via="situational",
                )
            )
        elif result.missing:
            notes.append(f"{sop.id}: signals not evaluable, missing {sorted(set(result.missing))}")
    return matched, notes


# --------------------------------------------------------------------------- #
# Conflict rule
# --------------------------------------------------------------------------- #
def _is_audience_specific(sop: SOP) -> bool:
    """True when the SOP targets a named audience rather than the general population."""
    if sop.applies_to is None:
        return False
    targeted = set(sop.applies_to.audience_any or []) - {"general"}
    return bool(targeted)


def rank(matched: list[MatchedSOP], policy: Policy) -> list[MatchedSOP]:
    """Overrides first, then severity, then audience specificity, then condition
    specificity, then id for stability.

    Audience specificity sits above condition count on purpose: at equal severity,
    guidance written for the person actually going outside beats generic guidance.
    Full rationale and the rejected alternatives are in DECISIONS.md.
    """
    def key(m: MatchedSOP) -> tuple[Any, ...]:
        sop = policy.sops[m.sop_id]
        return (sop.overrides, m.severity.rank, _is_audience_specific(sop),
                m.matched_conditions, m.matched_tags, m.sop_id)

    return sorted(matched, key=key, reverse=True)


def run(state: GraphState) -> dict[str, Any]:
    policy = get_policy()
    intent: Intent = state["intent"]
    snapshot: WeatherSnapshot = state["weather"]
    trace = list(state.get("trace") or [])

    pool = candidates(intent, policy)
    trace.append(f"match: {len(pool)} candidate SOPs after tag/audience filter")

    numeric, notes_n = match_numeric(pool, snapshot, intent, policy)
    situational, notes_s = match_situational(pool, snapshot, intent, policy)
    fuzzy, notes_f = match_fuzzy(pool, snapshot, intent, policy)
    trace += notes_n + notes_s + notes_f

    all_matched = rank(numeric + fuzzy + situational, policy)

    # Refuse to REASSURE a child, an older adult or an animal on generic policy alone.
    # If the audience is not the general population and nothing written for them
    # matched, a low/info answer would imply "fine for them" on evidence that was
    # never about them - so say no SOP applies instead. A moderate-or-worse SOP is
    # always surfaced, whoever it was written for: a thunderstorm warning must never
    # be suppressed because no audience-specific rule happened to fire.
    vulnerable = [a for a in intent.audience if a != "general"]
    if vulnerable and all_matched:
        targeted = any(_is_audience_specific(policy.sops[m.sop_id]) for m in all_matched)
        worst = max(m.severity.rank for m in all_matched)
        if not targeted and worst < Severity.moderate.rank:
            trace.append(
                f"match: dropped {[m.sop_id for m in all_matched]} - only general-population "
                f"guidance matched for audience {vulnerable}, and reassuring them on that "
                f"basis is not supported by policy"
            )
            all_matched = []

    situational_ids = [m.sop_id for m in all_matched if policy.sops[m.sop_id].overrides]
    window = state.get("window") or weather.resolve_window(snapshot, intent.time_window, policy)

    trace.append(
        f"match: numeric={[m.sop_id for m in numeric]} fuzzy={[m.sop_id for m in fuzzy]} "
        f"situational={[m.sop_id for m in situational]}"
    )
    branch = "override" if situational_ids else ("compose" if all_matched else "no_match")
    return {
        "candidate_sops": [s.id for s in pool],
        "matched": all_matched,
        "matched_sop_ids": [m.sop_id for m in all_matched],
        "situational_ids": situational_ids,
        "window": window,
        "branch": branch,
        "trace": trace,
    }

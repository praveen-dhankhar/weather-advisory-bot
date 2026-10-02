"""composer node: approved SOP text -> a draft reply; :func:`finalize` makes it final.

What the model is shown: the surfaced SOPs' own advice, the place and period labels,
the canonical activity and audience, and the NAMES of the readings. What it is not
shown: the user's message (no injection path into the text that becomes the answer)
and any weather value (it cannot restate, round or convert a number it never saw).

A reading reaches the reply only as a placeholder such as ``{wind_speed_10m}``, which
:func:`finalize` replaces with the snapshot value, then appends a code-built footer:
the readings used and the SOPs applied. The guard checks the draft before that.
:func:`template_reply` is the deterministic draft used when the model is unavailable
or fails the guard twice.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from backend import llm
from backend.loader import Policy, get_policy
from backend.models import SOP, GraphState, MatchedSOP, WeatherSnapshot, WindowValues

MAX_SECONDARY = 2
MAX_NUMBERS = 10  # keep the evidence trail readable

CORE_FIELDS = ("temperature_2m", "apparent_temperature", "wind_speed_10m",
               "precipitation_probability", "uv_index")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")

COMPOSE_SYSTEM = """JOB: compose
You are the voice of an SOP-grounded outdoor-safety bot. You phrase approved
guidance. You never create it, and you never state a weather reading yourself.

HARD RULES
1. Every piece of advice must come from the APPROVED GUIDANCE below. Do not add a
   precaution, a threshold, a policy or a recommendation that is not written there,
   however sensible it seems.
2. Never type a weather reading. To mention one, write its placeholder from READINGS
   exactly, braces included, e.g. "winds of {wind_speed_10m}". The system replaces it
   with the measured value and unit. The only digits you may type are ones already
   written in the approved guidance text, and clock times exactly as PERIOD shows them.
3. Cite every id under ALLOWED CITATIONS in square brackets right after the advice it
   supports, the PRIMARY SOP first. Cite nothing else.
4. Lead with the primary SOP. Add each secondary SOP as one short sentence.
5. Never claim that a government, meteorological or civic authority has issued a
   warning, alert or advisory. You only have forecast data.

STYLE: direct, 3-6 sentences, plain prose, no headings, no bullet lists, no emoji.
Open by answering the question. Name the place and the period once."""

OVERRIDE_EXTRA = """
OVERRIDE MODE: a situational SOP is active. Open with what the forecast data shows
about the weather system itself and that it governs the answer, then give the
activity-specific advice. Say "the forecast data shows", never "a warning has been
issued"."""


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def pretty(name: str) -> str:
    """Field name -> plain English, for the deterministic reply text."""
    return name.replace("_2m", "").replace("_10m", "").replace("_", " ").strip()


def label(name: str, policy: Policy) -> str:
    """What a reading is called in the reply: a derived signal's description, else its name."""
    return str(policy.derived.get(name, {}).get("description") or pretty(name))


def with_unit(value: Any, unit: str) -> str:
    return f"{_fmt(value)}{'' if unit in ('', '%') else ' '}{unit}"


def split(matched: list[MatchedSOP], policy: Policy) -> tuple[SOP, list[SOP]]:
    """Primary = first after ranking; up to MAX_SECONDARY distinct others."""
    seen: set[str] = set()
    ordered: list[SOP] = []
    for m in matched:
        if m.sop_id not in seen:
            seen.add(m.sop_id)
            ordered.append(policy.sops[m.sop_id])
    return ordered[0], ordered[1 : 1 + MAX_SECONDARY]


def relevant_numbers(
    matched: list[MatchedSOP], window: WindowValues, policy: Policy
) -> dict[str, dict[str, Any]]:
    """The readings a reply may mention: evidence for the SURFACED SOPs plus a small
    core set, numeric only, all straight from the window."""
    primary, secondary = split(matched, policy)
    surfaced = {primary.id, *(s.id for s in secondary)}
    wanted: list[str] = []
    for m in matched:
        if m.sop_id in surfaced:
            wanted += [k for k in m.evidence if k not in wanted]
    wanted += [k for k in CORE_FIELDS if k not in wanted]
    usable = [name for name in wanted
              if isinstance(window.values.get(name), (int, float))
              and not isinstance(window.values.get(name), bool)]
    return {
        name: {"value": window.values[name], "unit": policy.unit(name)}
        for name in usable[:MAX_NUMBERS]
    }


def period_text(window: WindowValues) -> str:
    if not window.times:
        return window.label
    first, last = window.times[0][11:16], window.times[-1][11:16]
    return f"{window.label} ({first} local)" if first == last else f"{window.label} ({first}-{last} local)"


def _guidance_block(primary: SOP, secondary: list[SOP]) -> str:
    out = []
    for role, sop in [("PRIMARY", primary)] + [("SECONDARY", s) for s in secondary]:
        out += [f"{role} SOP {sop.id} (severity {sop.severity.value}) - {sop.title}",
                f"  advice: {' '.join(sop.advice.split())}"]
    return "\n".join(out)


def build_prompt(
    state: GraphState,
    primary: SOP,
    secondary: list[SOP],
    numbers: dict[str, dict[str, Any]],
    override: bool,
    stricter: bool = False,
) -> tuple[str, str]:
    snapshot: WeatherSnapshot = state["weather"]
    window: WindowValues = state["window"]
    intent = state["intent"]
    policy = get_policy()
    system = COMPOSE_SYSTEM + (OVERRIDE_EXTRA if override else "")
    if stricter:
        system += (
            "\n\nRETRY: your previous answer broke rule 2 or 3 - it typed a number, used an "
            "unknown placeholder, or cited ids wrongly. Write it again: readings only as "
            "placeholders, every allowed id cited once, the primary first."
        )
    facts = state.get("established_facts") or {}
    previous = ""
    if facts.get("last_window_label"):
        same = set(facts.get("last_sop_ids") or []) == {primary.id, *(s.id for s in secondary)}
        previous = (
            f"PREVIOUS TURN IN THIS SESSION: you already answered for "
            f"'{facts['last_window_label']}'. The guidance that applies now is "
            f"{'the same as then' if same else 'DIFFERENT from then'}. If it is different, say so "
            f"in one clause, and never contradict the earlier turn without naming the change.\n\n"
        )
    readings = "\n".join(
        f"  {{{name}}}: {label(name, policy)}" + (f" ({body['unit']})" if body["unit"] else "")
        for name, body in numbers.items()
    ) or "  (none)"
    # No user text and no weather value appears below - see the module docstring.
    user = (
        previous
        + f"PLACE: {snapshot.place.label}\n"
        f"PERIOD: {period_text(window)}\n"
        f"ACTIVITY: {intent.activity or 'unspecified'}\n"
        f"AUDIENCE: {', '.join(intent.audience)}\n\n"
        f"APPROVED GUIDANCE (the only source of advice):\n{_guidance_block(primary, secondary)}\n\n"
        f"READINGS (placeholders only - the system fills in the measured values):\n{readings}\n\n"
        f"ALLOWED CITATIONS: {', '.join([primary.id] + [s.id for s in secondary])}\n\nREPLY:"
    )
    return system, user


def template_reply(
    state: GraphState,
    primary: SOP,
    secondary: list[SOP],
    override: bool,
) -> str:
    """Deterministic draft built straight from SOP advice. Passes the guard by construction."""
    snapshot: WeatherSnapshot = state["weather"]
    window: WindowValues = state["window"]
    lead = "The forecast data shows a situation that governs this answer. " if override else ""
    parts = [f"{lead}For {snapshot.place.label}, {window.label}: {primary.advice.strip()} [{primary.id}]"]
    parts += [f"Also note: {sop.advice.strip()} [{sop.id}]" for sop in secondary]
    return "\n\n".join(parts)


def render(draft: str, numbers: dict[str, dict[str, Any]]) -> str:
    """Replace each {field} placeholder with the snapshot value and its unit."""
    def value(match: re.Match[str]) -> str:
        body = numbers.get(match.group(1))
        return with_unit(body["value"], body["unit"]) if body else match.group(0)

    return PLACEHOLDER_RE.sub(value, draft)


def finalize(draft: str, state: GraphState) -> str:
    """The reply the user sees: rendered draft + the code-built evidence footer.

    The footer is the audit trail - which readings, from where, for which period, and
    which SOPs - so "why did it say that?" is answered in the reply itself, and observed
    weather is never mixed up with policy text.
    """
    policy = get_policy()
    matched: list[MatchedSOP] = state["matched"]
    window: WindowValues = state["window"]
    snapshot: WeatherSnapshot = state["weather"]
    numbers = relevant_numbers(matched, window, policy)
    primary, secondary = split(matched, policy)
    parts = [render(draft, numbers).strip()]
    if numbers:
        readings = "; ".join(f"{label(n, policy)}: {with_unit(b['value'], b['unit'])}"
                             for n, b in numbers.items())
        parts.append(f"Readings used - Open-Meteo forecast for {snapshot.place.label}, "
                     f"{period_text(window)}: {readings}.")
    unavailable = sorted(name for name in window.missing if name in policy.fields)
    if unavailable:
        parts.append("Not available in the forecast for this period, so not checked: "
                     f"{', '.join(pretty(n) for n in unavailable)}.")
    parts.append("Guidance applied: " + "; ".join(s.cite_as for s in [primary, *secondary]) + ".")
    return "\n\n".join(parts)


def run(state: GraphState, override: Optional[bool] = None, stricter: bool = False) -> dict[str, Any]:
    policy = get_policy()
    matched: list[MatchedSOP] = state["matched"]
    override = bool(state.get("situational_ids")) if override is None else override
    primary, secondary = split(matched, policy)
    numbers = relevant_numbers(matched, state["window"], policy)
    trace = list(state.get("trace") or [])

    system, user = build_prompt(state, primary, secondary, numbers, override, stricter)
    try:
        draft = llm.chat(system, user, temperature=0.2).strip()
        trace.append(f"compose: llm reply, primary={primary.id}, secondary={[s.id for s in secondary]}")
    except llm.LLMError as exc:
        draft = template_reply(state, primary, secondary, override)
        trace.append(f"compose: LLM unavailable ({exc}); used the deterministic template")
    return {
        "draft": draft,
        "matched_sop_ids": [primary.id] + [s.id for s in secondary],
        "branch": "override" if override else "compose",
        "trace": trace,
    }


def run_override(state: GraphState) -> dict[str, Any]:
    return run(state, override=True)

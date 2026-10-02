"""A deterministic stand-in for the LLM.

Used when ``LLM_PROVIDER=fake`` and by the eval suite, so the graph can be
exercised end to end with no API key and no network. It is NOT a silent fallback:
nothing installs it unless the env var or a test asks for it.

It is deliberately dumb - keyword extraction for intake, the written rubric
re-expressed as a threshold for the fuzzy pass, and the deterministic template for
composing. Where it is weaker than a real model is listed in DECISIONS.md.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.loader import get_policy
from backend.nodes import composer
from backend.nodes.intake import resolve_activity

AUDIENCE_HINTS = {
    "child": ["kid", "child", "toddler", "baby", "infant", "son", "daughter", "children", "school"],
    "elderly": ["elderly", "grandmother", "grandfather", "grandma", "grandpa", "old age", "senior",
                "my mother", "my father", "aged parent"],
    "pet": ["dog", "puppy", "pet", "cat"],
}

WINDOW_HINTS = [
    ("tomorrow_morning", ["tomorrow morning"]),
    ("tomorrow_afternoon", ["tomorrow afternoon"]),
    ("tomorrow_evening", ["tomorrow evening", "tomorrow night"]),
    ("tomorrow", ["tomorrow"]),
    ("this_evening", ["this evening", "evening", "after work", "later today"]),
    ("tonight", ["tonight", "late night"]),
    ("this_morning", ["this morning", "morning"]),
    ("midday", ["midday", "noon", "lunchtime", "middle of the day"]),
    ("this_afternoon", ["this afternoon", "afternoon"]),
    ("today", ["today", "rest of the day"]),
]

OUTDOOR_HINTS = ["safe", "should i", "can i", "is it ok", "is it okay", "good day", "advisable",
                 "alright", "wise", "fine to", "what about", "and tomorrow", "instead",
                 "and for", "same for", "how about"]

LOCATION_RE = re.compile(
    r"\b(?:in|at|around|near|to|from|for)\s+([A-Z][a-zA-Z]+(?:[ -][A-Z][a-zA-Z]+)?)")
STOPWORDS = {"the", "work", "office", "school", "park", "home", "my", "me", "i"}


def _intake(user: str) -> str:
    policy = get_policy()
    block = re.search(r"<user_message>\n(.*?)\n</user_message>", user, re.DOTALL)
    message = block.group(1) if block else user
    lowered = message.lower()
    facts_block = re.search(r"ESTABLISHED FACTS FROM THIS SESSION \(use for follow-ups\): (\{.*?\})", user)
    facts = json.loads(facts_block.group(1)) if facts_block else {}

    activity = resolve_activity(policy, message)
    location = None
    for candidate in LOCATION_RE.findall(message):
        if candidate.lower() not in STOPWORDS:
            location = candidate
            break

    def has(phrase: str) -> bool:
        # whole-phrase match: "afternoon" must not match the "noon" hint
        return re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", lowered) is not None

    audience = [name for name, hints in AUDIENCE_HINTS.items() if any(has(h) for h in hints)]
    window = next((name for name, hints in WINDOW_HINTS if any(has(h) for h in hints)), "now")
    is_followup = bool(re.match(r"^\s*(what about|and|how about|ok but what about)\b", lowered)) or (
        activity is None and location is None and window != "now"
    )
    # a follow-up is a continuation of an outdoor question by definition
    outdoor = bool(activity) or is_followup or any(h in lowered for h in OUTDOOR_HINTS)

    return json.dumps(
        {
            "is_outdoor_safety_question": bool(outdoor and (activity or is_followup or facts)),
            "location": location,
            "activity_raw": message.strip()[:80] or None,
            "activity": activity,
            "activity_tags": sorted(policy.activities[activity]["tags"]) if activity else [],
            "audience": audience or ["general"],
            "time_window": window,
            "is_followup": is_followup,
        }
    )


def _fuzzy(user: str) -> str:
    """Re-express the written rubrics as thresholds over the supplied numbers."""
    weather_block = re.search(r"WEATHER \(the only source of numbers\):\n(\{.*?\n\})", user, re.DOTALL)
    numbers: dict[str, Any] = json.loads(weather_block.group(1)) if weather_block else {}

    def value(name: str) -> Any:
        return (numbers.get(name) or {}).get("value")

    ids = re.findall(r"- id: (SOP-[A-Z0-9]+-\d+)", user)
    # Thresholds mirror the fuzzy_criteria text in sops/outdoor_exercise.yaml.
    limits = {
        "SOP-EX-06": {"precipitation_probability": 30, "temp_lo": 14, "temp_hi": 30,
                      "wind_speed_10m": 30, "uv_index": 8, "temp_field": "apparent_temperature"},
        "SOP-EX-07": {"precipitation_probability": 20, "temp_lo": 18, "temp_hi": 30,
                      "wind_speed_10m": 25, "uv_index": 8, "temp_field": "temperature_2m"},
    }
    matches = []
    for sop_id in ids:
        rule = limits.get(sop_id)
        if rule is None:
            matches.append({"id": sop_id, "apply": False, "fields": {}})
            continue
        temp_field = rule["temp_field"]
        used = {
            name: value(name)
            for name in (temp_field, "precipitation_probability", "wind_speed_10m",
                         "uv_index", "precipitation")
            if value(name) is not None
        }
        temp = value(temp_field)
        ok = (
            temp is not None
            and rule["temp_lo"] <= temp <= rule["temp_hi"]
            and (value("precipitation_probability") or 0) < rule["precipitation_probability"]
            and (value("precipitation") or 0) <= 0.2
            and (value("wind_speed_10m") or 0) < rule["wind_speed_10m"]
            and (value("uv_index") or 0) < rule["uv_index"]
        )
        matches.append({"id": sop_id, "apply": bool(ok), "fields": used})
    return json.dumps({"matches": matches})


def _compose(user: str) -> str:
    """Phrase the approved guidance with no paraphrasing at all."""
    place = re.search(r"PLACE: (.*)", user)
    period = re.search(r"PERIOD: (.*?) \(local", user)
    advice = re.findall(r"  advice: (.*?)(?=\n(?:PRIMARY|SECONDARY|\n)|\nWEATHER NUMBERS)", user, re.DOTALL)
    ids = re.findall(r"(?:PRIMARY|SECONDARY) SOP (SOP-[A-Z0-9]+-\d+)", user)
    numbers_block = re.search(r"WEATHER NUMBERS \(the only source of figures\):\n(\{.*?\n\})", user, re.DOTALL)
    numbers: dict[str, Any] = json.loads(numbers_block.group(1)) if numbers_block else {}
    override = "OVERRIDE MODE" in user

    parts = []
    if override:
        parts.append("The forecast data shows a weather system that governs this answer.")
    head = f"For {place.group(1).strip() if place else 'this location'}, {period.group(1).strip() if period else 'now'}:"
    for index, (sop_id, text) in enumerate(zip(ids, advice)):
        body = " ".join(text.split())
        parts.append(f"{head} {body} [{sop_id}]" if index == 0 else f"Also note: {body} [{sop_id}]")
    readings = composer.readings_line(numbers)
    if readings:
        parts.append(readings)
    return "\n\n".join(parts)


def fake_llm(system: str, user: str) -> str:
    """Dispatch on the JOB: tag that every prompt in this repo starts with."""
    job = system.splitlines()[0].replace("JOB:", "").strip()
    if job == "intake":
        return _intake(user)
    if job == "fuzzy-match":
        return _fuzzy(user)
    if job == "compose":
        return _compose(user)
    raise AssertionError(f"fake_llm has no behaviour for JOB: {job!r}")

r"""The LangGraph: nodes, conditional edges, four genuinely different endings.

    START -> intake -> location -> weather -> match -> {override|compose} -> guard -> END
                 |         |          |         |
                 |         |          |         +--> no_match_node (fixed text)
                 +---------+----------+------------> failure_node (fixed text)
                 +--> no_match_node (fixed text)
                 +---------+-----------------------> clarify_node (fixed text)

`python -m backend.graph "is it safe to cycle in Pune this evening?"` runs it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from backend import guards, llm, weather
from backend.loader import get_policy
from backend.memory import MEMORY, Memory
from backend.models import (
    GraphState,
    LocationAmbiguous,
    LocationUnresolved,
    WeatherSnapshot,
    WeatherUnavailable,
)
from backend.nodes import composer, fixed, intake, matcher

# One logger for the whole graph. Answers "why did it say that" after the fact:
# branch taken, SOPs cited, place and snapshot timestamp, guard verdict.
log = logging.getLogger("advisory")


def install_llm() -> None:
    """LLM_PROVIDER=fake swaps in the deterministic stand-in (see fake_llm.py)."""
    if os.getenv("LLM_PROVIDER", "").strip().lower() == "fake":
        from backend.fake_llm import fake_llm

        llm.set_fake(fake_llm)


# --------------------------------------------------------------------------- #
# Nodes that only wrap deterministic code
# --------------------------------------------------------------------------- #
def location_node(state: GraphState) -> dict[str, Any]:
    trace = list(state.get("trace") or [])
    name = state["intent"].location or ""
    try:
        place = weather.geocode(name)
    except LocationAmbiguous as exc:
        trace.append(f"location: AMBIGUOUS {name!r} -> {exc.candidates}; asking instead of guessing")
        return {
            "branch": "clarify",
            "clarify_question": fixed.AMBIGUOUS_TEXT.format(
                query=name, candidates="; ".join(exc.candidates)
            ),
            "trace": trace,
        }
    except (LocationUnresolved, WeatherUnavailable) as exc:
        trace.append(f"location: FAILED for {name!r} ({type(exc).__name__}: {exc})")
        return {"branch": "fail", "error": str(exc), "failure": "weather", "trace": trace}
    trace.append(f"location: {name!r} -> {place.label} ({place.latitude}, {place.longitude})")
    return {"location": place, "trace": trace}


def weather_node(state: GraphState) -> dict[str, Any]:
    """Fetch a fresh snapshot every turn - a follow-up never reuses an earlier one - and
    refuse to go on when the asked-about period has no usable hours in it."""
    trace = list(state.get("trace") or [])
    place = state["location"]
    policy = get_policy()
    try:
        snapshot: WeatherSnapshot = weather.fetch_forecast(
            place.latitude, place.longitude, place, policy
        )
    except (WeatherUnavailable, LocationUnresolved) as exc:
        trace.append(f"weather: FAILED ({type(exc).__name__}: {exc})")
        return {"branch": "fail", "error": str(exc), "failure": "weather", "trace": trace}

    window = weather.resolve_window(snapshot, state["intent"].time_window, policy)
    reason = None
    if not window.times:
        reason = (f"the forecast has no hours left for {window.label} - that period has "
                  f"already passed or is beyond the forecast range")
    elif all(window.values.get(name) is None for name in policy.fields):
        reason = f"the weather service returned no usable values for {window.label}"
    if reason:
        trace.append(f"weather: FAILED ({reason})")
        return {"branch": "fail", "error": reason, "failure": "weather", "trace": trace}
    trace.append(f"weather: snapshot at {snapshot.current_time} ({snapshot.place.timezone}), "
                 f"{len(window.times)} slot(s) for {window.label}")
    return {"weather": snapshot, "window": window, "trace": trace}


# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #
def route_after_intake(state: GraphState) -> str:
    if state.get("branch") == "fail":
        return "failure"
    intent = state.get("intent")
    if intent is None or not intent.is_outdoor_safety_question:
        return "no_match"
    if intent.activity is None:
        # No vocabulary entry for this activity: there can be no SOP for it.
        return "no_match"
    if state.get("branch") == "clarify" or not intent.location:
        return "clarify"  # an unsupported period, or no place anywhere in the session
    return "location"


def route_after_location(state: GraphState) -> str:
    branch = state.get("branch")
    if branch == "fail":
        return "failure"
    if branch == "clarify":  # same-named places in different countries
        return "clarify"
    return "weather"


def route_after_weather(state: GraphState) -> str:
    return "failure" if state.get("branch") == "fail" else "match"


def route_after_match(state: GraphState) -> str:
    """match only ever produces these three. Weather/location failures never reach it."""
    branch = state.get("branch") or "no_match"
    return {"override": "override", "compose": "compose", "no_match": "no_match"}.get(
        branch, "no_match"
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def build_graph():
    builder = StateGraph(GraphState)
    builder.add_node("intake", intake.run)
    builder.add_node("location", location_node)
    builder.add_node("weather", weather_node)
    builder.add_node("match", matcher.run)
    builder.add_node("compose", composer.run)
    builder.add_node("override", composer.run_override)
    builder.add_node("guard", guards.run)
    builder.add_node("no_match", fixed.no_match_node)
    builder.add_node("failure", fixed.failure_node)
    builder.add_node("clarify", fixed.clarify_node)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake,
                                 ["location", "no_match", "clarify", "failure"])
    builder.add_conditional_edges("location", route_after_location,
                                 ["weather", "clarify", "failure"])
    builder.add_conditional_edges("weather", route_after_weather, ["match", "failure"])
    builder.add_conditional_edges("match", route_after_match,
                                 ["compose", "override", "no_match"])
    builder.add_edge("compose", "guard")
    builder.add_edge("override", "guard")
    for terminal in ("guard", "no_match", "failure", "clarify"):
        builder.add_edge(terminal, END)
    return builder.compile()


GRAPH = build_graph()


def answer(
    message: str,
    session_id: str = "cli",
    memory: Optional[Memory] = None,
    graph: Any = None,
) -> dict[str, Any]:
    """Run one turn and update session memory. Returns the raw final state."""
    memory = memory or MEMORY
    graph = graph or GRAPH
    session = memory.get(session_id)
    with session.lock:  # two requests on one session run one after the other, never interleaved
        state: GraphState = {
            "session_id": session_id,
            "user_message": message,
            "history": list(session.history),
            "established_facts": dict(session.facts),
            "trace": [],
        }
        final = graph.invoke(state)
        session.add_turn("user", message)
        session.add_turn("assistant", final.get("reply", ""))
        memory.record(session, final)

    snapshot = final.get("weather")
    guard = final.get("guard_report") or {}
    log.info(
        "session=%s branch=%s sops=%s place=%s observed_at=%s window=%s guard_ok=%s%s",
        session_id,
        final.get("branch"),
        ",".join(final.get("matched_sop_ids") or []) or "-",
        snapshot.place.label if snapshot else "-",
        snapshot.current_time if snapshot else "-",
        final["window"].window if final.get("window") else "-",
        guard.get("ok", "-"),
        " FELL_BACK_TO_TEMPLATE" if guard.get("fell_back") else "",
    )
    for line in final.get("trace") or []:
        if "FAILED" in line or "dropped" in line or "AMBIGUOUS" in line:
            log.warning("session=%s %s", session_id, line)
    return final


def facts_for_api(final: dict[str, Any]) -> dict[str, Any]:
    """The numbers actually used, for the /chat response and the UI."""
    window = final.get("window")
    snapshot = final.get("weather")
    return {
        "source": "Open-Meteo forecast API" if snapshot else None,
        "place": snapshot.place.label if snapshot else None,
        "timezone": snapshot.place.timezone if snapshot else None,
        "observed_at": snapshot.current_time if snapshot else None,
        "fetched_at": snapshot.fetched_at if snapshot else None,
        "period": window.label if window else None,
        "hours_used": window.times if window else [],
        "values": {k: v for k, v in (window.values if window else {}).items() if v is not None},
        "missing_fields": window.missing if window else [],
    }


if __name__ == "__main__":
    import sys

    install_llm()
    messages = sys.argv[1:] or ["is it safe to cycle in Bhopal today?"]
    for turn, text in enumerate(messages, 1):
        result = answer(text, session_id="cli")
        print(f"\n=== turn {turn}: {text}")
        print(f"branch={result.get('branch')}  sops={result.get('matched_sop_ids')}")
        print(result.get("reply", ""))
        print("--- trace")
        for line in result.get("trace", []):
            print("   ", line)

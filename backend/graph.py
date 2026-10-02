r"""The LangGraph: nodes, conditional edges, four genuinely different endings.

    START -> intake -> location -> weather -> match -> {override|compose} -> guard -> END
                 |         |          |         |
                 |         +----------+---------+--> failure_node (fixed text)
                 +--> no_match_node (fixed text)   \--> no_match_node
                 +--> clarify_node (fixed text)

`python -m backend.graph "is it safe to cycle in Pune this evening?"` runs it.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from backend import guards, llm, weather
from backend.loader import get_policy
from backend.memory import MEMORY, Memory
from backend.models import (
    GraphState,
    LocationUnresolved,
    WeatherSnapshot,
    WeatherUnavailable,
)
from backend.nodes import composer, fixed, intake, matcher


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
    except (LocationUnresolved, WeatherUnavailable) as exc:
        trace.append(f"location: FAILED for {name!r} ({type(exc).__name__}: {exc})")
        return {"branch": "fail", "error": str(exc), "trace": trace}
    trace.append(f"location: {name!r} -> {place.label} ({place.latitude}, {place.longitude})")
    return {"location": place, "trace": trace}


def weather_node(state: GraphState) -> dict[str, Any]:
    trace = list(state.get("trace") or [])
    place = state["location"]
    try:
        snapshot: WeatherSnapshot = weather.fetch_forecast(
            place.latitude, place.longitude, place, get_policy()
        )
    except (WeatherUnavailable, LocationUnresolved) as exc:
        trace.append(f"weather: FAILED ({type(exc).__name__}: {exc})")
        return {"branch": "fail", "error": str(exc), "trace": trace}
    trace.append(f"weather: snapshot at {snapshot.current_time} ({snapshot.place.timezone})")
    return {"weather": snapshot, "trace": trace}


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
    if not intent.location:
        return "clarify"
    return "location"


def route_after_location(state: GraphState) -> str:
    return "failure" if state.get("branch") == "fail" else "weather"


def route_after_weather(state: GraphState) -> str:
    return "failure" if state.get("branch") == "fail" else "match"


def route_after_match(state: GraphState) -> str:
    branch = state.get("branch") or "no_match"
    return {"fail": "failure", "override": "override", "compose": "compose",
            "no_match": "no_match"}.get(branch, "no_match")


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
    builder.add_conditional_edges("location", route_after_location, ["weather", "failure"])
    builder.add_conditional_edges("weather", route_after_weather, ["match", "failure"])
    builder.add_conditional_edges("match", route_after_match,
                                 ["compose", "override", "no_match", "failure"])
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
    memory.record(session_id, final)
    return final


def facts_for_api(final: dict[str, Any]) -> dict[str, Any]:
    """The numbers actually used, for the /chat response and the UI."""
    window = final.get("window")
    snapshot = final.get("weather")
    return {
        "place": snapshot.place.label if snapshot else None,
        "timezone": snapshot.place.timezone if snapshot else None,
        "observed_at": snapshot.current_time if snapshot else None,
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

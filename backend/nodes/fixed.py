"""Fixed-text nodes. No LLM is called here, by design.

When the bot has nothing to stand on - a dead API, an unknown place, no matching
SOP - the words are written by a human in this file. A language model asked to
phrase an apology will eventually phrase advice with it.
"""

from __future__ import annotations

from typing import Any

from backend.models import GraphState

NO_SOP_SENTENCE = "No SOP applies to this question, so I have no guidance to give."

FAILURE_TEXT = (
    "I could not get the forecast data I need, so I am not going to answer. "
    "No standard operating procedure was applied and no weather figures are "
    "available for this request. Reason: {reason} "
    "Please try again shortly, or check your local forecast directly."
)

# A failure BEFORE the forecast is attempted must not blame the forecast...
FAILURE_TEXT_INTAKE = (
    "I could not understand that request well enough to act on it, so I am not "
    "going to answer rather than guess. No weather data was requested and no "
    "standard operating procedure was applied. Reason: {reason} "
    "Please rephrase it as an outdoor-activity question naming the place, for "
    'example "is it safe to cycle in Pune this evening?"'
)

# ...and an outage of the language model is not the user's wording either.
FAILURE_TEXT_LLM = (
    "I cannot answer right now: the language service I use to read questions is "
    "unavailable. No weather data was requested and no standard operating procedure "
    "was applied, so nothing in this message is advice. Please try again shortly."
)

FAILURE_TEXTS = {"weather": FAILURE_TEXT, "intake": FAILURE_TEXT_INTAKE, "llm": FAILURE_TEXT_LLM}

NO_MATCH_TEXT = (
    NO_SOP_SENTENCE + " I only answer outdoor-activity safety questions that one of "
    "my written procedures covers, and I will not improvise advice outside them. "
    "Try an activity such as cycling, walking, running, a park visit, a picnic, a "
    "dog walk or a commute, and name the place - for example "
    '"is it safe to cycle in Pune this evening?"'
)

CLARIFY_TEXT = (
    "Which place should I check? I do not guess a location, because the advice "
    "depends entirely on the forecast for the right one. Tell me the city or town "
    "and I will look it up. No standard operating procedure was applied to this "
    "message."
)

AMBIGUOUS_TEXT = (
    "I am not sure which {query} you mean, and I will not guess - the forecast for "
    "the wrong one would be worse than no answer. The closest matches I can see "
    "are: {candidates}. Tell me which, or give a nearby larger city. No standard "
    "operating procedure was applied to this message."
)

# Turn caps on a public deployment (backend/memory.py::Memory.admit). Checked before
# the graph runs, so these turns cost no model call and no weather call.
SESSION_LIMIT_TEXT = (
    "This conversation has reached its question limit, so I am not answering this one. "
    "No weather data was requested and no standard operating procedure was applied. "
    "Start a new session to keep going."
)

BUSY_TEXT = (
    "The bot has reached its question limit for this hour, so I am not answering right "
    "now. No weather data was requested and no standard operating procedure was "
    "applied. Please try again later."
)

PERIOD_TEXT = (
    "Which period should I check? I can only advise on {periods}, because those are "
    "the hours the forecast covers, and I will not stretch it to a time it does not. "
    "No standard operating procedure was applied to this message."
)


def failure_node(state: GraphState) -> dict[str, Any]:
    """Honest failure text. Which template is used depends on what actually failed."""
    reason = state.get("error") or "the weather service could not be reached."
    template = FAILURE_TEXTS[state.get("failure") or "weather"]
    trace = list(state.get("trace") or []) + ["failure_node: fixed text, no LLM call"]
    return {
        "reply": template.format(reason=str(reason).rstrip(".") + "."),
        "branch": "fail",
        "matched_sop_ids": [],
        "trace": trace,
    }


def no_match_node(state: GraphState) -> dict[str, Any]:
    trace = list(state.get("trace") or []) + ["no_match_node: fixed text, no LLM call"]
    return {"reply": NO_MATCH_TEXT, "branch": "no_match", "matched_sop_ids": [], "trace": trace}


def clarify_node(state: GraphState) -> dict[str, Any]:
    """Ask for the one thing missing. Wording is fixed; only the names are filled in."""
    trace = list(state.get("trace") or []) + ["clarify_node: fixed text, no LLM call"]
    return {
        "reply": state.get("clarify_question") or CLARIFY_TEXT,
        "branch": "clarify",
        "matched_sop_ids": [],
        "trace": trace,
    }

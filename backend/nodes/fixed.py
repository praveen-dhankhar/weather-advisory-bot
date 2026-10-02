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
    "and I will look it up."
)


def failure_node(state: GraphState) -> dict[str, Any]:
    reason = state.get("error") or "the weather service could not be reached."
    trace = list(state.get("trace") or []) + ["failure_node: fixed text, no LLM call"]
    return {
        "reply": FAILURE_TEXT.format(reason=str(reason).rstrip(".") + "."),
        "branch": "fail",
        "matched_sop_ids": [],
        "trace": trace,
    }


def no_match_node(state: GraphState) -> dict[str, Any]:
    trace = list(state.get("trace") or []) + ["no_match_node: fixed text, no LLM call"]
    return {"reply": NO_MATCH_TEXT, "branch": "no_match", "matched_sop_ids": [], "trace": trace}


def clarify_node(state: GraphState) -> dict[str, Any]:
    trace = list(state.get("trace") or []) + ["clarify_node: fixed text, no LLM call"]
    return {"reply": CLARIFY_TEXT, "branch": "clarify", "matched_sop_ids": [], "trace": trace}

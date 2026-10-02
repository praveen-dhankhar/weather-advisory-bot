"""FastAPI surface: POST /chat.

The policy is loaded at import time, so a malformed SOP file stops the process
with the file name instead of quietly disabling a rule.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from backend.graph import GRAPH, answer, facts_for_api, install_llm
from backend.loader import get_policy

# The per-turn decision log ("advisory" logger: branch, SOPs, place, guard verdict)
# is INFO; without this it would be emitted and dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

POLICY = get_policy()  # fail loudly at startup
install_llm()

app = FastAPI(
    title="SOP-grounded weather advisory bot",
    description="Answers outdoor-activity safety questions from written SOPs only.",
    version="1.0.0",
)


MAX_MESSAGE_CHARS = 8000


class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,120}$",
        description="Omit to start a new session; reuse the session_id from the response to continue it.",
    )
    message: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description=(
            f"Up to {MAX_MESSAGE_CHARS} characters; longer is rejected with 422. "
            "Long messages are truncated head-and-tail before reaching a prompt "
            "(backend/llm.py::untrusted_block)."
        ),
    )

    @field_validator("message")
    @classmethod
    def _has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message is blank")
        return value


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    sop_ids: list[str]
    branch: Optional[str]
    facts: dict[str, Any]
    trace: list[str]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "sops": len(POLICY.sops), "fields": len(POLICY.fields)}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    # A missing id gets a fresh session - never a shared "default" one that would leak
    # one caller's place and activity into another caller's follow-up.
    session_id = request.session_id or uuid.uuid4().hex
    final = answer(request.message, session_id=session_id, graph=GRAPH)
    return ChatResponse(
        session_id=session_id,
        reply=final.get("reply", ""),
        sop_ids=list(final.get("matched_sop_ids") or []),
        branch=final.get("branch"),
        facts=facts_for_api(final),
        trace=list(final.get("trace") or []),
    )

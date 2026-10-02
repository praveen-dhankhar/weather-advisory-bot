"""FastAPI surface: POST /chat.

The policy is loaded at import time, so a malformed SOP file stops the process
with the file name instead of quietly disabling a rule.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from backend.graph import GRAPH, answer, facts_for_api, install_llm
from backend.loader import get_policy

POLICY = get_policy()  # fail loudly at startup
install_llm()

app = FastAPI(
    title="SOP-grounded weather advisory bot",
    description="Answers outdoor-activity safety questions from written SOPs only.",
    version="1.0.0",
)


class ChatRequest(BaseModel):
    session_id: str = Field(default="default", min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
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
    final = answer(request.message, session_id=request.session_id, graph=GRAPH)
    return ChatResponse(
        reply=final.get("reply", ""),
        sop_ids=list(final.get("matched_sop_ids") or []),
        branch=final.get("branch"),
        facts=facts_for_api(final),
        trace=list(final.get("trace") or []),
    )

"""Streamlit chat frontend.

Talks to the FastAPI backend over HTTP by default. Set EMBEDDED=1 to run the graph
in the Streamlit process instead, which is how this deploys to a single URL on
Streamlit Community Cloud.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
EMBEDDED = os.getenv("EMBEDDED", "0").strip() in {"1", "true", "yes"}

st.set_page_config(page_title="Outdoor safety advisor", page_icon="🌦", layout="centered")
st.title("Outdoor safety advisor")
st.caption(
    "Every answer comes from a written SOP and cites it. Live Open-Meteo data. "
    "If no SOP covers your question, the bot says so instead of guessing."
)

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.turns = []


def ask(message: str) -> dict:
    if EMBEDDED:
        from backend.graph import answer, facts_for_api, install_llm

        install_llm()
        final = answer(message, session_id=st.session_state.session_id)
        return {
            "reply": final.get("reply", ""),
            "sop_ids": final.get("matched_sop_ids") or [],
            "branch": final.get("branch"),
            "facts": facts_for_api(final),
            "trace": final.get("trace") or [],
        }
    response = httpx.post(
        f"{BACKEND_URL}/chat",
        json={"session_id": st.session_state.session_id, "message": message},
        timeout=90.0,
    )
    response.raise_for_status()
    return response.json()


with st.sidebar:
    st.subheader("Session")
    st.code(st.session_state.session_id, language=None)
    st.write(f"Mode: {'embedded graph' if EMBEDDED else BACKEND_URL}")
    if st.button("New session"):
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.turns = []
        st.rerun()
    st.markdown(
        "**Try**\n"
        "- is it safe to cycle to work in Pune today?\n"
        "- what about this evening instead?\n"
        "- should I take my toddler to the park in Jaipur this afternoon?\n"
        "- good day for a picnic in Goa tomorrow?\n"
        "- is it safe to go scuba diving?"
    )

for turn in st.session_state.turns:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])
        if turn.get("payload"):
            payload = turn["payload"]
            badge = ", ".join(payload["sop_ids"]) or "no SOP applied"
            st.caption(f"branch: `{payload['branch']}` · cited: {badge}")
            with st.expander("Data and trace"):
                st.json(payload["facts"])
                st.code("\n".join(payload["trace"]) or "(no trace)", language=None)

prompt = st.chat_input("Ask about an outdoor activity, and name the place")
if prompt:
    st.session_state.turns.append({"role": "user", "content": prompt})
    try:
        payload = ask(prompt)
    except Exception as exc:  # the UI must never die on a backend hiccup
        payload = {
            "reply": f"The backend could not be reached: {exc}",
            "sop_ids": [], "branch": "fail", "facts": {}, "trace": [],
        }
    st.session_state.turns.append(
        {"role": "assistant", "content": payload["reply"], "payload": payload}
    )
    st.rerun()

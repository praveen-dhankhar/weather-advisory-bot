"""Per-session memory. A dict in the process, nothing else.

Holds the last N turns plus the structured facts the intake node needs so that
"what about this evening?" keeps the location and the activity. Resets on restart;
there is deliberately no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

MAX_TURNS = 8


@dataclass
class Session:
    session_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def add_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        del self.history[:-MAX_TURNS]


class Memory:
    """In-memory session store keyed by session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str) -> Session:
        return self._sessions.setdefault(session_id, Session(session_id=session_id))

    def record(self, session_id: str, state: dict[str, Any]) -> None:
        """Save what the next turn needs: location, activity, audience, window,
        the SOPs that fired and the branch taken."""
        session = self.get(session_id)
        intent = state.get("intent")
        if intent is not None:
            session.facts.update(
                {
                    "location": intent.location or session.facts.get("location"),
                    "activity": intent.activity or session.facts.get("activity"),
                    "activity_tags": intent.activity_tags or session.facts.get("activity_tags"),
                    "audience": intent.audience,
                    "time_window": intent.time_window,
                }
            )
        window = state.get("window")
        session.facts.update(
            {
                "last_sop_ids": list(state.get("matched_sop_ids") or []),
                "last_branch": state.get("branch"),
                "last_window_label": window.label if window else session.facts.get("last_window_label"),
                "last_numbers": dict(window.values) if window else None,
            }
        )

    def reset(self, session_id: Optional[str] = None) -> None:
        if session_id is None:
            self._sessions.clear()
        else:
            self._sessions.pop(session_id, None)


MEMORY = Memory()

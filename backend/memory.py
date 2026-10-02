"""Per-session memory. A dict in the process, nothing else.

Holds the last N turns plus the structured facts the intake node needs so that
"what about this evening?" keeps the location and the activity. Resets on restart;
there is deliberately no persistence. Weather is never kept here: every turn
fetches a fresh snapshot, so a follow-up cannot be answered from stale numbers.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_TURNS = 8
# ponytail: in-process LRU, single worker only; a shared store (e.g. Redis) if this
# ever runs with several workers or must survive a restart.
MAX_SESSIONS = 500


@dataclass
class Session:
    session_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        del self.history[:-MAX_TURNS]


class Memory:
    """In-memory session store keyed by session_id, least-recently-used evicted first."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._sessions[session_id] = Session(session_id=session_id)
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(session_id)
            return session

    @staticmethod
    def record(session: Session, state: dict[str, Any]) -> None:
        """Save what the next turn needs: location, activity, audience, window,
        the SOPs that fired and the branch taken."""
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
            }
        )

    def reset(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id is None:
                self._sessions.clear()
            else:
                self._sessions.pop(session_id, None)


MEMORY = Memory()

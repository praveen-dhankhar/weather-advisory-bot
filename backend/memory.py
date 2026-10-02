"""Per-session memory. A dict in the process, nothing else.

Holds the last N turns plus the structured facts the intake node needs so that
"what about this evening?" keeps the location and the activity. Resets on restart;
there is deliberately no persistence. Weather is never kept here: every turn
fetches a fresh snapshot, so a follow-up cannot be answered from stale numbers.

It also holds the turn caps that protect a public deployment's model key and the
Open-Meteo quota: MAX_TURNS_PER_SESSION and MAX_TURNS_PER_HOUR (unset or 0 = no cap).
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Optional

from backend.nodes import fixed

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
    turns_used: int = 0

    def add_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        del self.history[:-MAX_TURNS]


class Memory:
    """In-memory session store keyed by session_id, least-recently-used evicted first."""

    def __init__(self, max_sessions: int = MAX_SESSIONS, per_session: int = 0, per_hour: int = 0) -> None:
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.per_session, self.per_hour = per_session, per_hour
        self._recent: deque[float] = deque()  # admission times within the last hour

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

    def admit(self, session: Session) -> Optional[str]:
        """Count this turn against the caps, or return the fixed text that refuses it.
        Runs before the graph, so a refused turn costs no model or weather call."""
        now = monotonic()
        with self._lock:
            while self._recent and now - self._recent[0] >= 3600:
                self._recent.popleft()
            if self.per_hour and len(self._recent) >= self.per_hour:
                return fixed.BUSY_TEXT
            if self.per_session and session.turns_used >= self.per_session:
                return fixed.SESSION_LIMIT_TEXT
            self._recent.append(now)
            session.turns_used += 1
        return None

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


def _cap(name: str) -> int:
    """A turn cap from the environment. Unset or 0 means no cap; a bad value fails at import."""
    value = int(os.getenv(name) or 0)
    if value < 0:
        raise ValueError(f"{name} must be 0 (no cap) or a positive number, got {value}")
    return value


MEMORY = Memory(per_session=_cap("MAX_TURNS_PER_SESSION"), per_hour=_cap("MAX_TURNS_PER_HOUR"))

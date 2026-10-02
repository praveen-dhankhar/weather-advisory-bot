"""Pydantic models and typed errors shared by the whole bot.

Nothing here talks to a network or an LLM. The important invariant: every number
that ever reaches the user originates in a :class:`WeatherSnapshot`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --------------------------------------------------------------------------- #
# Typed errors. Both weather failures route to the same graph branch.
# --------------------------------------------------------------------------- #
class LocationUnresolved(Exception):
    """Geocoding returned nothing usable for the requested place name."""


class LocationAmbiguous(Exception):
    """Geocoding returned candidates that are not clearly the place asked for.

    Carries the candidate labels so the bot can ask which one was meant instead of
    silently picking one in another country.
    """

    def __init__(self, query: str, candidates: list[str]) -> None:
        self.query = query
        self.candidates = candidates
        super().__init__(f"{query!r} could be {', '.join(candidates)}")


class WeatherUnavailable(Exception):
    """Open-Meteo could not be reached, or returned an unusable payload."""


class SOPConfigError(Exception):
    """An SOP / vocabulary / field file is invalid. Raised at startup."""


# --------------------------------------------------------------------------- #
# SOP schema
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    info = "info"
    low = "low"
    moderate = "moderate"
    high = "high"
    critical = "critical"

    @property
    def rank(self) -> int:
        return SEVERITY_RANK[self.value]


SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "low": 1,
    "moderate": 2,
    "high": 3,
    "critical": 4,
}

SOPKind = Literal["numeric", "fuzzy", "situational"]


class AppliesTo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_tags_any: Optional[list[str]] = None
    audience_any: Optional[list[str]] = None


class SOPTimeWindow(BaseModel):
    """Hours of the local day (inclusive) in which the SOP can apply at all."""

    model_config = ConfigDict(extra="forbid")

    hours: tuple[int, int]

    @model_validator(mode="after")
    def _ordered(self) -> "SOPTimeWindow":
        lo, hi = self.hours
        if not (0 <= lo <= hi <= 23):
            raise ValueError(f"time_window.hours must be 0..23 and ordered, got {self.hours}")
        return self


class SOP(BaseModel):
    """One approved piece of guidance. Policy, authored as data."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^SOP-[A-Z0-9]+-\d+$")
    category: str
    severity: Severity
    title: str
    kind: SOPKind
    advice: str
    cite_as: str
    applies_to: Optional[AppliesTo] = None
    time_window: Optional[SOPTimeWindow] = None
    conditions: Optional[dict[str, Any]] = None
    fuzzy_criteria: Optional[str] = None
    signals: Optional[dict[str, Any]] = None
    overrides: bool = False
    source_file: str = ""  # filled in by the loader, for error messages

    @model_validator(mode="after")
    def _kind_matches_payload(self) -> "SOP":
        if self.kind == "numeric" and not self.conditions:
            raise ValueError("kind=numeric requires `conditions`")
        if self.kind == "fuzzy" and not self.fuzzy_criteria:
            raise ValueError("kind=fuzzy requires `fuzzy_criteria`")
        if self.kind == "situational" and not self.signals:
            raise ValueError("kind=situational requires `signals`")
        if self.kind != "numeric" and self.conditions:
            raise ValueError("`conditions` is only valid for kind=numeric")
        if self.kind != "fuzzy" and self.fuzzy_criteria:
            raise ValueError("`fuzzy_criteria` is only valid for kind=fuzzy")
        if self.kind != "situational" and self.signals:
            raise ValueError("`signals` is only valid for kind=situational")
        if self.overrides and self.kind != "situational":
            raise ValueError("only situational SOPs may set overrides: true")
        return self

    @property
    def rule(self) -> dict[str, Any]:
        """The condition tree for this SOP, whichever key it lives under."""
        return self.conditions or self.signals or {}


# --------------------------------------------------------------------------- #
# Intent (what the intake LLM is allowed to produce)
# --------------------------------------------------------------------------- #
class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_outdoor_safety_question: bool = True
    location: Optional[str] = None
    activity_raw: Optional[str] = None
    activity: Optional[str] = None  # canonical key from sops/_vocab.yaml
    activity_tags: list[str] = Field(default_factory=list)
    audience: list[str] = Field(default_factory=lambda: ["general"])
    time_window: str = "now"
    is_followup: bool = False


# --------------------------------------------------------------------------- #
# Weather
# --------------------------------------------------------------------------- #
class ResolvedLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    country: Optional[str] = None
    admin1: Optional[str] = None
    latitude: float
    longitude: float
    timezone: str = "UTC"

    @property
    def label(self) -> str:
        bits = [self.name] + [b for b in (self.admin1, self.country) if b]
        return ", ".join(bits)


class WeatherSnapshot(BaseModel):
    """The one and only source of numbers downstream. Raw Open-Meteo values."""

    model_config = ConfigDict(extra="forbid")

    place: ResolvedLocation
    fetched_at: str
    current_time: str
    current: dict[str, Optional[float]] = Field(default_factory=dict)
    hourly_times: list[str] = Field(default_factory=list)
    hourly: dict[str, list[Optional[float]]] = Field(default_factory=dict)
    daily: dict[str, list[Any]] = Field(default_factory=dict)
    derived: dict[str, Optional[float]] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)


class WindowValues(BaseModel):
    """Field values collapsed over the time window the user actually asked about."""

    model_config = ConfigDict(extra="forbid")

    window: str
    label: str
    times: list[str] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)


class MatchedSOP(BaseModel):
    """An SOP that matched, with why it matched (used by the conflict rule)."""

    model_config = ConfigDict(extra="forbid")

    sop_id: str
    severity: Severity
    matched_conditions: int = 0
    matched_tags: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)
    via: Literal["numeric", "fuzzy", "situational"] = "numeric"

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Severity first, then specificity (conditions, then tags)."""
        return (self.severity.rank, self.matched_conditions, self.matched_tags)


# --------------------------------------------------------------------------- #
# Graph state
# --------------------------------------------------------------------------- #
Branch = Literal["compose", "override", "no_match", "fail", "clarify"]


class GraphState(TypedDict, total=False):
    session_id: str
    user_message: str
    history: list[dict[str, str]]
    established_facts: dict[str, Any]
    intent: Intent
    location: ResolvedLocation
    weather: WeatherSnapshot
    window: WindowValues
    candidate_sops: list[str]
    matched: list[MatchedSOP]
    matched_sop_ids: list[str]
    situational_ids: list[str]
    error: Optional[str]
    clarify_question: Optional[str]
    failed_before_fetch: bool
    branch: Branch
    reply: str
    guard_report: dict[str, Any]
    trace: list[str]

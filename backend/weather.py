"""Open-Meteo client and time-window resolution. Deterministic, no LLM.

The query lists are built from ``sops/_fields.yaml``, so adding a weather
variable is a data change. Every network call has a timeout, every payload is
validated with Pydantic before anything reads it, and every failure mode (timeout,
HTTP error, bad JSON, wrong shape, missing variable) raises a typed error that the
graph routes to a single failure branch. Error messages are written for the user;
the raw cause is logged, never shown.

Snapshot dictionaries are keyed by *Open-Meteo variable name* - the raw payload
names - while SOP conditions use the field names from ``_fields.yaml``;
:func:`resolve_window` is the only place that translates between the two.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import unicodedata
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from backend import conditions
from backend.loader import Policy, get_policy
from backend.models import (
    ForecastPayload,
    GeocodeResponse,
    GeocodeResult,
    LocationAmbiguous,
    LocationUnresolved,
    ResolvedLocation,
    WeatherSnapshot,
    WeatherUnavailable,
    WindowValues,
)

log = logging.getLogger("advisory")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
FORECAST_DAYS = 3  # floor; a time window further out in _vocab.yaml raises it (see fetch_forecast)
PAST_HOURS = 6  # needed for the pressure-trend derived signal
GEOCODE_COUNT = 10
# A same-named place in another country is only accepted silently when it clearly
# dominates: at least this many people, and this many times the runner-up.
MIN_POPULATION = 50_000
POPULATION_DOMINANCE = 10


def _simulated_down() -> bool:
    return os.getenv("SIMULATE_WEATHER_DOWN", "0").strip() in {"1", "true", "yes"}


def _get_json(url: str, params: dict[str, Any], service: str, client: Optional[httpx.Client]) -> Any:
    """GET + status check + JSON decode. Raises WeatherUnavailable with a user-safe reason;
    the raw cause (URL, status line, body) goes to the log only."""
    owns = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        log.warning("%s timed out: %r", service, exc)
        raise WeatherUnavailable(f"the {service} timed out") from exc
    except httpx.HTTPStatusError as exc:
        log.warning("%s returned an error: %r", service, exc)
        raise WeatherUnavailable(f"the {service} returned an error response") from exc
    except httpx.HTTPError as exc:
        log.warning("%s unreachable: %r", service, exc)
        raise WeatherUnavailable(f"the {service} could not be reached") from exc
    except ValueError as exc:
        log.warning("%s returned non-JSON: %r", service, exc)
        raise WeatherUnavailable(f"the {service} returned unreadable data") from exc
    finally:
        if owns:
            client.close()


# --------------------------------------------------------------------------- #
# Geocoding
# --------------------------------------------------------------------------- #
def _normalise_place(name: str) -> str:
    """Casefold and strip accents so "Bhopal" matches "Bhopāl"."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _population(result: GeocodeResult) -> float:
    return result.population or 0


def _label(result: GeocodeResult) -> str:
    return ", ".join([result.name] + [b for b in (result.admin1, result.country) if b])


def geocode(city: str, client: Optional[httpx.Client] = None) -> ResolvedLocation:
    """Resolve a place name, or refuse to guess.

    Open-Meteo ranks by relevance, not by what the user meant: "Goa" returns Genoa
    (Italy) first, and a typo like "bhopl" returns a village in Bangladesh. So:

      * no results -> :class:`LocationUnresolved`;
      * no exact name match (accent- and case-insensitive) -> :class:`LocationAmbiguous`,
        because the place the user typed is not in the index at all;
      * "Name, Qualifier" ("Springfield, Illinois", "Bhopal, India") -> only exact
        matches whose state, district, country or country code equals a qualifier
        count; the most populous of those wins, none -> :class:`LocationAmbiguous`.
        This is how a user answers the bot's own "which one did you mean?";
      * one exact match, or several within a single country -> take the most populous;
      * exact matches in several countries -> accept the most populous ONLY if it has
        at least MIN_POPULATION people and POPULATION_DOMINANCE times the runner-up;
        otherwise raise :class:`LocationAmbiguous` and let the graph ask.
    """
    query = " ".join((city or "").split())[:100]
    if not query:
        raise LocationUnresolved("no place name was given")
    if _simulated_down():
        raise WeatherUnavailable("the weather service is switched off for this demo (SIMULATE_WEATHER_DOWN)")

    name, _, qualifier = query.partition(",")
    name = name.strip()
    qualifiers = {_normalise_place(q) for q in qualifier.split(",") if q.strip()}
    params = {"name": name, "count": GEOCODE_COUNT, "language": "en", "format": "json"}
    payload = _get_json(GEOCODE_URL, params, "place-name lookup service", client)
    try:
        results = GeocodeResponse.model_validate(payload).results
    except ValidationError as exc:
        log.warning("geocoding payload failed validation: %s", exc)
        raise WeatherUnavailable("the place-name lookup service returned data that failed validation") from exc
    if not results:
        raise LocationUnresolved(f"no place found matching {query!r}")

    wanted = _normalise_place(name)
    exact = [r for r in results if _normalise_place(r.name) == wanted]
    if not exact:
        raise LocationAmbiguous(query, [_label(r) for r in results[:3]])

    ranked = sorted(exact, key=_population, reverse=True)
    if qualifiers:
        named = [r for r in ranked if qualifiers & {
            _normalise_place(x) for x in (r.admin1, r.admin2, r.country, r.country_code) if x}]
        if named:
            return location_from_geocode(named[0])
        raise LocationAmbiguous(query, [_label(r) for r in ranked[:3]])
    if len(ranked) == 1 or len({r.country for r in ranked}) == 1:
        return location_from_geocode(ranked[0])

    best, runner_up = _population(ranked[0]), _population(ranked[1])
    if best >= MIN_POPULATION and best >= POPULATION_DOMINANCE * max(runner_up, 1):
        return location_from_geocode(ranked[0])
    raise LocationAmbiguous(query, [_label(r) for r in ranked[:3]])


def location_from_geocode(result: GeocodeResult | dict[str, Any]) -> ResolvedLocation:
    """Build a ResolvedLocation from one Open-Meteo geocoding result."""
    try:
        place = GeocodeResult.model_validate(result) if isinstance(result, dict) else result
    except ValidationError as exc:
        raise LocationUnresolved("the place-name lookup returned a result without valid coordinates") from exc
    return ResolvedLocation(
        name=place.name,
        country=place.country,
        admin1=place.admin1,
        latitude=place.latitude,
        longitude=place.longitude,
        timezone=place.timezone or "UTC",
    )


# --------------------------------------------------------------------------- #
# Forecast
# --------------------------------------------------------------------------- #
def _query_variables(policy: Policy) -> tuple[list[str], list[str]]:
    current, hourly = [], []
    for spec in policy.fields.values():
        if "current" in spec.blocks and spec.variable not in current:
            current.append(spec.variable)
        if "hourly" in spec.blocks and spec.variable not in hourly:
            hourly.append(spec.variable)
    return current, hourly


def fetch_forecast(
    lat: float,
    lon: float,
    place: ResolvedLocation,
    policy: Optional[Policy] = None,
    client: Optional[httpx.Client] = None,
) -> WeatherSnapshot:
    """Fetch current + hourly values for one point, with the variables named explicitly."""
    policy = policy or get_policy()
    if _simulated_down():
        raise WeatherUnavailable("the weather service is switched off for this demo (SIMULATE_WEATHER_DOWN)")

    current_vars, hourly_vars = _query_variables(policy)
    # A time window added to _vocab.yaml further out than the floor still gets data.
    furthest = max(spec.day_offset for spec in policy.time_windows.values())
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(current_vars),
        "hourly": ",".join(hourly_vars),
        "timezone": "auto",
        "forecast_days": max(FORECAST_DAYS, furthest + 2),  # +2: the next-24h signals run past midnight
        "past_hours": PAST_HOURS,
    }
    payload = _get_json(FORECAST_URL, params, "weather service", client)
    return snapshot_from_payload(payload, place, policy)


def snapshot_from_payload(
    payload: dict[str, Any],
    place: ResolvedLocation,
    policy: Optional[Policy] = None,
) -> WeatherSnapshot:
    """Build a snapshot from a forecast payload (live response or recorded fixture).

    Raises WeatherUnavailable unless the payload validates as :class:`ForecastPayload`
    and carries every hourly variable that was requested - a partial or malformed
    payload never becomes a partial forecast.
    """
    policy = policy or get_policy()
    parsed = _parse_forecast(payload)
    _, hourly_vars = _query_variables(policy)
    missing = [v for v in hourly_vars if v not in parsed.hourly]
    if missing:
        log.warning("forecast payload is missing hourly variables %s", missing)
        raise WeatherUnavailable("the weather service left out values that were asked for")

    ctx = conditions.DerivedContext(
        current=parsed.current,
        hourly=parsed.hourly,
        times=parsed.hourly_time,
        now_index=now_index(parsed.hourly_time, parsed.current_time),
    )
    return WeatherSnapshot(
        place=place.model_copy(update={"timezone": parsed.timezone or place.timezone}),
        fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        current_time=parsed.current_time,
        current=parsed.current,
        hourly_times=parsed.hourly_time,
        hourly=parsed.hourly,
        derived=conditions.compute_derived(ctx, policy.derived),
        units=parsed.units,
    )


def _parse_forecast(payload: Any) -> ForecastPayload:
    """Validate the raw forecast JSON. Every shape problem is one typed error."""
    try:
        if not isinstance(payload, dict):
            raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
        current = dict(payload["current"])
        hourly = dict(payload["hourly"])
        current.pop("interval", None)
        units = {**(payload.get("current_units") or {}), **(payload.get("hourly_units") or {})}
        return ForecastPayload(
            timezone=payload.get("timezone"),
            current_time=current.pop("time"),
            current=current,
            hourly_time=hourly.pop("time"),
            hourly=hourly,
            units={str(k): str(v) for k, v in units.items()},
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:  # ValidationError is a ValueError
        log.warning("forecast payload failed validation: %s", exc)
        raise WeatherUnavailable("the weather service returned data that failed validation") from exc


def get_weather(city: str, policy: Optional[Policy] = None) -> WeatherSnapshot:
    """geocode + fetch in one call (used by the graph and the CLI)."""
    policy = policy or get_policy()
    place = geocode(city)
    return fetch_forecast(place.latitude, place.longitude, place, policy)


# --------------------------------------------------------------------------- #
# Time windows
# --------------------------------------------------------------------------- #
def now_index(times: list[Any], current_time: str) -> int:
    """Index of the hourly slot covering `current_time` (nearest slot if absent)."""
    prefix = str(current_time)[:13]  # YYYY-MM-DDTHH
    for index, value in enumerate(times):
        if str(value).startswith(prefix):
            return index
    try:
        target = dt.datetime.fromisoformat(str(current_time))
        return min(
            range(len(times)),
            key=lambda i: abs(dt.datetime.fromisoformat(str(times[i])) - target),
        )
    except ValueError:
        return 0


def _hour_of(stamp: str) -> int:
    return int(str(stamp)[11:13])


def _date_of(stamp: str) -> str:
    return str(stamp)[:10]


def resolve_window(
    snapshot: WeatherSnapshot,
    window: str,
    policy: Optional[Policy] = None,
    clamp_hours: Optional[tuple[int, int]] = None,
) -> WindowValues:
    """Collapse the snapshot to the field values for the period asked about.

    `window` is a key from ``_vocab.yaml::time_windows``. `clamp_hours` narrows it
    further - the matcher passes an SOP's own ``time_window.hours`` so that, say,
    a UV rule written for 11:00-16:00 is judged on those hours only.

    Derived signals are always included: they are "from now on" signals (the next
    6h / 24h, the last 3h of pressure) and do not re-slice per window.
    """
    policy = policy or get_policy()
    spec = policy.time_windows.get(window) or policy.time_windows["now"]
    index = now_index(snapshot.hourly_times, snapshot.current_time)

    use_current = spec.use_current and clamp_hours is None
    if spec.use_current and clamp_hours is not None:
        # "right now" plus an SOP hour restriction: only applies if now is inside it.
        lo, hi = clamp_hours
        if not lo <= _hour_of(snapshot.current_time) <= hi:
            return WindowValues(window=window, label=spec.label, times=[], values={},
                               missing=sorted(policy.fields))
        use_current = True

    if use_current:
        values: dict[str, Any] = {}
        missing: list[str] = []
        for name, field_spec in policy.fields.items():
            raw = snapshot.current.get(field_spec.variable)
            if raw is None:  # not every variable exists in the `current` block (e.g. uv_index)
                series = snapshot.hourly.get(field_spec.variable) or []
                raw = series[index] if index < len(series) else None
            if raw is None:
                missing.append(name)
                values[name] = None
            else:
                values[name] = conditions.aggregate([raw], field_spec.aggregate)
        values.update(snapshot.derived)
        missing += [k for k, v in snapshot.derived.items() if v is None]
        return WindowValues(
            window=window, label=spec.label, times=[snapshot.current_time],
            values=values, missing=sorted(set(missing)),
        )

    start_hour = spec.start_hour if spec.start_hour is not None else 0
    end_hour = spec.end_hour if spec.end_hour is not None else 23
    if clamp_hours:
        start_hour = max(start_hour, clamp_hours[0])
        end_hour = min(end_hour, clamp_hours[1])

    target_date = (
        dt.date.fromisoformat(_date_of(snapshot.current_time)) + dt.timedelta(days=spec.day_offset)
    ).isoformat()
    slots = [
        i
        for i, stamp in enumerate(snapshot.hourly_times)
        if _date_of(stamp) == target_date and start_hour <= _hour_of(stamp) <= end_hour
    ]
    # Never advise on hours that have already passed today. A period that is over
    # yields no slots, and the graph says so instead of answering about old hours.
    if spec.day_offset == 0:
        slots = [i for i in slots if i >= index]

    if start_hour > end_hour or not slots:
        return WindowValues(window=window, label=spec.label, times=[], values={},
                           missing=sorted(policy.fields))

    values = {}
    missing = []
    for name, field_spec in policy.fields.items():
        series = snapshot.hourly.get(field_spec.variable) or []
        picked = [series[i] for i in slots if i < len(series)]
        value = conditions.aggregate(picked, field_spec.aggregate)
        values[name] = value
        if value is None:
            missing.append(name)
    values.update(snapshot.derived)
    missing += [k for k, v in snapshot.derived.items() if v is None]
    return WindowValues(
        window=window,
        label=spec.label,
        times=[snapshot.hourly_times[i] for i in slots],
        values=values,
        missing=sorted(set(missing)),
    )


if __name__ == "__main__":
    import json
    import sys

    city = sys.argv[1] if len(sys.argv) > 1 else "Bhopal"
    snap = get_weather(city)
    print(f"{snap.place.label}  tz={snap.place.timezone}  now={snap.current_time}")
    print("derived:", json.dumps(snap.derived, indent=2))
    for name in ("now", "this_evening", "tomorrow_afternoon"):
        win = resolve_window(snap, name)
        print(f"\n[{name}] {win.label}  slots={len(win.times)} {win.times[:1]}..{win.times[-1:]}")
        print("  ", {k: v for k, v in win.values.items() if k in
                     ("temperature_2m", "apparent_temperature", "uv_index",
                      "precipitation_probability", "wind_speed_10m", "weather_code", "visibility")})

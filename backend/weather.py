"""Open-Meteo client and time-window resolution. Deterministic, no LLM.

The query lists are built from ``sops/_fields.yaml``, so adding a weather
variable is a data change. Every network call has a timeout, and every failure
mode (timeout, HTTP error, bad JSON, missing block) raises a typed error that the
graph routes to a single failure branch.

Snapshot dictionaries are keyed by *Open-Meteo variable name* - the raw payload
names - while SOP conditions use the field names from ``_fields.yaml``;
:func:`resolve_window` is the only place that translates between the two.
"""

from __future__ import annotations

import datetime as dt
import os
import unicodedata
from typing import Any, Optional

import httpx

from backend import conditions
from backend.loader import Policy, get_policy
from backend.models import (
    LocationAmbiguous,
    LocationUnresolved,
    ResolvedLocation,
    WeatherSnapshot,
    WeatherUnavailable,
    WindowValues,
)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
FORECAST_DAYS = 3
PAST_HOURS = 6  # needed for the pressure-trend derived signal
# A same-named place in another country is only accepted silently when it clearly
# dominates: at least this many people, and this many times the runner-up.
MIN_POPULATION = 50_000
POPULATION_DOMINANCE = 10


def _simulated_down() -> bool:
    return os.getenv("SIMULATE_WEATHER_DOWN", "0").strip() in {"1", "true", "yes"}


# --------------------------------------------------------------------------- #
# Geocoding
# --------------------------------------------------------------------------- #
def _normalise_place(name: str) -> str:
    """Casefold and strip accents so "Bhopal" matches "Bhopāl"."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _population(result: dict[str, Any]) -> int:
    value = result.get("population")
    return int(value) if isinstance(value, (int, float)) else 0


def _label(result: dict[str, Any]) -> str:
    bits = [result.get("name", "?")] + [
        str(b) for b in (result.get("admin1"), result.get("country")) if b
    ]
    return ", ".join(bits)


def geocode(city: str, client: Optional[httpx.Client] = None) -> ResolvedLocation:
    """Resolve a place name, or refuse to guess.

    Open-Meteo ranks by relevance, not by what the user meant: "Goa" returns Genoa
    (Italy) first, and a typo like "bhopl" returns a village in Bangladesh. So:

      * no results -> :class:`LocationUnresolved`;
      * no exact name match (accent- and case-insensitive) -> :class:`LocationAmbiguous`,
        because the place the user typed is not in the index at all;
      * one exact match, or several within a single country -> take the most populous;
      * exact matches in several countries -> accept the most populous ONLY if it has
        at least MIN_POPULATION people and POPULATION_DOMINANCE times the runner-up;
        otherwise raise :class:`LocationAmbiguous` and let the graph ask.
    """
    name = (city or "").strip()
    if not name:
        raise LocationUnresolved("no place name given")
    if _simulated_down():
        raise WeatherUnavailable("SIMULATE_WEATHER_DOWN=1 (geocoding call not attempted)")

    params = {"name": name, "count": 5, "language": "en", "format": "json"}
    try:
        owns = client is None
        client = client or httpx.Client(timeout=TIMEOUT)
        try:
            response = client.get(GEOCODE_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns:
                client.close()
    except httpx.HTTPError as exc:
        raise LocationUnresolved(f"geocoding service unreachable for {name!r}: {exc}") from exc
    except ValueError as exc:
        raise LocationUnresolved(f"geocoding returned unreadable JSON for {name!r}") from exc

    results = payload.get("results") or []
    if not results:
        raise LocationUnresolved(f"no place found matching {name!r}")

    wanted = _normalise_place(name)
    exact = [r for r in results if _normalise_place(r.get("name", "")) == wanted]
    if not exact:
        raise LocationAmbiguous(name, [_label(r) for r in results[:3]])

    ranked = sorted(exact, key=_population, reverse=True)
    if len(ranked) == 1 or len({r.get("country") for r in ranked}) == 1:
        return location_from_geocode(ranked[0])

    best, runner_up = _population(ranked[0]), _population(ranked[1])
    if best >= MIN_POPULATION and best >= POPULATION_DOMINANCE * max(runner_up, 1):
        return location_from_geocode(ranked[0])
    raise LocationAmbiguous(name, [_label(r) for r in ranked[:3]])


def location_from_geocode(result: dict[str, Any]) -> ResolvedLocation:
    """Build a ResolvedLocation from one Open-Meteo geocoding result."""
    try:
        return ResolvedLocation(
            name=result["name"],
            country=result.get("country"),
            admin1=result.get("admin1"),
            latitude=float(result["latitude"]),
            longitude=float(result["longitude"]),
            timezone=result.get("timezone") or "UTC",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LocationUnresolved(f"geocoding result missing coordinates: {result!r}") from exc


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
    """Fetch current + hourly + daily values for one point."""
    policy = policy or get_policy()
    if _simulated_down():
        raise WeatherUnavailable("SIMULATE_WEATHER_DOWN=1 (forecast call not attempted)")

    current_vars, hourly_vars = _query_variables(policy)
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(current_vars),
        "hourly": ",".join(hourly_vars),
        "daily": ",".join(policy.daily),
        "timezone": "auto",
        "forecast_days": FORECAST_DAYS,
        "past_hours": PAST_HOURS,
    }
    try:
        owns = client is None
        client = client or httpx.Client(timeout=TIMEOUT)
        try:
            response = client.get(FORECAST_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns:
                client.close()
    except httpx.HTTPError as exc:
        raise WeatherUnavailable(f"Open-Meteo forecast unreachable: {exc}") from exc
    except ValueError as exc:
        raise WeatherUnavailable("Open-Meteo forecast returned unreadable JSON") from exc

    return snapshot_from_payload(payload, place, policy)


def snapshot_from_payload(
    payload: dict[str, Any],
    place: ResolvedLocation,
    policy: Optional[Policy] = None,
) -> WeatherSnapshot:
    """Build a snapshot from a forecast payload (live response or recorded fixture)."""
    policy = policy or get_policy()
    current = payload.get("current")
    hourly = payload.get("hourly")
    if not isinstance(current, dict) or not isinstance(hourly, dict):
        raise WeatherUnavailable("Open-Meteo payload has no `current`/`hourly` block")

    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise WeatherUnavailable("Open-Meteo payload has no hourly timeline")

    _, hourly_vars = _query_variables(policy)
    missing = [v for v in hourly_vars if v not in hourly]
    if missing:
        raise WeatherUnavailable(f"Open-Meteo payload is missing hourly variables: {missing}")

    current_time = str(current.get("time") or times[0])
    series = {k: list(v) for k, v in hourly.items() if k != "time" and isinstance(v, list)}
    current_values = {
        k: (float(v) if isinstance(v, (int, float)) else None)
        for k, v in current.items()
        if k not in {"time", "interval"}
    }

    ctx = conditions.DerivedContext(
        current=current_values,
        hourly=series,
        times=[str(t) for t in times],
        now_index=now_index(times, current_time),
    )
    derived = conditions.compute_derived(ctx, policy.derived)

    units = {**(payload.get("current_units") or {}), **(payload.get("hourly_units") or {})}
    place = place.model_copy(update={"timezone": payload.get("timezone") or place.timezone})

    return WeatherSnapshot(
        place=place,
        fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        current_time=current_time,
        current=current_values,
        hourly_times=[str(t) for t in times],
        hourly=series,
        daily={k: list(v) for k, v in (payload.get("daily") or {}).items()},
        derived=derived,
        units={k: str(v) for k, v in units.items()},
    )


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
    # Never advise on hours that have already passed today.
    if spec.day_offset == 0:
        slots = [i for i in slots if i >= index] or slots[-1:]

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

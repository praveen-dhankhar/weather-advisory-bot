"""Records the Open-Meteo fixtures used by the deterministic eval mode.

Two payloads are recorded verbatim from the live API (mild, hot). The extreme
scenarios are DERIVED from a recorded payload by editing specific arrays, because
you cannot wait for a storm to write a test. Each file carries a `_provenance`
block saying exactly what was edited - see evals/RESULTS.md.

    python evals/fixtures/record.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

from backend.loader import get_policy  # noqa: E402
from backend.weather import FORECAST_DAYS, FORECAST_URL, GEOCODE_URL, PAST_HOURS, _query_variables  # noqa: E402

HERE = Path(__file__).resolve().parent


def record(city: str, stem: str) -> dict:
    policy = get_policy()
    geo = httpx.get(GEOCODE_URL, params={"name": city, "count": 1, "format": "json"}, timeout=15).json()
    place = geo["results"][0]
    current_vars, hourly_vars = _query_variables(policy)
    forecast = httpx.get(
        FORECAST_URL,
        params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": ",".join(current_vars), "hourly": ",".join(hourly_vars),
            "timezone": "auto",
            "forecast_days": FORECAST_DAYS, "past_hours": PAST_HOURS,
        },
        timeout=20,
    ).json()
    payload = {
        "_provenance": f"recorded verbatim from Open-Meteo for {city}",
        "geocode": place,
        "forecast": forecast,
    }
    (HERE / f"{stem}.json").write_text(json.dumps(payload, indent=1))
    print(f"wrote {stem}.json ({city})")
    return payload


def derive(base: dict, stem: str, note: str, **edits) -> None:
    """Copy a recorded payload and overwrite named series/current values."""
    out = copy.deepcopy(base)
    out["_provenance"] = f"derived from a recorded Open-Meteo payload; edited: {note}"
    hourly = out["forecast"]["hourly"]
    current = out["forecast"]["current"]
    for key, value in edits.items():
        block, _, name = key.partition("__")
        if block == "hourly":
            hourly[name] = value if isinstance(value, list) else [value] * len(hourly["time"])
        elif block == "hourlyfrom":  # value = (start_hour_of_day, filler) applied to every day
            start, filler = value
            hourly[name] = [
                filler if int(t[11:13]) >= start else hourly[name][i]
                for i, t in enumerate(hourly["time"])
            ]
        elif block == "current":
            current[name] = value
        else:
            raise ValueError(key)
    (HERE / f"{stem}.json").write_text(json.dumps(out, indent=1))
    print(f"wrote {stem}.json ({note})")


def main() -> None:
    mild = record("Pune", "mild_pune")
    record("Jaipur", "hot_jaipur")
    times = mild["forecast"]["hourly"]["time"]
    n = len(times)

    derive(
        mild, "severe_rain",
        "heavy continuous rain, falling pressure 1006->994, rain/thunder codes, 95% rain probability",
        hourly__precipitation=[6.5] * n,
        hourly__rain=[6.5] * n,
        hourly__precipitation_probability=[95.0] * n,
        hourly__weather_code=[65 if i % 3 else 95 for i in range(n)],
        hourly__pressure_msl=[max(994.0, 1006.0 - 0.5 * i) for i in range(n)],
        hourly__visibility=[2500.0] * n,
        hourly__wind_speed_10m=[22.0] * n,
        hourly__wind_gusts_10m=[41.0] * n,
        current__precipitation=6.5,
        current__rain=6.5,
        current__weather_code=65,
        current__pressure_msl=997.0,
        current__wind_speed_10m=22.0,
        current__wind_gusts_10m=41.0,
        current__visibility=2500.0,
    )
    derive(
        mild, "high_wind",
        "sustained wind 48 km/h with 71 km/h gusts, otherwise the recorded day",
        hourly__wind_speed_10m=[48.0] * n,
        hourly__wind_gusts_10m=[71.0] * n,
        current__wind_speed_10m=48.0,
        current__wind_gusts_10m=71.0,
    )
    derive(
        mild, "high_uv_midday",
        "UV index 9.4 and 36.5 C apparent through the day (peak-sun scenario)",
        hourly__uv_index=[9.4 if 10 <= int(t[11:13]) <= 17 else 1.0 for t in times],
        hourly__temperature_2m=[35.0] * n,
        hourly__apparent_temperature=[36.5] * n,
        current__temperature_2m=35.0,
        current__apparent_temperature=36.5,
    )
    derive(
        mild, "fog",
        "visibility 400 m with fog code 45",
        hourly__visibility=[400.0] * n,
        hourly__weather_code=[45] * n,
        current__visibility=400.0,
        current__weather_code=45,
    )
    derive(
        mild, "cold_snap",
        "apparent temperature 4.5 C, air 6 C",
        hourly__apparent_temperature=[4.5] * n,
        hourly__temperature_2m=[6.0] * n,
        current__apparent_temperature=4.5,
        current__temperature_2m=6.0,
    )
    derive(
        mild, "pleasant",
        "23 C air / 24 C apparent, UV 4, 5% rain probability, 12 km/h wind (comfortable day)",
        hourly__temperature_2m=[23.0] * n,
        hourly__apparent_temperature=[24.0] * n,
        hourly__uv_index=[4.0] * n,
        hourly__precipitation=[0.0] * n,
        hourly__rain=[0.0] * n,
        hourly__precipitation_probability=[5.0] * n,
        hourly__wind_speed_10m=[12.0] * n,
        hourly__wind_gusts_10m=[19.0] * n,
        hourly__weather_code=[1] * n,
        current__temperature_2m=23.0,
        current__apparent_temperature=24.0,
        current__precipitation=0.0,
        current__rain=0.0,
        current__wind_speed_10m=12.0,
        current__wind_gusts_10m=19.0,
        current__weather_code=1,
    )
    derive(
        mild, "thunderstorm",
        "thunderstorm codes 95/96 all day, moderate rain",
        hourly__weather_code=[95 if i % 2 else 96 for i in range(n)],
        hourly__precipitation=[2.0] * n,
        hourly__precipitation_probability=[80.0] * n,
        current__weather_code=95,
    )


if __name__ == "__main__":
    main()

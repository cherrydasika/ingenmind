"""get_weather: current weather and a daily forecast from Open-Meteo, for a
place name: geocoding-api.open-meteo.com (name → coordinates), then
api.open-meteo.com/v1/forecast. No API key. Free for non-commercial use
(under 10,000 calls a day); data CC BY 4.0, attribution required. Answers
are cached for 30 minutes per place and number of days.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ApiTool, failure

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"
CACHE_SECONDS = 30 * 60
MAX_DAYS = 7
USER_AGENT = "rag-systems-cloud/1.0 (personal project; external-APIs agent)"
# WMO weather interpretation codes, as documented by Open-Meteo.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle", 56: "light freezing drizzle",
    57: "dense freezing drizzle", 61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain", 71: "slight snow", 73: "moderate snow",
    75: "heavy snow", 77: "snow grains", 80: "slight rain showers", 81: "moderate rain showers",
    82: "violent rain showers", 85: "slight snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

# Open-Meteo's geocoder matches a place name only: "Leeds, UK" finds nothing.
# What follows the first comma picks among the matches instead (country,
# country code or region); these names stand for a country code.
COUNTRY_ALIASES = {
    "uk": "gb", "u.k.": "gb", "united kingdom": "gb", "britain": "gb", "great britain": "gb",
    "england": "gb", "scotland": "gb", "wales": "gb", "northern ireland": "gb",
    "us": "us", "usa": "us", "u.s.": "us", "u.s.a.": "us", "united states": "us", "america": "us",
}
MAX_MATCHES = 10

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _get(url: str, params: dict) -> dict:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def _geocode(location: str) -> dict | None:
    """The best match for "Name" or "Name, Region, Country". The name is
    searched, within the country when a qualifier names one by code or alias
    (UK, England, USA…), else everywhere. Each qualifier that names a match's
    region or country counts; an alias counts half; then the larger place
    wins (Newark, UK → Newark-on-Trent, not a hamlet in Orkney)."""
    name, *qualifiers = [part.strip() for part in location.split(",") if part.strip()] or [location]
    params = {"name": name, "count": MAX_MATCHES if qualifiers else 1, "language": "en", "format": "json"}
    codes = [COUNTRY_ALIASES.get(q.lower()) or (q.lower() if len(q) == 2 and q.isalpha() else None) for q in qualifiers]
    code = next((c for c in codes if c), None)
    found = _get(GEOCODING, {**params, "countryCode": code.upper()}).get("results") or [] if code else []
    if not found:
        found = _get(GEOCODING, params).get("results") or []

    def rank(place: dict) -> tuple[float, int]:
        fields = {str(place.get(k) or "").lower() for k in ("country", "country_code", "admin1", "admin2")} - {""}
        score = 0.0
        for qualifier in (q.lower() for q in qualifiers):
            if qualifier in fields:
                score += 1
            elif COUNTRY_ALIASES.get(qualifier) in fields:
                score += 0.5
        return score, place.get("population") or 0

    return max(found, key=rank, default=None)   # max keeps the first of equal ranks


def _forecast(place: dict, days: int) -> dict:
    data = _get(FORECAST, {
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
    })
    current, daily, units = data.get("current") or {}, data.get("daily") or {}, data.get("daily_units") or {}
    return {
        "place": {k: place.get(k) for k in ("name", "admin1", "country", "latitude", "longitude", "timezone")},
        "current": {
            "time": current.get("time"),
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "conditions": WMO.get(current.get("weather_code"), f"code {current.get('weather_code')}"),
            "wind_kmh": current.get("wind_speed_10m"),
            "precipitation_mm": current.get("precipitation"),
        },
        "daily": [{
            "date": date,
            "conditions": WMO.get(daily["weather_code"][i], f"code {daily['weather_code'][i]}"),
            "max_c": daily["temperature_2m_max"][i],
            "min_c": daily["temperature_2m_min"][i],
            "precipitation_mm": daily["precipitation_sum"][i],
            "precipitation_chance_pct": (daily.get("precipitation_probability_max") or [None] * len(daily["time"]))[i],
        } for i, date in enumerate(daily.get("time") or [])],
        "units": {"temperature": units.get("temperature_2m_max", "°C"), "precipitation": units.get("precipitation_sum", "mm")},
        "source": ATTRIBUTION,
    }


def _display(s: dict) -> dict:
    place, now = s["place"], s["current"]
    where = ", ".join(v for v in (place["name"], place.get("admin1"), place.get("country")) if v)
    facts = [["Now", f"{now['temperature_c']} °C (feels {now['feels_like_c']} °C), {now['conditions']}, "
                     f"wind {now['wind_kmh']} km/h"]]
    facts += [[d["date"], f"{d['conditions']} · {d['min_c']}–{d['max_c']} °C · {d['precipitation_mm']} mm"
                          + (f" ({d['precipitation_chance_pct']}%)" if d["precipitation_chance_pct"] is not None else "")]
              for d in s["daily"]]
    facts.append(["Location", f"{place['latitude']}, {place['longitude']} · {place['timezone']}"])
    return {
        "headline": f"{where}: {now['temperature_c']} °C, {now['conditions']}",
        "facts": facts,
        "links": [["Open-Meteo", "https://open-meteo.com/"]],
        "warning": None,
        "attribution": ATTRIBUTION,
    }


def run(args: dict) -> dict:
    started = time.monotonic()
    location = str(args.get("location") or "").strip()[:100]
    try:
        days = max(1, min(int(args.get("days") or 3), MAX_DAYS))
    except (TypeError, ValueError):
        days = 3
    if not location:
        return failure("location is empty; give a city or place name")
    key = (location.lower(), days)
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        summary, cached = hit[1], True
    else:
        try:
            place = _geocode(location)
            if not place:
                return failure(f"No place called {location!r} found by Open-Meteo geocoding", time.monotonic() - started)
            summary = _forecast(place, days)
        except urllib.error.HTTPError as http_error:
            body = http_error.read(300).decode(errors="replace")
            return failure(f"Open-Meteo returned HTTP {http_error.code}: {body}", time.monotonic() - started)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as other:
            return failure(f"Open-Meteo could not be read: {other}", time.monotonic() - started)
        with _lock:
            _cache[key] = (time.time(), summary)
        cached = False
    return {"ok": True, "cached": cached, "error": None, "seconds": round(time.monotonic() - started, 3),
            "summary": summary, "display": _display(summary)}


TOOL = ApiTool(
    name="get_weather",
    title="Weather · Open-Meteo",
    description=(
        "Current weather and a daily forecast (up to 7 days) for a city or place name, from Open-Meteo: "
        "temperature, conditions, wind, precipitation and its chance."
    ),
    properties={
        "location": {"type": "string", "description": "City or place name, e.g. Vienna or Salzburg, Austria"},
        "days": {"type": "integer", "description": f"Forecast days, 1–{MAX_DAYS}; 3 if not stated"},
    },
    required=["location"],
    run=run,
    steps=[["Geocoding", "geocoding-api.open-meteo.com · place name → coordinates"],
           ["Forecast", "api.open-meteo.com/v1/forecast · current + daily"],
           ["Cache", "30 minutes per place and number of days"]],
    attribution=ATTRIBUTION,
)

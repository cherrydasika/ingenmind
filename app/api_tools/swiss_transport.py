"""Swiss public transport from transport.opendata.ch (https://transport.opendata.ch/docs.html):

- swiss_connections: GET /v1/connections?from=…&to=… (date, time, arrival or
  departure time), the next connections with their legs, platforms and delays;
- swiss_departures: GET /v1/stationboard?station=…, a station's next departures.

No API key. Requests count against timetable.search.ch's daily limits (1,000
route searches and 10,080 departure boards), so answers are cached briefly:
10 minutes for connections, 2 minutes for departures (they carry delays).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import ApiTool, failure

API = "https://transport.opendata.ch/v1"
ATTRIBUTION = "Timetable data: transport.opendata.ch (search.ch / opentransportdata.swiss)"
USER_AGENT = "rag-systems-cloud/1.0 (personal project; external-APIs agent)"
MAX_RESULTS = 6

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _get(path: str, params: list[tuple[str, str]], ttl: int) -> tuple[dict, bool]:
    key = (path, tuple(params))
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1], True
    request = urllib.request.Request(f"{API}/{path}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())
    with _lock:
        _cache[key] = (time.time(), data)
    return data, False


def _call(path: str, params: list[tuple[str, str]], ttl: int, shape) -> dict:
    started = time.monotonic()
    try:
        data, cached = _get(path, params, ttl)
        summary, display = shape(data)
    except urllib.error.HTTPError as http_error:
        body = http_error.read(300).decode(errors="replace")
        return failure(f"transport.opendata.ch returned HTTP {http_error.code}: {body}", time.monotonic() - started)
    except (urllib.error.URLError, TimeoutError, ValueError) as other:
        return failure(f"transport.opendata.ch could not be read: {other}", time.monotonic() - started)
    except LookupError as missing:
        return failure(str(missing.args[0]) if missing.args else "No results", time.monotonic() - started)
    return {"ok": True, "cached": cached, "error": None, "seconds": round(time.monotonic() - started, 3),
            "summary": summary, "display": display}


def _clock(stamp: str | None) -> str | None:
    """"2026-10-02T05:19:00+0200" → "05:19"."""
    return stamp[11:16] if stamp else None


def _duration(value: str | None) -> str | None:
    """"00d01:09:00" → "1h 09"."""
    match = re.fullmatch(r"(\d+)d(\d+):(\d+):\d+", value or "")
    if not match:
        return value
    days, hours, minutes = (int(x) for x in match.groups())
    hours += 24 * days
    return f"{hours}h {minutes:02d}" if hours else f"{minutes} min"


def _limit(args: dict, default: int = 4) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), MAX_RESULTS))
    except (TypeError, ValueError):
        return default


def _stop(stop: dict) -> dict:
    return {"station": (stop.get("station") or {}).get("name"), "departure": _clock(stop.get("departure")),
            "arrival": _clock(stop.get("arrival")), "platform": stop.get("platform"), "delay_min": stop.get("delay")}


# ---------- connections ----------

def connections(args: dict) -> dict:
    origin, destination = str(args.get("from") or "").strip()[:100], str(args.get("to") or "").strip()[:100]
    if not origin or not destination:
        return failure("from and to are required: Swiss station or place names, e.g. Zürich HB and Bern")
    params = [("from", origin), ("to", destination), ("limit", str(_limit(args)))]
    date, clock = str(args.get("date") or "").strip(), str(args.get("time") or "").strip()
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return failure("date must be YYYY-MM-DD")
        params.append(("date", date))
    if clock:
        if not re.fullmatch(r"\d{1,2}:\d{2}", clock):
            return failure("time must be HH:MM")
        params.append(("time", clock))
    if args.get("is_arrival_time"):
        params.append(("isArrivalTime", "1"))

    def shape(data: dict):
        found = data.get("connections") or []
        if not found:
            raise LookupError(f"No connections found from {origin!r} to {destination!r}")
        trips = []
        for c in found:
            legs = [{
                "line": f"{(s['journey'] or {}).get('category') or ''} {(s['journey'] or {}).get('number') or ''}".strip(),
                "operator": (s["journey"] or {}).get("operator"),
                "direction": (s["journey"] or {}).get("to"),
                "from": _stop(s["departure"]), "to": _stop(s["arrival"]),
            } if s.get("journey") else {"walk": True, "from": _stop(s["departure"]), "to": _stop(s["arrival"])}
                for s in c.get("sections") or []]
            trips.append({
                "date": (c["from"].get("departure") or "")[:10],
                "departure": _clock(c["from"].get("departure")), "arrival": _clock(c["to"].get("arrival")),
                "from": (c["from"].get("station") or {}).get("name"), "to": (c["to"].get("station") or {}).get("name"),
                "platform": c["from"].get("platform"), "delay_min": c["from"].get("delay"),
                "duration": _duration(c.get("duration")), "transfers": c.get("transfers"),
                "products": c.get("products"), "legs": legs,
            })
        first = trips[0]
        summary = {"from": first["from"], "to": first["to"], "connections": trips, "source": ATTRIBUTION}
        def fact(t: dict) -> list[str]:
            changes = "direct" if not t["transfers"] else f"{t['transfers']} change(s)"
            text = f"{t['duration']} · {changes} · {', '.join(t['products'] or [])}"
            text += f" · platform {t['platform']}" if t["platform"] else ""
            text += f" · +{t['delay_min']} min" if t["delay_min"] else ""
            return [f"{t['date']} {t['departure']} → {t['arrival']}", text]

        facts = [fact(t) for t in trips]
        query = urllib.parse.urlencode({"from": first["from"], "to": first["to"]})
        display = {"headline": f"{first['from']} → {first['to']}: next {first['departure']}, {first['duration']}",
                   "facts": facts, "links": [["Timetable on search.ch", f"https://search.ch/fahrplan/?{query}"]],
                   "warning": None, "attribution": ATTRIBUTION}
        return summary, display

    return _call("connections", params, 600, shape)


# ---------- departures ----------

def departures(args: dict) -> dict:
    station = str(args.get("station") or "").strip()[:100]
    if not station:
        return failure("station is required: a Swiss station name, e.g. Luzern")
    params = [("station", station), ("limit", str(_limit(args, 6)))]
    when = str(args.get("datetime") or "").strip()
    if when:
        try:
            datetime.strptime(when, "%Y-%m-%d %H:%M")
        except ValueError:
            return failure("datetime must be YYYY-MM-DD HH:MM")
        params.append(("datetime", when))

    def shape(data: dict):
        board = data.get("stationboard") or []
        name = (data.get("station") or {}).get("name") or station
        if not board:
            raise LookupError(f"No departures found for {station!r}")
        rows = [{"time": _clock(d["stop"].get("departure")), "line": f"{d.get('category') or ''} {d.get('number') or ''}".strip(),
                 "to": d.get("to"), "platform": d["stop"].get("platform"), "delay_min": d["stop"].get("delay"),
                 "operator": d.get("operator")} for d in board]
        summary = {"station": name, "departures": rows, "source": ATTRIBUTION}
        display = {"headline": f"{name}: next departures from {rows[0]['time']}",
                   "facts": [[r["time"], f"{r['line']} to {r['to']}" + (f" · platform {r['platform']}" if r["platform"] else "")
                              + (f" · +{r['delay_min']} min" if r["delay_min"] else "")] for r in rows],
                   "links": [["Departures on search.ch", f"https://search.ch/fahrplan/?{urllib.parse.urlencode({'stop': name})}"]],
                   "warning": None, "attribution": ATTRIBUTION}
        return summary, display

    return _call("stationboard", params, 120, shape)


CONNECTIONS = ApiTool(
    name="swiss_connections",
    title="Swiss connections · transport.opendata.ch",
    description=(
        "Swiss public transport (trains, buses, trams, boats) connections between two places in Switzerland: "
        "departure and arrival times, duration, changes, lines, platforms and delays. Optionally for a date "
        "and time, as departure or arrival time."
    ),
    properties={
        "from": {"type": "string", "description": "Departure station or place in Switzerland, e.g. Zürich HB"},
        "to": {"type": "string", "description": "Arrival station or place in Switzerland, e.g. Bern"},
        "date": {"type": "string", "description": "YYYY-MM-DD; today if not given"},
        "time": {"type": "string", "description": "HH:MM; now if not given"},
        "is_arrival_time": {"type": "boolean", "description": "true if time is the wanted arrival time"},
        "limit": {"type": "integer", "description": f"Connections to return, 1–{MAX_RESULTS}; 4 if not given"},
    },
    required=["from", "to"],
    run=connections,
    steps=[["API", "GET transport.opendata.ch/v1/connections · 1,000 route searches a day"],
           ["Cache", "10 minutes per query"]],
    attribution=ATTRIBUTION,
)

DEPARTURES = ApiTool(
    name="swiss_departures",
    title="Swiss departures · transport.opendata.ch",
    description="The next departures from a Swiss station or stop: time, line, destination, platform and delay.",
    properties={
        "station": {"type": "string", "description": "Swiss station or stop name, e.g. Luzern"},
        "datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM; now if not given"},
        "limit": {"type": "integer", "description": f"Departures to return, 1–{MAX_RESULTS}; 6 if not given"},
    },
    required=["station"],
    run=departures,
    steps=[["API", "GET transport.opendata.ch/v1/stationboard · 10,080 boards a day"],
           ["Cache", "2 minutes per station (delays change)"]],
    attribution=ATTRIBUTION,
)

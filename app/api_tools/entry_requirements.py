"""check_entry_requirements: visa and entry rules from canienter.com's free API,
GET https://api.canienter.com/free/check?passport=AFG&destination=FRA.

The free tier allows 5 checks per day per IP (X-RateLimit-* headers), so each
passport → destination → purpose answer is cached for 24 hours (the dataset is
re-verified daily) and the quota is reported. When the quota is spent the
tool says so instead of guessing. Data: canienter.com, CC BY-NC 4.0
(attribution required, non-commercial use).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ApiTool, failure

API = "https://api.canienter.com/free/check"
ATTRIBUTION = "Entry requirements: canienter.com (CC BY-NC 4.0)"
CACHE_SECONDS = 24 * 3600
PURPOSES = ("tourism", "business")
USER_AGENT = "rag-systems-cloud/1.0 (personal project; external-APIs agent)"
WARN_STATUSES = {"needs_review", "stale", "not_curated", "unknown"}

_cache: dict[tuple, tuple[float, dict]] = {}
_quota = {"limit": None, "remaining": None, "reset": None}
_lock = threading.Lock()


def quota() -> dict:
    with _lock:
        return dict(_quota)


def _code(value) -> str | None:
    code = str(value or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _summary(data: dict) -> dict:
    """The parts the agent needs."""
    rules = data.get("entry_rules") or {}
    verdict = data.get("verdict_verification") or {}
    rules_check = data.get("entry_rules_verification") or {}
    return {
        "passport": data.get("passport"),
        "destination": data.get("destination"),
        "purpose": (data.get("inputs") or {}).get("purpose"),
        "requirement": data.get("requirement"),
        "requirement_label": data.get("requirement_label"),
        "allowed_stay_days": data.get("allowed_stay_days"),
        "verdict_status": verdict.get("status"),
        "verdict_basis": verdict.get("basis"),
        "entry_rules_status": rules_check.get("status"),
        "entry_rules_last_verified": rules_check.get("last_verified"),
        "apply": data.get("apply"),
        "entry_rules": {k: v for k, v in rules.items() if k not in ("citations", "sources")},
        "defaults_applied": data.get("defaults_applied"),
        "disclaimer": data.get("disclaimer"),
        "dataset_version": data.get("dataset_version"),
        "request_id": data.get("request_id"),
        "source": ATTRIBUTION,
    }


def _display(s: dict, p: str, d: str) -> dict:
    flagged = {s.get("verdict_status"), s.get("entry_rules_status")} & WARN_STATUSES
    value = lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    facts = [["Requirement", s.get("requirement_label") or s.get("requirement")],
             ["Allowed stay", f"{s['allowed_stay_days']} days" if s.get("allowed_stay_days") else "—"],
             ["Purpose", s.get("purpose")],
             ["Verdict status", s.get("verdict_status")],
             ["Entry rules status", f"{s.get('entry_rules_status')} (last verified {s.get('entry_rules_last_verified')})"]]
    facts += [[k.replace("_", " ").capitalize(), value(v)] for k, v in (s.get("entry_rules") or {}).items()]
    facts.append(["Dataset", f"{s.get('dataset_version')} · request {s.get('request_id')}"])
    links = [["View on canienter.com", f"https://canienter.com/check/{p.lower()}-to-{d.lower()}"]]
    if (s.get("apply") or {}).get("url"):
        links.insert(0, ["Official application", s["apply"]["url"]])
    return {
        "headline": f"{(s.get('passport') or {}).get('name', p)} → {(s.get('destination') or {}).get('name', d)}: "
                    f"{s.get('requirement_label') or s.get('requirement')}",
        "facts": facts,
        "links": links,
        "warning": s.get("verdict_basis") if flagged else None,
        "attribution": ATTRIBUTION,
    }


def run(args: dict) -> dict:
    started = time.monotonic()
    p, d = _code(args.get("passport")), _code(args.get("destination"))
    purpose = args.get("purpose") if args.get("purpose") in PURPOSES else "tourism"
    if not p or not d:
        return failure("passport and destination must be ISO 3166-1 alpha-3 codes, e.g. AFG and FRA")
    key = (p, d, purpose)
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        data, cached = hit[1], True
    else:
        url = f"{API}?{urllib.parse.urlencode({'passport': p, 'destination': d, 'purpose': purpose})}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                _note_quota(response.headers)
                data = json.loads(response.read())
        except urllib.error.HTTPError as http_error:
            _note_quota(http_error.headers)
            if http_error.code == 429:
                reset = quota()["reset"] or "midnight UTC"
                return failure(f"The free canienter.com quota is used up for today (resets {reset}). "
                               "Check the official authority instead.", time.monotonic() - started)
            body = http_error.read(400).decode(errors="replace")
            return failure(f"canienter.com returned HTTP {http_error.code}: {body}", time.monotonic() - started)
        except (urllib.error.URLError, TimeoutError, ValueError) as other:
            return failure(f"canienter.com could not be reached: {other}", time.monotonic() - started)
        with _lock:
            _cache[key] = (time.time(), data)
        cached = False
    summary = _summary(data)
    return {"ok": True, "cached": cached, "error": None, "seconds": round(time.monotonic() - started, 3),
            "summary": summary, "display": _display(summary, p, d)}


def _note_quota(headers) -> None:
    if headers is None:
        return
    with _lock:
        for key, header in (("limit", "X-RateLimit-Limit"), ("remaining", "X-RateLimit-Remaining"),
                            ("reset", "X-RateLimit-Reset")):
            value = headers.get(header)
            if value is not None:
                _quota[key] = int(value) if key != "reset" and value.isdigit() else value


def status() -> dict | None:
    q = quota()
    if q["remaining"] is None:
        return {"text": "5 free checks per day · cached 24 h"}
    return {"text": f"{q['remaining']}/{q['limit']} free checks left today", "reset": q["reset"]}


TOOL = ApiTool(
    name="check_entry_requirements",
    title="Entry requirements · canienter.com",
    description=(
        "Visa / entry requirements for one passport and destination (canienter.com, re-verified daily): "
        "requirement, allowed stay, verification status, official application link and entry rules. "
        "Use ISO 3166-1 alpha-3 codes (Afghanistan AFG, France FRA, United Kingdom GBR, India IND)."
    ),
    properties={
        "passport": {"type": "string", "description": "Passport country, ISO 3166-1 alpha-3 (e.g. AFG)"},
        "destination": {"type": "string", "description": "Destination country, ISO 3166-1 alpha-3 (e.g. FRA)"},
        "purpose": {"type": "string", "enum": list(PURPOSES), "description": "Trip purpose; tourism if not stated"},
    },
    required=["passport", "destination"],
    run=run,
    steps=[["API", "GET api.canienter.com/free/check · 5 per day per IP"],
           ["Cache", "24 hours per passport, destination and purpose"]],
    attribution=ATTRIBUTION,
    status=status,
)

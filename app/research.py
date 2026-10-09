"""Research agent support: web search (Tavily), source extraction (HTML and
PDF) and source validation, for knowledge gaps the evidence evaluator finds.

Nothing here writes to the knowledge base: the graph (app/agent.py) sends
validated sources automatically through the ingestion pipeline
(common.ingest.ingest_url) into the separate research_chunks table.

Source validation is hybrid, like the evidence evaluator:
1. Deterministic checks per source: extraction worked and has enough text,
   official-looking domain (government / EU / public-body suffixes), age of
   the page or PDF from its own metadata.
2. Claude via Bedrock Converse with a forced tool whose schema is the
   Pydantic model SourceAssessment: authority, freshness, consistency (with
   the other sources), relevance to the gap, and reasons.
3. Deterministic acceptance: every score above its threshold.

A gap about live or structured data needs a data source, not a page: see
"data sources" below (gap classification, SourceProfile, profiling with
read-only probing and verification against the fetched pages).

TAVILY_API_KEY, or the SSM SecureString named by TAVILY_API_KEY_PARAMETER,
holds the search key.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Annotated, Callable, Literal
from urllib.parse import urlparse

import boto3
from pydantic import BaseModel, Field, field_validator

import llm
import structured
from common import scraping
from tracing import observation

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_PARAMETER = os.environ.get("TAVILY_API_KEY_PARAMETER", "/rag-systems/prod/tavily-api-key")
MAX_RESULTS = 5
MIN_TEXT_CHARS = 400            # less than this is not a usable source
MAX_PROMPT_CHARS = 2500         # per source, in the validation prompt
ACCEPT = {"authority": 0.6, "freshness": 0.5, "consistency": 0.6, "relevance": 0.6}
RESEARCH_TTL_DAYS = 90          # how long ingested research sources live
# Public bodies: government, EU and similar domains count as authoritative.
OFFICIAL_DOMAINS = re.compile(
    r"(\.gov(\.[a-z]{2})?$|\.gouv\.[a-z]{2}$|\.gv\.at$|\.admin\.ch$|\.gob\.[a-z]{2}$|\.govt\.[a-z]{2}$|"
    r"\.bund\.de$|europa\.eu$|\.gc\.ca$|\.go\.[a-z]{2}$|\.mil$|\.int$)")

_key_lock = threading.Lock()
_key: str | None = None


# ---------- search ----------

def _api_key() -> str:
    """The key is read once; a missing key is retried on the next search."""
    global _key
    with _key_lock:
        if not _key:
            key = os.environ.get("TAVILY_API_KEY", "").strip()
            if not key:
                try:
                    ssm = boto3.client("ssm", region_name=llm.BEDROCK_REGION)
                    key = ssm.get_parameter(Name=TAVILY_PARAMETER, WithDecryption=True)["Parameter"]["Value"].strip()
                except Exception as error:
                    raise RuntimeError(f"web search is not configured: no Tavily key in TAVILY_API_KEY or SSM "
                                       f"{TAVILY_PARAMETER} ({type(error).__name__})") from error
            _key = key
        return _key


def web_search(query: str, max_results: int = MAX_RESULTS, country: str | None = None) -> list[dict]:
    """Tavily search: [{"title", "url", "snippet", "score", "published"}].
    country (an English name, such as "united kingdom"): Tavily favours pages
    from it; a name Tavily rejects searches worldwide instead."""
    payload = {"query": query[:400], "max_results": max(1, min(max_results, 8)),
               "search_depth": "basic", "include_answer": False}
    if country:
        payload.update(topic="general", country=country.strip().lower())

    def search(payload: dict) -> dict:
        request = urllib.request.Request(TAVILY_URL, data=json.dumps(payload).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {_api_key()}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    try:
        data = search(payload)
    except urllib.error.HTTPError as error:
        if error.code != 400 or "country" not in payload:
            raise
        data = search({k: v for k, v in payload.items() if k not in ("topic", "country")})
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": (r.get("content") or "")[:500],
             "score": r.get("score"), "published": r.get("published_date")} for r in data.get("results", [])]


# ---------- extraction ----------

def fetch_source(url: str) -> dict:
    """Fetch and extract one candidate: HTML through trafilatura, PDF through
    pypdf. {"url", "kind", "title", "date", "sitename", "chars", "text", "error"},
    and for HTML its "links" (scraping.extract_links)."""
    started = time.monotonic()
    record = {"url": url, "kind": None, "title": None, "date": None, "sitename": None, "chars": 0,
              "text": "", "error": None}
    try:
        if not urlparse(url).scheme.startswith("http"):
            raise ValueError("not an http(s) URL")
        if scraping.is_pdf(url):
            data, content_type = scraping.fetch_bytes(url)
            record.update(kind="pdf", text=scraping.extract_pdf_text(data, url))
        else:
            data, content_type = scraping.fetch_bytes(url)
            if scraping.is_pdf(url, content_type):
                record.update(kind="pdf", text=scraping.extract_pdf_text(data, url))
            else:
                html = data.decode("utf-8", errors="replace")
                record.update(kind="html", text=scraping.extract_text(html, url),
                              links=scraping.extract_links(html, url), **scraping.extract_metadata(html, url))
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    record["chars"] = len(record["text"])
    record["seconds"] = round(time.monotonic() - started, 3)
    record.pop("author", None)
    return record


# ---------- validation ----------

Score = Annotated[float, Field(ge=0.0, le=1.0)]


class SourceAssessment(BaseModel):
    url: str
    publisher: str = Field(description="Who publishes the source, as the evidence shows it")
    authority: Score
    freshness: Score
    consistency: Score
    relevance: Score
    published_or_updated: str | None = Field(None, description="Date the text itself gives, if any (YYYY-MM-DD)")
    reasons: str = Field(description="One or two sentences on the scores; never an answer to the question")


class SourceAssessments(BaseModel):
    sources: list[SourceAssessment]


VALIDATE_PROMPT = (
    "You validate candidate web sources before they are added to the knowledge base. For each source "
    "score from 0 to 1: authority (an official government, public body or the service operator itself scores "
    "high; established publishers medium; blogs, forums, resellers and SEO pages low), freshness (does it look "
    "current; old dates, past timetables or outdated rules score low; unknown dates medium), consistency "
    "(does it agree with the other candidates on the facts they share; contradictions score low), relevance "
    "(does it contain the information listed as missing, for the country, region and subject the "
    "ASSISTANT BRIEF gives: a source about another country's services, such as another country's railways "
    "for a UK rail assistant, scores relevance 0 however well it matches the question's words). Judge only the text given; do not use outside "
    "knowledge about the facts, do not guess, and do NOT answer the question. Report through the "
    "record_source_assessment tool only."
)
VALIDATE_TOOL = "record_source_assessment"


def _deterministic(source: dict, now: datetime) -> dict:
    host = (urlparse(source["url"]).hostname or "").lower()
    official = bool(OFFICIAL_DOMAINS.search(host))
    age_days = None
    if source.get("date"):
        try:
            age_days = (now - datetime.fromisoformat(source["date"][:10]).replace(tzinfo=timezone.utc)).days
        except ValueError:
            pass
    freshness = None if age_days is None else 1.0 if age_days <= 365 else 0.7 if age_days <= 730 else 0.3
    return {"host": host, "official_domain": official, "https": source["url"].startswith("https://"),
            "extracted": not source.get("error") and source["chars"] >= MIN_TEXT_CHARS,
            "chars": source["chars"], "age_days": age_days, "freshness": freshness}


def assess_with_llm(question: str, missing: list[str], sources: list[dict],
                    brief: str = "") -> tuple[SourceAssessments, dict]:
    blocks = [f"SOURCE {i + 1}: {s['url']} ({s['kind']}, title: {s.get('title') or '—'}, "
              f"date in metadata: {s.get('date') or 'unknown'})\n{s['text'][:MAX_PROMPT_CHARS]}" for i, s in enumerate(sources)]
    prompt = ((f"ASSISTANT BRIEF:\n{brief}\n\n" if brief.strip() else "") + f"QUESTION:\n{question}\n\nMISSING FROM THE KNOWLEDGE BASE:\n" + ("\n".join(f"- {m}" for m in missing) or "—")
              + "\n\nCANDIDATES:\n\n" + "\n\n".join(blocks))
    with observation(as_type="generation", name="source_validator", model=llm.MODEL,
                     input=[{"role": "system", "content": VALIDATE_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=VALIDATE_PROMPT, name=VALIDATE_TOOL,
                              description="Record the source assessments.",
                              schema=SourceAssessments.model_json_schema(), max_tokens=1500, temperature=0)
        raw = reply.tool_input
        items = structured.as_list(raw.get("sources") if isinstance(raw, dict) else None)
        result = SourceAssessments(sources=[SourceAssessment.model_validate(structured.normalise(
            item, scores=("authority", "freshness", "consistency", "relevance"), strings=("publisher", "reasons")))
            for item in items if isinstance(item, dict)])
        usage = reply.usage
        gen.update(output=result.model_dump(), usage_details=usage)
    return result, usage


def validate(question: str, missing: list[str], sources: list[dict],
             assessor: Callable[..., tuple[SourceAssessments, dict]] = assess_with_llm,
             now: datetime | None = None, accept: dict[str, float] | None = None,
             ttl_days: int = RESEARCH_TTL_DAYS, brief: str = "") -> dict:
    """Score every candidate; accepted = extracted and every score above its
    threshold (accept: per score, default ACCEPT; a flow's Source validator
    node may change them). ttl_days: how long an ingested source is kept.
    brief: the flow's brief; a source about another country or subject is
    not relevant.
    Returns {"sources": [...], "accepted": n, "usage"}."""
    accept = {**ACCEPT, **(accept or {})}
    now = now or datetime.now(timezone.utc)
    checks = {s["url"]: _deterministic(s, now) for s in sources}
    usable = [s for s in sources if checks[s["url"]]["extracted"]]
    judged, usage = ({}, {"input_tokens": 0, "output_tokens": 0})
    if usable:
        result, usage = assessor(question, missing, usable, brief=brief)
        judged = {a.url: a for a in result.sources}
    out = []
    for source in sources:
        check, a = checks[source["url"]], judged.get(source["url"])
        if a is None:
            scores = {"authority": 0.0, "freshness": 0.0, "consistency": 0.0, "relevance": 0.0}
            reasons = source.get("error") or (f"Only {source['chars']} characters of text" if not check["extracted"]
                                              else "Not assessed")
            publisher, dated = None, source.get("date")
        else:
            # An official domain is authoritative whatever the model says.
            # Freshness takes the stricter of the metadata date and the
            # model's reading of the text: pages are often re-dated without
            # their content changing.
            scores = {
                "authority": max(a.authority, 0.9) if check["official_domain"] else a.authority,
                "freshness": min(a.freshness, check["freshness"]) if check["freshness"] is not None else a.freshness,
                "consistency": a.consistency,
                "relevance": a.relevance,
            }
            reasons, publisher, dated = a.reasons, a.publisher, source.get("date") or a.published_or_updated
        accepted = check["extracted"] and all(scores[k] >= v for k, v in accept.items())
        out.append({
            "url": source["url"], "kind": source["kind"], "title": source.get("title"), "publisher": publisher,
            "date": dated, "chars": source["chars"], "checks": check, "scores": scores,
            "overall": round(sum(scores.values()) / 4, 3), "accepted": accepted, "reasons": reasons,
            "excerpt": source["text"][:600], "ttl_days": ttl_days,
        })
    out.sort(key=lambda s: (not s["accepted"], -s["overall"]))
    return {"sources": out, "accepted": sum(s["accepted"] for s in out), "usage": usage}


# ---------- data sources: gaps about live or structured data ----------
#
# A gap about data that changes (departures, delays, live prices) is not
# filled by ingesting a page: it needs a data source to integrate. The
# research agent finds candidates (agent.py, the research_data role), then:
# profile_sources() profiles each one from the pages it fetched (one forced
# tool call, a second only when probing found the docs or spec it names),
# and verify_profile() keeps only what fetched pages support. Nothing here
# signs up, stores credentials or calls an authenticated endpoint: probing
# is a read-only GET through fetch_source().

MAX_PROFILES = 4              # candidates profiled per gap
MAX_PROFILE_PAGES = 4         # pages per candidate in the profiling prompt (docs, pricing, terms, spec)
MAX_PROFILES_PER_CANDIDATE = 3   # one candidate's separate APIs and feeds, each profiled
MAX_PROFILE_PAGE_CHARS = 3000
MAX_FACT_CHARS = 300          # a profile holds short facts, never page text
UNKNOWN = "unknown"
VAGUE = {"", "unknown", "not stated", "not specified", "n/a", "none found", "unclear"}

AccessMethod = Literal["rest_api", "soap_api", "push_feed", "bulk_download", "web_page", "scrape_only", "unknown"]
Auth = Literal["none", "api_key", "registration", "oauth", "contract", "unknown"]
Freshness = Literal["real-time", "minutes", "daily", "static", "unknown"]
Confidence = Literal["high", "medium", "low"]


def _choice(value, allowed: tuple, default: str):
    value = value.strip().lower() if isinstance(value, str) else value
    return value if value in allowed else default


class GapClassification(BaseModel):
    gap_type: Literal["static_content", "data_source"]
    reason: str = Field(description="One sentence; never an answer to the task")

    @field_validator("gap_type", mode="before")
    @classmethod
    def _known(cls, value):
        return _choice(value, ("static_content", "data_source"), "static_content")


CLASSIFY_PROMPT = (
    "You classify a knowledge gap in a retrieval-augmented assistant. static_content: the missing information "
    "is facts a web page can hold (rules, facilities, policies, how something works). data_source: it is live, "
    "frequently changing or structured data (departures, delays, platforms, live prices, availability, "
    "positions), or the task asks where to get such data, so a stored page could not keep it current. Do NOT "
    "answer the task. Report through the record_gap_type tool only."
)


def classify_gap(task: str, missing: list[str], brief: str = "") -> tuple[GapClassification, dict]:
    """For a gap the evidence evaluator did not judge (no evidence, so no model call)."""
    prompt = ((f"ASSISTANT BRIEF:\n{brief}\n\n" if brief.strip() else "") + f"TASK:\n{task}\n\nMISSING:\n"
              + ("\n".join(f"- {m}" for m in missing) or "—"))
    with observation(as_type="generation", name="gap_classifier", model=llm.MODEL,
                     input=[{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=CLASSIFY_PROMPT, name="record_gap_type",
                              description="Record the gap type.", schema=GapClassification.model_json_schema(),
                              max_tokens=200, temperature=0)
        result = GapClassification.model_validate(structured.normalise(reply.tool_input, strings=("reason",)))
        gen.update(output=result.model_dump(), usage_details=reply.usage)
    return result, reply.usage


class SourceProfile(BaseModel):
    candidate: int = Field(description="The candidate's number, as given")
    name: str
    provider: str
    access_method: AccessMethod
    auth: Auth
    pricing: str = Field(description="free / free tier with limits / paid, with tiers as text; 'unknown' if not stated")
    rate_limits: str = Field(description="As the pages state them; 'unknown' if not stated")
    freshness: Freshness
    format: str = Field(description="Data format and protocol (JSON, XML, SOAP, STOMP…); 'unknown' if not stated")
    coverage: str = Field(description="What and where the data covers")
    licence_and_terms: str = Field(description="Licence and terms, including any ban on scraping or redistribution; "
                                               "'unknown' if not stated")
    docs_url: str | None = None
    signup_url: str | None = None
    spec_url: str | None = Field(None, description="OpenAPI or WSDL spec, if a page links one")
    evidence_urls: list[str] = Field(default_factory=list, description="The pages each claim came from")
    confidence: Confidence
    unknowns: list[str] = Field(default_factory=list, description="What the pages do not say or could not be verified")

    @field_validator("access_method", "auth", "freshness", "confidence", mode="before")
    @classmethod
    def _known(cls, value, info):
        allowed = {"access_method": AccessMethod, "auth": Auth, "freshness": Freshness,
                   "confidence": Confidence}[info.field_name].__args__
        return _choice(value, allowed, "low" if info.field_name == "confidence" else UNKNOWN)

    @field_validator("name", "provider", "pricing", "rate_limits", "format", "coverage", "licence_and_terms",
                     mode="before")
    @classmethod
    def _short(cls, value):
        return (str(value) if value is not None else UNKNOWN).strip()[:MAX_FACT_CHARS]


class SourceProfiles(BaseModel):
    profiles: list[SourceProfile]


PROFILE_PROMPT = (
    "You profile candidate data sources (APIs, push feeds, bulk downloads, web pages) for an engineer who will "
    "integrate one of them. For each numbered candidate, fill the record_source_profiles tool from that "
    "candidate's pages ONLY: access method, authentication, pricing, rate limits, freshness, format, coverage, "
    "licence and terms (including any ban on scraping or redistribution), and the docs, sign-up and spec "
    "(OpenAPI/WSDL) links the pages give. Every claim must come from the pages: anything they do not state is "
    "'unknown' and is listed in unknowns; never use outside knowledge and never guess a URL. evidence_urls are "
    "the candidate's page URLs the claims came from. Keep each fact short; do not copy page text. confidence: "
    "high when the provider's own pages state access, pricing and terms; low when the key facts are missing. "
    "When a candidate's pages describe several distinct APIs or feeds (for example a push feed and a "
    "request/response API from the same provider), give one profile per API or feed, each with the "
    "candidate's number and its own name: they are integrated differently. Do NOT answer the user's question."
)


def profile_with_llm(task: str, brief: str, candidates: list[dict]) -> tuple[SourceProfiles, dict]:
    """candidates: [{"candidate", "name", "provider", "pages": [fetched source]}]."""
    blocks = []
    for c in candidates:
        pages = "\n\n".join(f"PAGE {p['url']} ({p.get('kind')}, title: {p.get('title') or '—'})\n"
                            f"{p['text'][:MAX_PROFILE_PAGE_CHARS]}" for p in c["pages"])
        blocks.append(f"CANDIDATE {c['candidate']}: {c['name']} (provider: {c.get('provider') or 'unknown'})\n{pages}")
    prompt = ((f"ASSISTANT BRIEF:\n{brief}\n\n" if brief.strip() else "") + f"GAP:\n{task}\n\nCANDIDATES:\n\n"
              + "\n\n".join(blocks))
    with observation(as_type="generation", name="source_profiler", model=llm.MODEL,
                     input=[{"role": "system", "content": PROFILE_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=PROFILE_PROMPT, name="record_source_profiles",
                              description="Record one profile per candidate.",
                              schema=SourceProfiles.model_json_schema(), max_tokens=3000, temperature=0)
        raw = reply.tool_input
        items = structured.as_list(raw.get("profiles") if isinstance(raw, dict) else None)
        result = SourceProfiles(profiles=[SourceProfile.model_validate(structured.normalise(
            item, lists=("evidence_urls", "unknowns"))) for item in items if isinstance(item, dict)])
        gen.update(output=result.model_dump(), usage_details=reply.usage)
    return result, reply.usage


def _vague(value: str | None) -> bool:
    return (value or "").strip().lower().rstrip(".") in VAGUE


# Words that show the model already listed a field in its own words
# ("specific rate limits", "pricing structure", "data formats").
FIELD_WORDS = {"access_method": ("access",), "auth": ("auth",), "freshness": ("fresh",),
               "pricing": ("pric", "cost"), "rate_limits": ("rate limit",), "format": ("format",),
               "coverage": ("coverage",), "licence_and_terms": ("licen", "terms")}


def _named(field: str, unknowns: list[str]) -> bool:
    return any(word in u.lower() for u in unknowns for word in FIELD_WORDS[field])


def verify_profile(profile: SourceProfile, pages: list[dict]) -> SourceProfile:
    """Keep only what the candidate's fetched pages support: a link must be a
    fetched page or appear in one, evidence must be fetched pages, and every
    unstated fact is named in unknowns. Without evidence, or without a
    stated access method or pricing, confidence is low."""
    urls = {p["url"] for p in pages}
    text = "\n".join(p.get("text") or "" for p in pages)
    unknowns = list(profile.unknowns)
    changes = {}
    for field in ("docs_url", "signup_url", "spec_url"):
        url = getattr(profile, field)
        if url and url not in urls and url not in text:
            changes[field] = None
            unknowns.append(f"{field} {url} is not on a fetched page")
    evidence = [u for u in dict.fromkeys(profile.evidence_urls) if u in urls]
    if not evidence:
        unknowns.append("no fetched page supports this profile")
    for field in ("access_method", "auth", "freshness", "pricing", "rate_limits", "format", "coverage",
                  "licence_and_terms"):
        value = getattr(profile, field)
        if (value == UNKNOWN if field in ("access_method", "auth", "freshness") else _vague(value)) \
                and not _named(field, profile.unknowns):
            unknowns.append(field)
    confidence = profile.confidence
    if not evidence or profile.access_method == UNKNOWN or _vague(profile.pricing):
        confidence = "low"
    return profile.model_copy(update={**changes, "evidence_urls": evidence, "confidence": confidence,
                                      "unknowns": list(dict.fromkeys(u[:MAX_FACT_CHARS] for u in unknowns))})


def profile_sources(task: str, candidates: list[dict], fetched: dict[str, dict], brief: str = "",
                    profiler: Callable[..., tuple[SourceProfiles, dict]] = profile_with_llm,
                    fetch: Callable[[str], dict] = fetch_source, max_profiles: int = MAX_PROFILES) -> dict:
    """candidates: [{"name", "provider", "urls"}] the research agent submitted;
    fetched: url → fetch_source() record. Profiles at most max_profiles,
    probes the docs or spec link a profile names when it was not fetched
    (only links that appear on a fetched page), re-profiles those with the
    new page, and verifies every profile.
    Returns {"profiles": [dict], "probes": [{"url", "ok", "error"}], "usage"}."""
    usage = {"input_tokens": 0, "output_tokens": 0}

    def add(more: dict) -> None:
        for key in usage:
            usage[key] += more.get(key, 0)

    chosen = []
    for number, c in enumerate(candidates[:max_profiles], 1):
        pages = [fetched[u] for u in dict.fromkeys(c.get("urls") or [])
                 if u in fetched and not fetched[u].get("error") and fetched[u].get("text")][:MAX_PROFILE_PAGES]
        chosen.append({"candidate": number, "name": str(c.get("name") or "")[:MAX_FACT_CHARS],
                       "provider": str(c.get("provider") or "")[:MAX_FACT_CHARS], "pages": pages})
    with_pages = [c for c in chosen if c["pages"]]
    if not with_pages:
        return {"profiles": [], "probes": [], "usage": usage}
    result, spent = profiler(task, brief, with_pages)
    add(spent)
    by_number = {c["candidate"]: c for c in with_pages}

    def grouped(items: list[SourceProfile], numbers) -> dict[int, list[SourceProfile]]:
        """candidate → its profiles: a provider's separate APIs and feeds each get one."""
        out = {}
        for p in items:
            if p.candidate in numbers and len(out.setdefault(p.candidate, [])) < MAX_PROFILES_PER_CANDIDATE:
                out[p.candidate].append(p)
        return out

    profiles = grouped(result.profiles, by_number)

    # Probe: the docs or spec page a profile names, if a fetched page links it.
    probes, reprofile = [], []
    for number, found in profiles.items():
        c = by_number[number]
        seen = {p["url"] for p in c["pages"]}
        text = "\n".join(p["text"] for p in c["pages"])
        url = next((u for profile in found for u in (profile.spec_url, profile.docs_url)
                    if u and u not in seen and u in text), None)
        if not url:
            continue
        page = fetch(url)
        probes.append({"url": url, "candidate": number, "ok": not page.get("error") and bool(page.get("text")),
                       "error": page.get("error")})
        if probes[-1]["ok"]:
            fetched[url] = page
            c["pages"] = c["pages"][:MAX_PROFILE_PAGES - 1] + [page]
            reprofile.append(c)
    if reprofile:
        result, spent = profiler(task, brief, reprofile)
        add(spent)
        profiles.update(grouped(result.profiles, {c["candidate"] for c in reprofile}))

    verified = [verify_profile(p, by_number[n]["pages"]).model_dump() for n in sorted(profiles) for p in profiles[n]]
    return {"profiles": verified, "probes": probes, "usage": usage}


# ---------- data sources: judge and recommendation ----------
#
# The judge sees only the verified profiles and the brief (never the search
# history), ranks them and recommends one with a fallback. Then, in code: a
# scrape-only source, or one whose terms forbid scraping or automated access,
# is never recommended, and the integration method follows the recommended
# source's access method.

Method = Literal["api_tool", "feed_consumer", "bulk_ingest", "page_ingest", "none"]
METHOD_FOR_ACCESS = {"rest_api": "api_tool", "soap_api": "api_tool", "push_feed": "feed_consumer",
                     "bulk_download": "bulk_ingest", "web_page": "page_ingest"}
# "may not scrape", "scraping is prohibited", "no automated access"…
FORBIDS_SCRAPING = re.compile(
    r"\b(prohibit\w*|forbid\w*|not (?:be )?(?:permitted|allowed)|may not|must not|no)\b[^.;]{0,60}?"
    r"\b(scrap\w*|crawl\w*|automated|robots?|harvest\w*)"
    r"|\b(scrap\w*|crawl\w*|automated (?:access|collection|means))\b[^.;]{0,40}?"
    r"\b(prohibited|forbidden|not (?:permitted|allowed))", re.IGNORECASE)


class RankedSource(BaseModel):
    name: str
    reason: str = Field(description="One line: why it ranks here, against the brief")


class Integration(BaseModel):
    """How to use the recommended source; a proposal, nothing is built."""
    tool_name: str = Field("", description="api_tool: a snake_case app/api_tools module name")
    description: str = Field("", description="What the tool or job does, in one or two sentences")
    inputs: str = Field("", description="api_tool: its input parameters, e.g. 'station (CRS code), rows (int)'")
    endpoint: str = Field("", description="The endpoint, feed or download it uses, from the profile")
    notes: str = Field("", description="Auth to arrange, limits, and for a feed the AWS shape "
                                       "(ECS/Fargate consumer, Kinesis or SQS)")


class SourceRecommendation(BaseModel):
    ranking: list[RankedSource] = Field(default_factory=list, description="Every profiled source, best first")
    recommended: str | None = Field(None, description="The name of the source to integrate, or null if none fits")
    fallback: str | None = Field(None, description="The next best source's name, or null")
    method: Method = "none"
    integration: Integration = Field(default_factory=Integration)
    not_recommended: list[RankedSource] = Field(default_factory=list,
                                                description="Sources to avoid, with the reason (terms, scraping)")
    rationale: str = Field("", description="Two or three sentences on the choice; never an answer to the question")

    @field_validator("method", mode="before")
    @classmethod
    def _known(cls, value):
        return _choice(value, Method.__args__, "none")


JUDGE_PROMPT = (
    "You choose how an assistant should get live or structured data it lacks. You see only profiles of "
    "candidate data sources, each built from the provider's pages. Rank them against the assistant's brief "
    "and the gap: freshness needed, cost, licence and terms risk, engineering effort and reliability. Prefer "
    "the provider's official API or feed. Recommend one source and a fallback (or null), and the "
    "integration method: api_tool (a request/response API wrapped as a new tool the assistant calls on "
    "demand), feed_consumer (a push feed consumed continuously into a store, read by a tool), bulk_ingest "
    "(scheduled downloads into the knowledge base) or page_ingest (stable facts on a web page). Never "
    "recommend scraping a website, and list scrape-only sources and sources whose terms forbid automated "
    "access under not_recommended. Use only what the profiles say; an 'unknown' is a risk, not a fact: when a "
    "source's terms are unknown, say they were not verified and never claim they forbid anything. "
    "Do NOT answer the user's question. Report through the record_source_recommendation tool only."
)


def judge_with_llm(task: str, brief: str, profiles: list[dict]) -> tuple[SourceRecommendation, dict]:
    keep = ("name", "provider", "access_method", "auth", "pricing", "rate_limits", "freshness", "format",
            "coverage", "licence_and_terms", "docs_url", "spec_url", "confidence", "unknowns")
    prompt = ((f"ASSISTANT BRIEF:\n{brief}\n\n" if brief.strip() else "") + f"GAP:\n{task}\n\nPROFILES:\n"
              + json.dumps([{k: p.get(k) for k in keep} for p in profiles], ensure_ascii=False, indent=1))
    with observation(as_type="generation", name="source_judge", model=llm.MODEL,
                     input=[{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=JUDGE_PROMPT, name="record_source_recommendation",
                              description="Record the ranking and recommendation.",
                              schema=SourceRecommendation.model_json_schema(), max_tokens=1500, temperature=0)
        raw = structured.normalise(reply.tool_input, strings=("rationale",))
        for key in ("ranking", "not_recommended"):
            raw[key] = [i for i in structured.as_list(raw.get(key)) if isinstance(i, dict)]
        if not isinstance(raw.get("integration"), dict):
            raw.pop("integration", None)
        result = SourceRecommendation.model_validate(raw)
        gen.update(output=result.model_dump(), usage_details=reply.usage)
    return result, reply.usage


TERMS_CLAIM = re.compile(r"\b(terms|licen[cs]e|violat\w*|prohibit\w*|forbid\w*|illegal|not (?:permitted|allowed))\b",
                         re.IGNORECASE)
TERMS_UNVERIFIED = "terms of use not verified"


def _grounded_reason(reason: str, profile: dict | None) -> str:
    """A reason may cite a source's terms only when its profile states them:
    with unknown terms, a claim about them becomes 'terms of use not verified'."""
    if profile is None or not _vague(profile.get("licence_and_terms")) or not TERMS_CLAIM.search(reason):
        return reason
    kept = [part.strip() for part in re.split(r"[;.]\s*|,\s*(?:and|so|but)\s+", reason)
            if part.strip() and not TERMS_CLAIM.search(part)]
    return "; ".join(kept + [TERMS_UNVERIFIED])


def ineligible(profile: dict) -> str | None:
    """Why a source must never be recommended, or None."""
    if profile.get("access_method") == "scrape_only":
        return "scrape only"
    if FORBIDS_SCRAPING.search(profile.get("licence_and_terms") or ""):
        return "its terms forbid scraping or automated access"
    return None


def judge_sources(task: str, profiles: list[dict], brief: str = "",
                  judge: Callable[..., tuple[SourceRecommendation, dict]] = judge_with_llm) -> dict:
    """{"recommendation": dict, "usage"}; no model call without profiles."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    if not profiles:
        return {"recommendation": SourceRecommendation(rationale="No data source could be profiled.").model_dump(),
                "usage": usage}
    result, usage = judge(task, brief, profiles)
    by_name = {(p.get("name") or "").strip().lower(): p for p in profiles}
    blocked = {name: why for name, p in by_name.items() if (why := ineligible(p))}

    def eligible(name: str | None) -> bool:
        return bool(name) and name.strip().lower() in by_name and name.strip().lower() not in blocked

    order = [name for name in [result.recommended, result.fallback, *(r.name for r in result.ranking)]
             if eligible(name)]
    order = list(dict.fromkeys(order))
    recommended, fallback = (order + [None, None])[:2]
    method = "none"
    if recommended:
        method = METHOD_FOR_ACCESS.get(by_name[recommended.strip().lower()].get("access_method"), result.method)
    integration = result.integration if recommended == result.recommended else Integration()
    avoid = {r.name.strip().lower(): r.model_copy(update={
        "reason": _grounded_reason(r.reason, by_name.get(r.name.strip().lower()))}) for r in result.not_recommended}
    for name, why in blocked.items():
        avoid.setdefault(name, RankedSource(name=by_name[name]["name"], reason=why))
    return {"recommendation": result.model_copy(update={
        "recommended": recommended, "fallback": fallback, "method": method, "integration": integration,
        "not_recommended": list(avoid.values())}).model_dump(), "usage": usage}

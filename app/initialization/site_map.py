"""Source analysis (epic #19, #13): what is on a chosen site, read politely
and within a budget — not a crawl.

1. robots.txt   read for our user agent; disallowed paths are never listed or
                fetched; a longer Crawl-delay than ours is honoured; a site
                that disallows its home page cannot be read
2. home page    for the names its navigation gives sections
3. sitemaps     the robots.txt Sitemap: lines, else /sitemap.xml; an index is
                followed into its child sitemaps; page URLs and lastmod
4. navigation   without a sitemap: the home page's links, preferring
                navigation, then up to MAX_SECTION_PAGES pages one level down
5. sections     URLs grouped by path, named from the navigation (not by
                "See all"-style link texts); on a flat site (FLAT_SITE_PATHS or
                more paths with fewer than MIN_SECTION_PAGES pages) those are
                gathered into "Other pages"

Sitemaps are read general ones first and constantly changing ones (news,
live, incidents) last, each with a fair share of the listing, so one large
sitemap does not crowd out the rest.

Same host only, at most MAX_URLS listed and MAX_REQUESTS requests per site.
A 401/403 on the home page means the site refuses automated access.
"""

import gzip
import re
import time
from collections import defaultdict
from typing import Callable
from urllib import robotparser
from urllib.parse import urlparse

import requests

from common import config, scraping

MAX_REQUESTS = 15
MAX_URLS = 2000
MAX_SECTION_PAGES = 12
SAMPLE_URLS = 5
# A first path segment that only groups others: the section is its first two.
CONTAINERS = {"help", "travel-information", "information", "info", "en", "en-gb", "gb", "uk", "pages", "content",
              "site", "topics", "guidance", "services", "s", "c", "hub", "our-services", "customer-services"}
SITEMAP_NS = re.compile(r"\{[^}]*\}")
MIN_SECTION_PAGES = 3       # smaller groups are gathered into "Other pages" …
FLAT_SITE_PATHS = 5         # … when there are at least this many of them (a flat site)
OTHER = "other"
# Sitemaps read first and last: general pages before what changes all the time.
SITEMAP_FIRST = re.compile(r"page|help|info|guide|faq|service|about|content|main", re.IGNORECASE)
SITEMAP_LAST = re.compile(r"news|live|incident|event|status|blog|press|post|tag|author|archive|search",
                          re.IGNORECASE)
# Link texts that name nothing ("See all", a bare host): not section names.
GENERIC_LINK = re.compile(r"^(see|view|show|browse) (all|more)\b|^(read|learn|find out|see) more$|^more$|^all$|"
                          r"^home$|^menu$|^(www\.)?[\w-]+(\.[\w-]+)+$", re.IGNORECASE)


class Blocked(RuntimeError):
    """The site cannot be read: robots.txt or a refusal of automated access."""


class Fetcher:
    """Requests within a budget, honouring robots.txt's Crawl-delay.
    fetch(url) -> (body bytes, content type); raises on HTTP errors."""

    def __init__(self, fetch: Callable | None = None, max_requests: int = MAX_REQUESTS, sleep: Callable = time.sleep):
        self.fetch = fetch or scraping.fetch_bytes
        self.max_requests = max_requests
        self.sleep = sleep
        self.requests: list[str] = []
        self.crawl_delay = 0.0

    @property
    def left(self) -> int:
        return self.max_requests - len(self.requests)

    def get(self, url: str) -> tuple[bytes, str] | None:
        """None once the budget is spent."""
        if self.left <= 0:
            return None
        if self.requests and self.crawl_delay > config.FETCH_MAX_DELAY_SECONDS:
            self.sleep(self.crawl_delay - config.FETCH_MAX_DELAY_SECONDS)   # on top of the polite delay
        self.requests.append(url)
        return self.fetch(url)


def _status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return response.status_code
    found = re.search(r"\b([45]\d\d)\b", str(error))
    return int(found.group(1)) if found else None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _clean(url: str) -> str:
    return url.split("#")[0].strip()


def read_robots(base_url: str, fetcher: Fetcher) -> tuple[robotparser.RobotFileParser, list[str], bool]:
    """(parser, sitemap URLs it names, found). A missing robots.txt allows everything."""
    parser = robotparser.RobotFileParser()
    try:
        got = fetcher.get(base_url.rstrip("/") + "/robots.txt")
        lines = got[0].decode("utf-8", errors="replace").splitlines() if got else []
        found = got is not None
    except Exception as error:
        if _status(error) in (401, 403):
            raise Blocked("refuses automated access") from error
        lines, found = [], False
    parser.parse(lines)
    delay = parser.crawl_delay(scraping.USER_AGENT)
    fetcher.crawl_delay = float(delay or 0)
    return parser, list(parser.site_maps() or []), found


def parse_sitemap(data: bytes, url: str) -> tuple[list[str], list[dict]]:
    """(child sitemaps, pages [{"url", "lastmod"}]) of one sitemap file."""
    import lxml.etree
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        root = lxml.etree.fromstring(data, parser=lxml.etree.XMLParser(resolve_entities=False, no_network=True,
                                                                       recover=True))
    except lxml.etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    children, pages = [], []
    for node in root:
        if not isinstance(node.tag, str):
            continue
        fields = {SITEMAP_NS.sub("", c.tag): (c.text or "").strip() for c in node if isinstance(c.tag, str)}
        if not fields.get("loc"):
            continue
        tag = SITEMAP_NS.sub("", node.tag)
        if tag == "sitemap":
            children.append(fields["loc"])
        elif tag == "url":
            pages.append({"url": _clean(fields["loc"]), "lastmod": fields.get("lastmod") or None})
    return children, pages


def analyse_site(base_url: str, fetch: Callable | None = None, max_requests: int = MAX_REQUESTS,
                 max_urls: int = MAX_URLS, sleep: Callable = time.sleep) -> dict:
    """{"status": ready | blocked | failed, "error", "robots": {...}, "how": sitemap | navigation,
    "requests": [...], "pages": [{"url", "lastmod"}], "nav": {url: anchor text}, "sections": [...]}"""
    fetcher = Fetcher(fetch, max_requests, sleep)
    host = _host(base_url)
    out = {"status": "ready", "error": None, "robots": {}, "how": None, "requests": fetcher.requests, "pages": [],
           "nav": {}, "sections": []}
    try:
        robots, sitemaps, found = read_robots(base_url, fetcher)
        allowed = lambda url: robots.can_fetch(scraping.USER_AGENT, url)
        out["robots"] = {"found": found, "crawl_delay": fetcher.crawl_delay, "sitemaps": sitemaps}
        home = base_url.rstrip("/") + "/"
        if not allowed(home):
            raise Blocked("its robots.txt does not allow us to read it")
        links = []
        try:
            got = fetcher.get(home)
            if got:
                links = scraping.extract_links(got[0].decode("utf-8", errors="replace"), home)
        except Exception as error:
            if _status(error) in (401, 403):
                raise Blocked("refuses automated access") from error
            raise
        same_host = lambda url: _host(url) == host
        out["nav"] = {l["url"]: l["text"] for l in links if l["nav"] and l["text"] and same_host(l["url"])}

        pages: dict[str, dict] = {}

        def add(page: dict) -> None:
            url = page["url"]
            if len(pages) < max_urls and url not in pages and same_host(url) and allowed(url):
                pages[url] = page

        order = lambda url: (1 if SITEMAP_LAST.search(url) else -1 if SITEMAP_FIRST.search(url) else 0)
        queue = sorted(dict.fromkeys(sitemaps or [base_url.rstrip("/") + "/sitemap.xml"]), key=order)
        seen = set()
        while queue and fetcher.left > 0 and len(pages) < max_urls:
            sitemap = queue.pop(0)
            if sitemap in seen:
                continue
            seen.add(sitemap)
            try:
                got = fetcher.get(sitemap)
            except Exception:
                continue        # a missing or broken sitemap: try the next, or the navigation
            if not got:
                break
            children, listed = parse_sitemap(got[0], sitemap)
            queue = sorted(queue + [c for c in children if _host(c) == host], key=order)
            # A fair share each, so one large sitemap does not fill the listing.
            share = max((max_urls - len(pages)) // max(min(len(queue) + 1, fetcher.left + 1), 1), 50)
            for page in listed[:share]:
                add(page)
        if pages:
            out["how"] = "sitemap"
        else:
            out["how"] = "navigation"
            ordered = sorted((l for l in links if same_host(l["url"]) and allowed(l["url"])),
                             key=lambda l: not l["nav"])
            for l in ordered:
                add({"url": l["url"], "lastmod": None})
            for l in [l for l in ordered if l["nav"]][:MAX_SECTION_PAGES]:
                if fetcher.left <= 0:
                    break
                try:
                    got = fetcher.get(l["url"])
                except Exception:
                    continue
                if got and "html" in (got[1] or "html"):
                    for sub in scraping.extract_links(got[0].decode("utf-8", errors="replace"), l["url"]):
                        add({"url": sub["url"], "lastmod": None})
        pages.pop(home, None)
        out["pages"] = list(pages.values())
        out["sections"] = sections(out["pages"], out["nav"])
    except Blocked as error:
        out.update(status="blocked", error=str(error))
    except (requests.RequestException, OSError) as error:
        out.update(status="failed", error=f"{type(error).__name__}: {error}"[:500])
    return out


# ---------- sections ----------

def section_key(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return "home"
    parts[-1] = re.sub(r"\.(html?|aspx?|php)$", "", parts[-1], flags=re.IGNORECASE)
    first = parts[0].lower()
    if first in CONTAINERS and len(parts) > 1:
        return f"{first}/{parts[1].lower()}"
    return first


def _readable(key: str) -> str:
    words = key.split("/")[-1].replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else key


def sections(pages: list[dict], nav: dict[str, str]) -> list[dict]:
    """Pages grouped by section_key, largest first: [{"key", "name", "path_prefix",
    "url_count", "pdf_count", "lastmod", "sample", "urls"}]."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for page in pages:
        groups[section_key(page["url"])].append(page)
    names = {}
    for url, text in nav.items():
        if GENERIC_LINK.search(text.strip()):
            continue
        key = section_key(url)
        path = "/" + "/".join(p for p in urlparse(url).path.split("/") if p)
        # The nav link that points at the section itself names it; else the most common one inside it.
        if path.lower().rstrip("/") == "/" + key or key not in names:
            names[key] = text
    small = [k for k, members in groups.items() if len(members) < MIN_SECTION_PAGES and k != OTHER]
    if len(small) >= FLAT_SITE_PATHS:   # many one- or two-page paths (a flat site): one section for them
        groups[OTHER] = [p for k in small for p in groups.pop(k)] + groups.get(OTHER, [])
        names[OTHER] = "Other pages"
    out = []
    for key, members in groups.items():
        dates = [p["lastmod"] for p in members if p.get("lastmod")]
        out.append({"key": key, "name": names.get(key) or _readable(key),
                    "path_prefix": None if key == OTHER else f"/{key}/",
                    "url_count": len(members),
                    "pdf_count": sum(scraping.is_pdf(p["url"]) for p in members),
                    "lastmod": max(dates) if dates else None,
                    "sample": [p["url"] for p in members[:SAMPLE_URLS]], "urls": members})
    out.sort(key=lambda s: (s["key"] == OTHER, -s["url_count"], s["key"]))
    return out

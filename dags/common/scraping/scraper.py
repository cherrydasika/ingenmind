import hashlib
import io
import ipaddress
import logging
import os
import random
import socket
import time

from urllib.parse import urldefrag, urljoin, urlparse

import requests
import trafilatura

from .. import config

logger = logging.getLogger(__name__)


def _polite_delay() -> None:
    time.sleep(random.uniform(config.FETCH_MIN_DELAY_SECONDS, config.FETCH_MAX_DELAY_SECONDS))


def fetch_html(url: str, timeout: int = 20) -> str:
    return _get(url, timeout).text


def fetch_bytes(url: str, timeout: int = 30) -> tuple[bytes, str]:
    """Raw body and content type, for PDFs (same retries as fetch_html)."""
    resp = _get(url, timeout)
    return resp.content, resp.headers.get("Content-Type", "")


# Pages come from admins, web search and links in pages, so a URL may point
# inside the deployment (the database, Langfuse, the cloud metadata service):
# server-side request forgery. Only public addresses are fetched, each
# redirect checked again. FETCH_ALLOW_PRIVATE=true allows private networks,
# for an intranet knowledge base; loopback, link-local (metadata) and
# reserved addresses are never fetched.
MAX_REDIRECTS = 5


class BlockedAddress(requests.RequestException):
    """The URL resolves to an address the scraper may not fetch."""


def _allow_private() -> bool:
    return os.environ.get("FETCH_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")


def check_url(url: str) -> None:
    """Raise BlockedAddress unless every address the URL's host resolves to may be fetched."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddress(f"not an http(s) URL: {url}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise requests.ConnectionError(f"cannot resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        never = ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        if never or (ip.is_private and not _allow_private()) or not (ip.is_global or ip.is_private):
            raise BlockedAddress(f"{parts.hostname} resolves to {ip}, which is not fetched")


def _request(url: str, timeout: int, headers: dict | None = None, stream: bool = False) -> requests.Response:
    """GET following redirects by hand, checking each address first."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT, **(headers or {})},
                            allow_redirects=False, stream=stream)
        if not resp.is_redirect:
            return resp
        url = urljoin(url, resp.headers["Location"])
    raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


def check_changed(url: str, etag: str | None = None, last_modified: str | None = None, timeout: int = 20) -> dict:
    """A conditional GET (If-None-Match, If-Modified-Since) without reading the body (R49):
    {"status": HTTP status (304: unchanged), "etag", "last_modified"}. The same address checks as a fetch."""
    headers = {**({"If-None-Match": etag} if etag else {}), **({"If-Modified-Since": last_modified} if last_modified else {})}
    with _request(url, timeout, headers, stream=True) as resp:
        return {"status": resp.status_code, "etag": resp.headers.get("ETag") or etag,
                "last_modified": resp.headers.get("Last-Modified") or last_modified}


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(config.FETCH_MAX_RETRIES):
        _polite_delay()
        try:
            resp = _request(url, timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            wait = config.FETCH_BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "Network error fetching %s (attempt %d/%d): %s, backing off %.1fs",
                url, attempt + 1, config.FETCH_MAX_RETRIES, exc, wait,
            )
            last_exc = exc
            time.sleep(wait)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            wait = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else config.FETCH_BACKOFF_BASE_SECONDS * (2**attempt)
            )
            logger.warning(
                "Got %d from %s (attempt %d/%d), backing off %.1fs",
                resp.status_code, url, attempt + 1, config.FETCH_MAX_RETRIES, wait,
            )
            last_exc = requests.HTTPError(f"{resp.status_code} error for {url}")
            time.sleep(wait)
            continue

        resp.raise_for_status()  # permanent client errors (e.g. 404) fail immediately, no retry
        return resp

    raise last_exc or requests.RequestException(f"Failed to fetch {url} after {config.FETCH_MAX_RETRIES} attempts")


USER_AGENT = "rag-ingest-bot/1.0"
NAV_TAGS = {"nav", "header"}
NAV_ROLES = {"navigation", "menubar", "menu"}


def extract_links(html: str, base_url: str) -> list[dict]:
    """Every link on a page, absolute and without its #fragment:
    [{"url", "text", "nav"}], in page order, one per URL; nav: inside a
    <nav>/<header> or an element with a navigation or menu role."""
    import lxml.html   # here: the text path does not need it
    try:
        root = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return []
    found: dict[str, dict] = {}
    for anchor in root.iter("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if urlparse(url).scheme not in ("http", "https"):
            continue
        nav = any(el.tag in NAV_TAGS or (el.get("role") or "").lower() in NAV_ROLES
                  for el in anchor.iterancestors() if isinstance(el.tag, str))
        text = " ".join(anchor.text_content().split())[:200]
        if url in found:
            found[url]["nav"] = found[url]["nav"] or nav
            found[url]["text"] = found[url]["text"] or text
        else:
            found[url] = {"url": url, "text": text, "nav": nav}
    return list(found.values())


def extract_text(html: str, url: str, headings: bool = False) -> str:
    """The page's main text; headings: as markdown, keeping its headings and
    lists (for structure-aware chunking, R21)."""
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False,
                               **({"output_format": "markdown"} if headings else {}))
    if not text:
        raise ValueError(f"No extractable text content found at {url}")
    return text.strip()


def is_pdf(url: str, content_type: str = "") -> bool:
    return "pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf")


def extract_pdf_text(data: bytes, url: str, max_pages: int = 200) -> str:
    """Text of a PDF's pages (pypdf); scanned PDFs without a text layer
    have none."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages[:max_pages]]
    text = "\n\n".join(p for p in pages if p)
    if not text:
        raise ValueError(f"No extractable text in the PDF at {url}")
    return text


def extract_metadata(html: str, url: str) -> dict:
    """Title, date (published or modified) and site name, when the page says."""
    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta is None:
        return {}
    return {k: v for k, v in {"title": meta.title, "date": meta.date, "sitename": meta.sitename,
                              "author": meta.author}.items() if v}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

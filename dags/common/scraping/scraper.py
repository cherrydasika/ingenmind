import hashlib
import io
import logging
import random
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


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(config.FETCH_MAX_RETRIES):
        _polite_delay()
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
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


def extract_text(html: str, url: str) -> str:
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
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

"""The embeddable chat widget (R50). A site adds one script tag:

    <script src="https://<ingenmind>/widget.js" data-widget="<channel_id>" async></script>

and gets a chat button that opens IngenMind's own page in a frame
(/widget/<channel_id>). Only the widget's allowed origins may frame it (the
page's Content-Security-Policy frame-ancestors). Public: anyone on those
sites asks, as the widget's own account, so the widget's limits apply.
Signed in: visitors ask as their own IngenMind account; the frame sends the
app's cookie only when the site is on the same site as IngenMind (an
intranet page under the same domain), else it shows a sign-in link.
"""

import html
import re
from urllib.parse import urlsplit

from . import ChannelError

MODES = ("public", "signed_in")
MAX_ORIGINS = 10


def check(settings: dict) -> dict:
    origins = settings.get("allowed_origins") or []
    if isinstance(origins, str):
        origins = [o for o in re.split(r"[\s,]+", origins) if o]
    cleaned = []
    for origin in origins[:MAX_ORIGINS + 1]:
        parts = urlsplit(origin.strip().rstrip("/"))
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query:
            raise ChannelError(f"{origin!r}: an origin is https://host (or http://host:port), no path")
        cleaned.append(f"{parts.scheme}://{parts.netloc.lower()}")
    if not cleaned:
        raise ChannelError("a widget needs at least one allowed origin (the site that embeds it)")
    if len(cleaned) > MAX_ORIGINS:
        raise ChannelError(f"at most {MAX_ORIGINS} allowed origins")
    mode = settings.get("mode") or "public"
    if mode not in MODES:
        raise ChannelError(f"mode: one of {', '.join(MODES)}")
    title = " ".join(str(settings.get("title") or "Ask us").split())[:40]
    greeting = " ".join(str(settings.get("greeting") or "").split())[:200]
    return {"allowed_origins": sorted(set(cleaned)), "mode": mode, "title": title, "greeting": greeting}


def csp(channel: dict) -> str:
    """The frame's policy: this app's own scripts, framed only by the allowed origins."""
    from web_security import CSP
    ancestors = " ".join(channel["config"]["allowed_origins"])
    return CSP.replace("frame-ancestors 'none'", f"frame-ancestors {ancestors}")


def page(channel: dict, static: str) -> str:
    """The frame's page; static: the versioned static prefix (/static/<version>)."""
    c = channel["config"]
    e = lambda v: html.escape(str(v), quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(c["title"])}</title><link rel="stylesheet" href="{static}/widget/frame.css"></head>
<body data-widget="{e(channel["channel_id"])}" data-mode="{e(c["mode"])}" data-greeting="{e(c.get("greeting") or "")}">
<header>{e(c["title"])}</header><main id="log" aria-live="polite"></main>
<form id="ask"><input id="q" type="text" autocomplete="off" maxlength="2000" placeholder="Ask a question" aria-label="Your question">
<button type="submit">Ask</button></form>
<script type="module" src="{static}/widget/frame.js"></script></body></html>"""

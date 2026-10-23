"""Browser-facing protections on every response and request (R05).

SecurityHeaders adds a strict Content Security Policy and the standard
headers to every HTTP response, so a future escaping bug can't load or run
someone else's script, and the app can't be framed.

SameOrigin refuses state-changing requests (anything but GET, HEAD and
OPTIONS) that a browser sent from another site: cross-site request forgery.
The session cookie is SameSite=Lax already; this is the second layer. A
request with neither an Origin nor a Sec-Fetch-Site header did not come from
a browser page (curl, tests, other services) and passes: it can only act
with credentials it holds itself.
"""
import os
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# 'unsafe-inline' for styles only: pages set style attributes (h() in ui.js)
# and Plotly adds style elements. Scripts come only from this origin.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def _extra_origins() -> set[str]:
    """ALLOWED_ORIGINS: more origins allowed to send state-changing requests,
    comma-separated (e.g. a site behind a proxy that rewrites Host)."""
    return {o.strip().rstrip("/").lower() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}


class SecurityHeaders:
    def __init__(self, app, https: bool = False):
        self.app = app
        self.headers = HEADERS + ([HSTS] if https else [])

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                if b"content-security-policy" in names:   # a page with its own policy (the widget's frame, R50)
                    names.add(b"x-frame-options")
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [h for h in self.headers if h[0] not in names]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def cross_site(method: str, headers: dict[str, str], extra: set[str] | None = None) -> str | None:
    """Why a request must be refused as cross-site, or None. headers: lower-case names."""
    if method in SAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None:
        origin = origin.strip().rstrip("/").lower()
        if origin == "null":
            return "an opaque origin"
        host = headers.get("x-forwarded-host") or headers.get("host") or ""
        if urlsplit(origin).netloc == host.split(",")[0].strip().lower():
            return None
        if origin in (extra if extra is not None else _extra_origins()):
            return None
        return f"origin {origin}"
    site = headers.get("sec-fetch-site")
    if site in ("cross-site", "same-site"):
        return f"a {site} request"
    return None


class SameOrigin:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        reason = cross_site(scope["method"], headers)
        if reason is None:
            return await self.app(scope, receive, send)
        response = JSONResponse({"error": f"Refused: this request came from another site ({reason})."}, 403)
        await response(scope, receive, send)

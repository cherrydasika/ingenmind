"""Sign-in, sign-out and the permission check on every API request.

Sign-in is delegated: with AUTH_MODE=oidc any OpenID Connect provider
(Cognito, Google, Microsoft Entra, Keycloak, Auth0…) authenticates the
person — authorization code flow with PKCE — and this app keeps only the
provider's subject ID and the profile in identity.py; it never sees a
password. AUTH_MODE=dev is a development sign-in for local Docker without a
provider: pick a user from a list. It answers only on localhost and the app
refuses to start with it when RAG_DEPLOYMENT=aws.

The signed-in user ID lives in a signed, HTTP-only cookie (SESSION_SECRET).
AuthMiddleware loads the user on every /api request and checks RULES: each
API route names the permission it needs (None: any signed-in user); a route
with no rule is refused, so a new route cannot be left open by mistake.

Settings:
  AUTH_MODE            oidc | dev
  SESSION_SECRET       signs the cookie (dev: generated per start if unset)
  OIDC_ISSUER          e.g. https://cognito-idp.eu-west-2.amazonaws.com/<pool>
  OIDC_CLIENT_ID, OIDC_CLIENT_SECRET (empty for a public client)
  OIDC_PROVIDER_NAME   shown on the sign-in button, e.g. "Google"
  OIDC_SCOPES          default "openid email profile"
  OIDC_LOGOUT_URL      the provider's logout URL; wins over discovery (Cognito needs it)
  ADMIN_EMAILS         comma-separated; these people may sign in as admins
  SESSION_COOKIE_SECURE  1 behind HTTPS
"""

import json
import logging
import os
import re
import secrets
from urllib.parse import parse_qs, quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import identity
import sessions

log = logging.getLogger(__name__)

MODES = ("oidc", "dev")
MODE = os.environ.get("AUTH_MODE", "dev").strip().lower() or "dev"
DEPLOYMENT = os.environ.get("RAG_DEPLOYMENT", "").strip().lower()
ISSUER = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
PROVIDER_NAME = os.environ.get("OIDC_PROVIDER_NAME", "").strip() or "your identity provider"
SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile").strip()
# Where the browser goes to sign out of the provider too, with {return} and
# {client_id} filled in; when set it wins over discovery's
# end_session_endpoint. Cognito needs it: its endpoint ignores the standard
# post_logout_redirect_uri (the user is left on its sign-in page), e.g.
# https://<domain>.auth.<region>.amazoncognito.com/logout?client_id={client_id}&logout_uri={return}
LOGOUT_URL = os.environ.get("OIDC_LOGOUT_URL", "").strip()
SESSION_MAX_AGE = 12 * 3600   # the cookie; a sign-in lasts at most a working day
# Send the cookie over HTTPS only. Off for http://localhost (and the SSM
# tunnel); turn on behind a domain with HTTPS.
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def config_error() -> str | None:
    """Why the app must not start, or None."""
    if MODE not in MODES:
        return f"AUTH_MODE={MODE!r} is not one of {', '.join(MODES)}"
    if MODE == "dev" and DEPLOYMENT == "aws":
        return "AUTH_MODE=dev is refused when RAG_DEPLOYMENT=aws: configure OIDC"
    if MODE == "oidc":
        missing = [name for name, value in (("OIDC_ISSUER", ISSUER), ("OIDC_CLIENT_ID", CLIENT_ID),
                                            ("SESSION_SECRET", os.environ.get("SESSION_SECRET")))
                   if not value]
        if missing:
            return f"AUTH_MODE=oidc needs {', '.join(missing)}"
    return None


def session_secret() -> str:
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret:
        log.warning("SESSION_SECRET is not set: sign-ins last until the web app restarts")
        secret = secrets.token_urlsafe(32)
    return secret


# ---------- the permission each API route needs ----------

ANY = None   # any signed-in user
# (methods, path pattern, permission or tuple of permissions any of which will do)
RULES = [
    ({"GET"}, r"/api/me", ANY),
    ({"GET"}, r"/api/(config|models|status|home|docs|docs/sample|knowledge-system)", ANY),
    ({"POST"}, r"/api/knowledge-system/reset", "manage_settings"),
    ({"GET", "POST"}, r"/api/setup(/message|/confirm|/back|/blueprint(/revise|/confirm)?"
                      r"|/sources/(discover|add|continue|\d+)|/content/(analyse|\d+|\d+/urls|\d+/ttl)|/back-to-sources"
                      r"|/plan|/plan/approve|/build/(start|retry)|/back-to-content|/evaluation(/prepare|/run)?|/readiness|/go-live|/relabel)?",
     "manage_settings"),
    ({"PUT"}, r"/api/me/theme", ANY),
    ({"POST"}, r"/api/(ask|ask/stream|agent/stream|agent/resume|session/reset)", "query_rag"),
    ({"GET"}, r"/api/agent(/session)?", "query_rag"),
    ({"GET"}, r"/api/agent/memory", "view_agent_memory"),
    ({"GET"}, r"/api/agent-memory", "view_agent_memory"),
    ({"GET"}, r"/api/flows(/[^/]+)?", ("manage_agents", "run_evaluations")),
    ({"GET", "POST", "PUT", "DELETE"}, r"/api/flows(/.*)?", "manage_agents"),
    ({"GET", "POST"}, r"/api/(flow-evals|evaluations)(/.*)?", "run_evaluations"),
    ({"GET"}, r"/api/(overview|ingestion|source-reports|knowledge/provenance)", "view_knowledge"),
    ({"POST"}, r"/api/(ingestion/trigger|sources/remove|source-reports/remove)", "manage_knowledge"),
    ({"GET", "POST"}, r"/api/adhoc(/.*)?", "upload_documents"),
    ({"GET", "POST", "PUT"}, r"/api/users(/.*)?", "manage_users"),
    # Own sessions: anyone signed in; others' need view_sessions (checked in web_api).
    ({"GET"}, r"/api/sessions(/[^/]+)?", ANY),
]
_COMPILED = [(methods, re.compile(f"^{pattern}$"), permission) for methods, pattern, permission in RULES]
NO_RULE = "no-rule"


def required_permission(method: str, path: str):
    """The permission a request needs: None for any signed-in user, a name or
    a tuple (any one will do), or NO_RULE when the route is not listed."""
    for methods, pattern, permission in _COMPILED:
        if method in methods and pattern.match(path):
            return permission
    return NO_RULE


def allowed(user: dict | None, permission) -> bool:
    if not user or user.get("status") != "active":
        return False
    if permission is ANY:
        return True
    if permission == NO_RULE:
        return False
    names = permission if isinstance(permission, tuple) else (permission,)
    return any(identity.can(user, name) for name in names)


class AuthMiddleware:
    """Every /api request: load the signed-in user from the cookie and check
    the route's permission. Pages and static files are public (the page asks
    /api/me and shows the sign-in screen); /auth/* is sign-in itself."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        session = scope.get("session") or {}
        user = None
        if session.get("user_id"):
            user = await run_in_threadpool(identity.get_user, session["user_id"])
            if not user or user["status"] != "active":
                session.clear()   # deactivated or removed: sign them out
                user = None
        scope.setdefault("state", {})["user"] = user
        permission = required_permission(scope["method"], scope["path"])
        if scope["path"] == "/api/me" or allowed(user, permission):
            return await self.app(scope, receive, send)
        if not user:
            response = JSONResponse({"error": "Sign in required", "signed_in": False}, 401)
        elif permission == NO_RULE:
            response = JSONResponse({"error": "This route has no permission rule"}, 403)
        else:
            response = JSONResponse({"error": "You don't have permission for this"}, 403)
        await response(scope, receive, send)


def current_user(request: Request) -> dict | None:
    """The signed-in user AuthMiddleware loaded, or None."""
    try:
        return getattr(request.state, "user", None)
    except (AttributeError, KeyError):   # a request that never passed the middleware
        return None


# ---------- OIDC ----------

_oauth = None


def _client():
    global _oauth
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth
        _oauth = OAuth()
        _oauth.register("oidc", client_id=CLIENT_ID, client_secret=CLIENT_SECRET or None,
                        server_metadata_url=f"{ISSUER}/.well-known/openid-configuration",
                        client_kwargs={"scope": SCOPES, "code_challenge_method": "S256"})
    return _oauth.oidc


def _fail(reason: str) -> RedirectResponse:
    return RedirectResponse(f"/?{urlencode({'auth_error': reason})}", 303)


async def login(request: Request) -> Response:
    if MODE == "dev":
        return RedirectResponse("/auth/dev", 303)
    return await _client().authorize_redirect(request, str(request.url_for("auth_callback")))


async def callback(request: Request) -> Response:
    if MODE != "oidc":
        return _fail("oidc_off")
    try:
        token = await _client().authorize_access_token(request)
    except Exception as error:   # state mismatch, bad code, unreachable provider
        log.warning("OIDC callback failed: %s", error)
        return _fail("provider")
    claims = token.get("userinfo") or {}
    if not claims.get("sub"):
        return _fail("provider")
    if claims.get("email") and claims.get("email_verified") is False:
        return _fail("email_unverified")
    try:
        user = await run_in_threadpool(identity.sign_in, ISSUER, claims["sub"], claims.get("email"),
                                       claims.get("given_name") or claims.get("name"))
    except identity.NotInvited:
        return _fail("not_invited")
    except identity.Disabled:
        return _fail("disabled")
    request.session.clear()
    request.session.update({"user_id": user["user_id"], "id_token": token.get("id_token", "")})
    return RedirectResponse("/", 303)


async def logout(request: Request) -> Response:
    """End this app's sign-in; with OIDC, also say where to end the
    provider's (the browser goes there next, then back to the app)."""
    id_token = request.session.get("id_token", "")
    user = current_user(request) or ({"user_id": request.session["user_id"]} if request.session.get("user_id") else None)
    if user:
        await run_in_threadpool(sessions.end, user["user_id"], request.session.get("sid"))
    request.session.clear()
    provider_logout = None
    if MODE == "oidc":
        # Without this the provider would still be signed in, and the next
        # "Sign in" would let the previous person straight back in.
        back = str(request.base_url).rstrip("/") + "/"
        if LOGOUT_URL:
            provider_logout = (LOGOUT_URL.replace("{client_id}", quote(CLIENT_ID, safe=""))
                               .replace("{return}", quote(back, safe="")))
        else:
            try:
                metadata = await _client().load_server_metadata()
                end = metadata.get("end_session_endpoint")
                if end:
                    params = {"client_id": CLIENT_ID, "post_logout_redirect_uri": back}
                    if id_token:
                        params["id_token_hint"] = id_token
                    provider_logout = f"{end}?{urlencode(params)}"
            except Exception as error:
                log.warning("could not read the provider's logout endpoint: %s", error)
    return JSONResponse({"ok": True, "provider_logout": provider_logout})


# ---------- development sign-in (AUTH_MODE=dev, localhost only) ----------

def _dev_allowed(request: Request) -> bool:
    return MODE == "dev" and DEPLOYMENT != "aws" and (request.url.hostname or "") in LOCAL_HOSTS


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Development sign-in</title>
<style>body{{font:15px system-ui,sans-serif;background:#0b1120;color:#e2e8f0;display:grid;place-items:center;
min-height:100vh;margin:0}}main{{background:#111a2e;border:1px solid #1e2a44;border-radius:12px;padding:28px;
width:min(420px,90vw)}}h1{{font-size:19px;margin:0 0 6px}}p{{color:#8b9ab8;font-size:13px}}
button,input{{font:inherit}}button{{display:block;width:100%;text-align:left;margin:8px 0;padding:10px 12px;
border-radius:8px;border:1px solid #2a3a5c;background:#16213a;color:inherit;cursor:pointer}}
button:hover{{border-color:#2563eb}}input{{width:100%;box-sizing:border-box;margin:4px 0 10px;padding:8px;
border-radius:6px;border:1px solid #2a3a5c;background:#0b1120;color:inherit}}small{{color:#8b9ab8}}</style>
</head><body><main>{body}</main></body></html>""")


def _escape(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


async def dev_page(request: Request) -> Response:
    if not _dev_allowed(request):
        return HTMLResponse("Development sign-in is off.", 404)
    users = [u for u in await run_in_threadpool(identity.list_users) if u["status"] == "active"]
    if not users:
        body = """<h1>Create the first admin</h1>
<p>Development sign-in (AUTH_MODE=dev, localhost only). No password: a real
deployment signs in through an identity provider.</p>
<form method="post"><label>First name<input name="first_name" required maxlength="80"></label>
<label>Email<input name="email" type="email" required></label><button type="submit">Create and sign in</button></form>"""
    else:
        buttons = "".join(
            f'<button name="user_id" value="{_escape(u["user_id"])}">{_escape(u["first_name"])} '
            f'<small>· {_escape(u["role"])} · {_escape(u["email"])}</small></button>' for u in users)
        body = f"""<h1>Sign in as</h1>
<p>Development sign-in (AUTH_MODE=dev, localhost only). No password: a real
deployment signs in through an identity provider.</p><form method="post">{buttons}</form>"""
    return _page(body)


async def dev_sign_in(request: Request) -> Response:
    if not _dev_allowed(request):
        return HTMLResponse("Development sign-in is off.", 404)
    # A plain URL-encoded form; parsed here (Starlette's form parser needs python-multipart).
    form = {key: values[0] for key, values in parse_qs((await request.body()).decode(errors="replace")).items()}
    users = await run_in_threadpool(identity.list_users)
    if not users:
        try:
            user = await run_in_threadpool(identity.create_user, form.get("first_name", ""), form.get("email", ""),
                                           "admin", None, "dev sign-in")
        except ValueError as error:
            return _page(f"<h1>Could not create the user</h1><p>{_escape(error)}</p><a href='/auth/dev'>Back</a>")
    else:
        user = next((u for u in users if u["user_id"] == form.get("user_id") and u["status"] == "active"), None)
        if not user:
            return _fail("not_invited")
    # No provider identity is linked, so the same person can later sign in
    # through a real provider by email.
    await run_in_threadpool(identity.record_login, user["user_id"])
    request.session.clear()
    request.session["user_id"] = user["user_id"]
    return RedirectResponse("/", 303)


# ---------- the signed-in user ----------

async def me(request: Request) -> Response:
    """Who is signed in (None when nobody is), and how to sign in."""
    user = current_user(request)
    return Response(json.dumps({
        "user": identity.public(user) if user else None,
        "auth": {"mode": MODE, "provider": PROVIDER_NAME if MODE == "oidc" else "Development sign-in"},
    }, default=str), media_type="application/json")


async def set_theme(request: Request) -> Response:
    body = await request.json()
    try:
        user = await run_in_threadpool(identity.update_user, current_user(request)["user_id"],
                                       {"theme": body.get("theme")}, current_user(request)["user_id"])
    except ValueError as error:
        return JSONResponse({"error": str(error)}, 400)
    return JSONResponse({"theme": user["theme"]})


ROUTES = [
    Route("/auth/login", login),
    Route("/auth/callback", callback, name="auth_callback"),
    Route("/auth/logout", logout, methods=["POST"]),
    Route("/auth/dev", dev_page),
    Route("/auth/dev", dev_sign_in, methods=["POST"]),
    Route("/api/me", me),
    Route("/api/me/theme", set_theme, methods=["PUT"]),
]

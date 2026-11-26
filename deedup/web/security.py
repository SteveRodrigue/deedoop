"""The web UI's security controls (milestone 6.5).

One guard runs before every route (it is an app-wide FastAPI dependency, so
a new route cannot forget it):

- SC1: a request that is not a GET or HEAD needs a signed-in admin and a
  valid CSRF token (form field ``csrf_token`` or header ``X-CSRF-Token``),
  and its ``Origin``/``Referer``, when sent, must be this site.
- Signed out, with "Signed-out visitors can browse" off, only the sign-in
  page (which shows the scan status), static files and ``/healthz`` load.
- Optional host-name allow-list against DNS rebinding.

:class:`SecurityHeaders` is plain ASGI middleware that adds CSP and friends
to every response and refuses oversized request bodies.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders

from .. import auth, db

SAFE_METHODS = frozenset({"GET", "HEAD"})
# Pages anyone may load, signed in or not, whatever the settings say.
PUBLIC_PATHS = frozenset({"/login", "/first-run", "/healthz", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)
# Non-GET routes that work signed out (they create the session).
SIGNIN_PATHS = frozenset({"/login", "/first-run"})
MAX_BODY = 1 << 20  # forms here are small; nothing uploads files

_LOCAL_PATH = re.compile(r"^/(?:[A-Za-z0-9_.~%-][^\\]*)?$")


def safe_next(target: str, default: str = "/") -> str:
    """A local path to redirect to: one leading slash, then not a slash or
    backslash (browsers read ``/\\evil.com`` as ``//evil.com``), no control
    characters."""
    if not target or len(target) > 2000 or any(ord(ch) < 32 for ch in target):
        return default
    if target.startswith("/?") or target == "/":
        return target
    return target if _LOCAL_PATH.match(target) else default


# -- addresses behind proxies ----------------------------------------------------


class Network:
    def __init__(self, trusted: tuple[str, ...]):
        self.nets = []
        for t in trusted:
            try:
                self.nets.append(ipaddress.ip_network(t, strict=False))
            except ValueError:
                pass

    def trusted(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in self.nets)

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else ""
        if not self.trusted(peer):
            return peer
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for h in reversed(hops):  # the rightmost address our proxies did not add
            if not self.trusted(h):
                return h[:64]
        return peer

    def is_https(self, request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else ""
        return self.trusted(peer) and request.headers.get("x-forwarded-proto", "").lower() == "https"


# -- the guard -------------------------------------------------------------------


def _same_origin(request: Request) -> bool:
    """When the browser says where a request came from, it must be here."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin is not None:
        return origin != "null" and urlsplit(origin).netloc == host
    referer = request.headers.get("referer")
    if referer is not None:
        return urlsplit(referer).netloc == host
    return True  # no header (old browsers, curl): the CSRF token still applies


def _wants_html(request: Request) -> bool:
    return "hx-request" not in request.headers and "text/html" in request.headers.get("accept", "text/html")


def _to_login(request: Request) -> HTTPException:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method not in SAFE_METHODS:
        nxt = safe_next(urlsplit(request.headers.get("referer", "")).path or "/")
    if not _wants_html(request):
        return HTTPException(401, "Sign in to do this.")
    return HTTPException(303, headers={"Location": "/login?next=" + quote(safe_next(nxt), safe="/")})


class Guard:
    def __init__(self, db_path: Path, network: Network):
        self.db_path = db_path
        self.network = network

    async def __call__(self, request: Request) -> None:
        conn = db.connect(self.db_path)
        try:
            sess = auth.load_session(conn, request.cookies.get(auth.COOKIE))
            settings = db.get_settings(conn)
            request.state.session = sess
            request.state.public_view = bool(settings["public_view"])
            request.state.client_ip = ip = self.network.client_ip(request)
            path = request.url.path

            hosts = [h.lower() for h in settings["allowed_hosts"]]
            if hosts and path != "/healthz":
                host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
                if host not in hosts:
                    raise HTTPException(400, "Unknown host name. Add it under Setup > Access.")

            if request.method in SAFE_METHODS:
                if sess is None and not request.state.public_view and not _public(path):
                    raise _to_login(request)
                return

            if not _same_origin(request):
                auth.event(conn, "csrf", sess.username if sess else "", ip, f"{request.method} {path}: other origin")
                raise HTTPException(403, "This form came from another site, so deedup refused it.")
            if path in SIGNIN_PATHS:
                return
            if sess is None:
                raise _to_login(request)
            token = request.headers.get("x-csrf-token")
            if token is None and request.headers.get("content-type", "").startswith(
                    ("application/x-www-form-urlencoded", "multipart/form-data")):
                token = (await request.form()).get("csrf_token")
            if not isinstance(token, str) or not secrets.compare_digest(token, sess.csrf):
                auth.event(conn, "csrf", sess.username, ip, f"{request.method} {path}: bad or missing token")
                raise HTTPException(403, "This form has expired. Go back, reload the page and try again.")
            auth.event(conn, "change", sess.username, ip, f"{request.method} {path}")
        finally:
            conn.close()


def _public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


# -- headers ---------------------------------------------------------------------


def inline_script_hashes(template: Path) -> list[str]:
    """CSP hashes of the inline ``<script>`` blocks of a template (the theme
    snippet in base.html). Computed at start, so editing the snippet cannot
    silently break or weaken the policy."""
    text = template.read_text(encoding="utf-8")
    out = []
    for body in re.findall(r"<script>(.*?)</script>", text, flags=re.S):
        out.append("'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'")
    return out


def content_security_policy(script_hashes: list[str]) -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(script_hashes),
        "style-src 'self' 'unsafe-inline'",  # style="" attributes in templates
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]).strip()


class SecurityHeaders:
    def __init__(self, app, csp: str):
        self.app = app
        self.headers = {
            "content-security-policy": csp,
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "same-origin",
            "permissions-policy": "camera=(), microphone=(), geolocation=()",
            "cross-origin-opener-policy": "same-origin",
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > MAX_BODY:
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b"Request too large"})
                return

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                h = MutableHeaders(scope=msg)
                for k, v in self.headers.items():
                    h[k] = v
                if "cache-control" not in h:
                    h["cache-control"] = "no-store"
            await send(msg)

        await self.app(scope, receive, send_with_headers)

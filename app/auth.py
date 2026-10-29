"""Officer login: one shared passcode, then a signed, HttpOnly session cookie.

Every path is private unless it is listed in PUBLIC_PATHS or under PUBLIC_PREFIXES (deny by
default), so a page or media URL added later is protected without anyone remembering to. Report
photos and texts are personal data under RA 10173.

- The passcode is compared as SHA-256 digests in constant time. Failed attempts are limited per
  client address (LoginLimiter); behind Modal's proxy the address comes from X-Forwarded-For.
- The cookie holds the expiry and the optional officer initials typed at login, signed with
  HMAC-SHA256 under a key derived from SESSION_SECRET and the passcode, so changing either one
  signs everyone out. Nothing is stored server-side.
- A dev server that answers only to localhost needs no login when OFFICER_PASSCODE is unset (the
  local workflow and the tests). Anywhere else the app refuses to start without one.

The initials are a convenience that prefills the review forms; with one shared passcode they are
self-declared, like the typed initials they replace, not proof of who acted.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import Settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "ulat_session"
LOGIN_PATH = "/login"
# The only routes reachable without a session. The webhook checks Telegram's own secret header.
PUBLIC_PATHS = frozenset({"/health", LOGIN_PATH, "/logout"})
PUBLIC_PREFIXES = ("/static/", "/telegram/webhook/")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
PASSCODE_MIN = 12
SESSION_SECRET_MIN = 32
MAX_TRACKED_CLIENTS = 10_000  # bounds the limiter's memory under a spray of addresses
NEXT_MAX = 2000
# A local path to return to after login: one leading slash (never "//host" or "/\\host").
_LOCAL_PATH = re.compile(r"/(?![/\\])[^\s\\]*")

# The signed-in officer's initials for this request, read by the templates' review forms.
current_officer: ContextVar[str] = ContextVar("current_officer", default="")


class AuthConfigError(RuntimeError):
    """The login is required here but not configured; the message names the fix."""


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def login_required(settings: Settings) -> bool:
    """True unless this is a localhost-only dev server without a passcode."""
    return bool(settings.officer_passcode.get_secret_value()) or any(
        host.lower() not in LOOPBACK_HOSTS for host in settings.allowed_hosts
    )


def safe_next(value: str | None) -> str:
    """Where to go after login: a local path only (no open redirect), never the login itself."""
    if not value or len(value) > NEXT_MAX or not _LOCAL_PATH.fullmatch(value):
        return "/"
    path = value.split("?", 1)[0]
    return "/" if path in (LOGIN_PATH, "/logout") else value


@dataclass(frozen=True)
class OfficerSession:
    officer: str  # initials typed at login, possibly empty
    expires_at: int  # Unix seconds


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionSigner:
    """`<payload>.<mac>`, both base64url; the payload is {"o": initials, "exp": unix seconds}."""

    def __init__(self, key: bytes, ttl_s: int, clock: Callable[[], float] = time.time) -> None:
        self._key = key
        self.ttl_s = ttl_s
        self._clock = clock

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self, officer: str) -> str:
        expires = int(self._clock()) + self.ttl_s
        payload = _b64(json.dumps({"o": officer, "exp": expires}, separators=(",", ":")).encode())
        return f"{payload}.{self._mac(payload)}"

    def read(self, token: str | None) -> OfficerSession | None:
        if not token or token.count(".") != 1:
            return None
        payload, mac = token.split(".")
        if not hmac.compare_digest(mac.encode(), self._mac(payload).encode()):
            return None
        try:
            data = json.loads(_unb64(payload))
            session = OfficerSession(str(data["o"]), int(data["exp"]))
        except (ValueError, KeyError, TypeError):
            return None
        return session if session.expires_at > self._clock() else None


class LoginLimiter:
    """Failed logins per client in a rolling window; past the limit, wait for the oldest to age
    out. In memory: a restart forgets, which only ever helps a legitimate officer."""

    def __init__(
        self,
        max_failures: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._clock = clock
        self._max_clients = max_clients
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, client: str) -> deque[float]:
        times = self._failures.get(client, deque())
        cutoff = self._clock() - self.window_s
        while times and times[0] <= cutoff:
            times.popleft()
        return times

    def retry_after(self, client: str) -> int:
        """Seconds until `client` may try again; 0 when it may try now."""
        times = self._recent(client)
        if len(times) < self.max_failures:
            return 0
        return max(1, int(times[0] + self.window_s - self._clock()) + 1)

    def failed(self, client: str) -> None:
        times = self._recent(client)
        times.append(self._clock())
        self._failures[client] = times
        self._failures.move_to_end(client)
        while len(self._failures) > self._max_clients:
            self._failures.popitem(last=False)  # forget the least recently failing client

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


class OfficerAuth:
    """The login policy of one app; `enabled` is False only on a localhost dev server."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.enabled = login_required(settings)
        passcode = settings.officer_passcode.get_secret_value()
        secret = settings.session_secret.get_secret_value()
        if self.enabled:
            if not passcode:
                where = ", ".join(settings.allowed_hosts)
                raise AuthConfigError(
                    f"Refusing to start without an officer login: the app answers to {where}. "
                    "Set OFFICER_PASSCODE and SESSION_SECRET (openssl rand -hex 32), or keep "
                    "ALLOWED_HOSTS=localhost,127.0.0.1 for local development."
                )
            if len(passcode) < PASSCODE_MIN:
                raise AuthConfigError(
                    f"OFFICER_PASSCODE must be at least {PASSCODE_MIN} characters."
                )
            if len(secret) < SESSION_SECRET_MIN:
                raise AuthConfigError(
                    f"SESSION_SECRET must be at least {SESSION_SECRET_MIN} characters "
                    "(openssl rand -hex 32)."
                )
        self._passcode_digest = hashlib.sha256(passcode.encode()).digest()
        # The passcode is part of the signing key, so changing it signs every session out.
        key = hmac.new(secret.encode(), b"ulat-session-v1\0" + self._passcode_digest, "sha256")
        self.signer = SessionSigner(key.digest(), settings.session_ttl_hours * 3600, clock)
        self.limiter = LoginLimiter(
            settings.login_failures_per_window, settings.login_window_minutes * 60, monotonic
        )

    def check_passcode(self, given: str) -> bool:
        digest = hashlib.sha256(given.encode()).digest()
        return self.enabled and hmac.compare_digest(digest, self._passcode_digest)

    def failed_login(self, client: str) -> None:
        self.limiter.failed(client)
        logger.warning("officer login failed: wrong passcode")  # no address: personal data

    def session(self, request: Request) -> OfficerSession | None:
        return self.signer.read(request.cookies.get(SESSION_COOKIE))

    def client_address(self, request: Request) -> str:
        if self.settings.proxy_headers:
            # The proxy appends the address it saw; earlier entries are whatever the client sent.
            hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
            if hops and hops[-1]:
                return hops[-1]
        return request.client.host if request.client else "unknown"

    def https(self, request: Request) -> bool:
        return request.url.scheme == "https" or self.settings.proxy_headers

    def sign_in(self, response: Response, request: Request, officer: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            self.signer.issue(officer),
            max_age=self.signer.ttl_s,
            path="/",
            httponly=True,
            secure=self.https(request),
            samesite="lax",
        )

    def sign_out(self, response: Response, request: Request) -> None:
        response.delete_cookie(
            SESSION_COOKIE, path="/", httponly=True, secure=self.https(request), samesite="lax"
        )

    @staticmethod
    def challenge(request: Request) -> Response:
        """No session: send a page view to the login, and tell a script (live updates, htmx)
        with 401 so it can send the whole page there instead of swapping a login form in."""
        back = "/"  # a form POST has no page to return to; its page's own link is lost anyway
        if request.method in ("GET", "HEAD"):
            query = request.url.query
            back = safe_next(request.url.path + (f"?{query}" if query else ""))
        target = f"{LOGIN_PATH}?next={quote(back, safe='/')}"
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": target})
        accept = request.headers.get("accept", "")
        if "text/html" not in accept and ("json" in accept or "event-stream" in accept):
            return JSONResponse(
                {"detail": "Officer login required.", "login": target}, status_code=401
            )
        return RedirectResponse(target, status_code=303)


def install_login_gate(app, auth: OfficerAuth) -> None:
    """The middleware that turns away every request without a session, except public paths."""

    @app.middleware("http")
    async def officer_login(request: Request, call_next):
        session = auth.session(request) if auth.enabled else None
        request.state.officer_session = session
        if auth.enabled and session is None and not is_public(request.url.path):
            return auth.challenge(request)
        token = current_officer.set(session.officer if session else "")
        try:
            return await call_next(request)
        finally:
            current_officer.reset(token)

"""Officer login: every route is private unless public by name, and the passcode is guarded."""

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import (
    SESSION_COOKIE,
    AuthConfigError,
    LoginLimiter,
    OfficerAuth,
    SessionSigner,
    is_public,
    safe_next,
)
from app.main import create_app
from tests.test_drafts_web import seed_houses

HOST = "ulat.example"
PASSCODE = "correct-horse-battery"
HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def public_settings(settings):
    return settings.model_copy(
        update={
            "allowed_hosts": [HOST],
            "officer_passcode": SecretStr(PASSCODE),
            "session_secret": SecretStr("s" * 40),
            "login_failures_per_window": 3,
        }
    )


@pytest.fixture
def client(public_settings):
    with TestClient(create_app(public_settings), base_url=f"https://{HOST}") as client:
        yield client


def sign_in(client, officer=""):
    response = client.post(
        "/login",
        data={"passcode": PASSCODE, "officer": officer, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


def private_routes(app):
    """Every registered route that is not public, with sample path parameters."""
    for route in app.routes:
        if isinstance(route, APIRoute) and not is_public(route.path):
            path = route.path.replace("{report_id:int}", "1").replace("{draft_id:int}", "1")
            for name in ("incident_id", "media_id", "action", "path_token", "draft_id"):
                path = path.replace("{" + name + "}", "1")
            for method in route.methods:
                yield method, path


def test_every_private_route_sends_a_visitor_to_the_login(client):
    routes = list(private_routes(client.app))
    assert ("GET", "/reports") in routes and ("GET", "/media/1") in routes
    assert ("POST", "/drafts") in routes and ("GET", "/events") in routes
    for method, path in routes:
        response = client.request(method, path, headers=HTML, follow_redirects=False)
        assert response.status_code == 303, (method, path)
        location = response.headers["location"]
        assert location.startswith("/login?next="), (method, path)
        if method == "GET":
            assert location == f"/login?next={path}"
        else:
            assert location == "/login?next=/"  # a form POST cannot be replayed by a redirect


def test_scripts_get_401_not_a_login_page(client):
    token = client.get("/events/token", headers={"Accept": "application/json"})
    assert token.status_code == 401 and token.json()["login"] == "/login?next=/events/token"
    stream = client.get("/events", headers={"Accept": "text/event-stream"})
    assert stream.status_code == 401
    swap = client.get("/reports", headers={"HX-Request": "true"})
    assert swap.status_code == 401 and swap.headers["HX-Redirect"] == "/login?next=/reports"


def test_public_routes_stay_open(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/login").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
    # The webhook is public at the gate (it checks Telegram's secret); off in polling mode.
    assert client.post("/telegram/webhook/abc", json={}).status_code == 404


def test_signed_in_officer_reaches_every_page(client):
    sign_in(client)
    for path in ("/", "/reports", "/incidents", "/review", "/drafts", "/map", "/damaged-houses"):
        response = client.get(path, headers=HTML, follow_redirects=False)
        assert response.status_code == 200, path
        assert "Sign out" in response.text
    assert client.get("/events/token").status_code == 200
    assert client.get("/media/999").status_code == 404  # past the gate: the route answers


def test_login_cookie_is_signed_httponly_secure_and_lax(client):
    cookie = sign_in(client).headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
    assert "max-age=43200" in lowered  # SESSION_TTL_HOURS=12


def test_login_returns_to_the_page_asked_for(client):
    page = client.get("/login?next=/incidents/3")
    assert 'value="/incidents/3"' in page.text
    response = client.post(
        "/login", data={"passcode": PASSCODE, "next": "/incidents/3"}, follow_redirects=False
    )
    assert response.headers["location"] == "/incidents/3"


@pytest.mark.parametrize(
    "value",
    ["//evil.example", "https://evil.example/", "/\\evil.example", "/login", "", None, "x"],
)
def test_next_is_never_an_open_redirect(value):
    assert safe_next(value) == "/"


def test_wrong_passcode_is_refused_and_rate_limited_per_client(client):
    for _ in range(3):
        wrong = client.post("/login", data={"passcode": "guess", "officer": "JDC"})
        assert wrong.status_code == 401 and "Wrong passcode" in wrong.text
        assert 'value="JDC"' in wrong.text  # the initials are kept for the next try
    limited = client.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
    assert limited.status_code == 429  # even the right passcode waits out the window
    assert int(limited.headers["Retry-After"]) > 0
    assert "Too many wrong passcodes" in limited.text
    assert SESSION_COOKIE not in client.cookies


def test_rate_limit_counts_each_client_behind_the_proxy(public_settings):
    settings = public_settings.model_copy(update={"proxy_headers": True})
    with TestClient(create_app(settings), base_url=f"https://{HOST}") as client:
        for _ in range(3):
            # Only the last hop counts: a client cannot dodge the limit by faking earlier hops.
            headers = {"X-Forwarded-For": f"10.0.0.{_}, 203.0.113.7"}
            client.post("/login", data={"passcode": "guess"}, headers=headers)
        blocked = {"X-Forwarded-For": "203.0.113.7"}
        other = {"X-Forwarded-For": "198.51.100.2"}
        assert (
            client.post("/login", data={"passcode": PASSCODE}, headers=blocked).status_code == 429
        )
        allowed = client.post(
            "/login", data={"passcode": PASSCODE}, headers=other, follow_redirects=False
        )
        assert allowed.status_code == 303


def test_limiter_forgets_old_failures_and_bounds_memory():
    now = [0.0]
    limiter = LoginLimiter(2, 60, clock=lambda: now[0], max_clients=2)
    limiter.failed("a")
    limiter.failed("a")
    assert limiter.retry_after("a") == 61
    now[0] = 61
    assert limiter.retry_after("a") == 0
    for client in ("b", "c", "d"):
        limiter.failed(client)
    assert len(limiter._failures) == 2


def test_tampered_expired_or_stale_sessions_are_refused(public_settings):
    now = [1_000.0]
    signer = SessionSigner(b"k" * 32, 60, clock=lambda: now[0])
    token = signer.issue("JDC")
    assert signer.read(token).officer == "JDC"
    payload, mac = token.split(".")
    assert signer.read(f"{payload}x.{mac}") is None
    assert SessionSigner(b"other" * 8, 60, clock=lambda: now[0]).read(token) is None
    now[0] += 61
    assert signer.read(token) is None
    # A new passcode signs every session out.
    old = OfficerAuth(public_settings).signer.issue("")
    rotated = public_settings.model_copy(update={"officer_passcode": SecretStr("another-passcode")})
    assert OfficerAuth(rotated).signer.read(old) is None


def test_sign_out_ends_the_session(client):
    sign_in(client)
    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert client.get("/reports", headers=HTML, follow_redirects=False).status_code == 303


def test_officer_initials_prefill_the_decision_forms(client):
    seed_houses(client.app.state.engine)
    sign_in(client, officer="  JDC   CDRRMO ")
    draft = client.post("/drafts/damaged-houses", follow_redirects=False)
    page = client.get(draft.headers["location"]).text
    assert "Signed in as JDC CDRRMO" in page
    assert page.count('value="JDC CDRRMO"') == 2  # approve and reject: "decided by"


def test_localhost_dev_server_needs_no_login(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert client.get("/reports").status_code == 200
        assert client.get("/login", follow_redirects=False).headers["location"] == "/"
        assert "Sign out" not in client.get("/").text


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"allowed_hosts": ["localhost", HOST]}, "Refusing to start without an officer login"),
        (
            {"allowed_hosts": [HOST], "officer_passcode": SecretStr("short")},
            "at least 12 characters",
        ),
        (
            {"allowed_hosts": [HOST], "officer_passcode": SecretStr(PASSCODE)},
            "SESSION_SECRET must be at least 32",
        ),
    ],
)
def test_a_public_app_refuses_to_start_without_a_proper_login(settings, update, message):
    with pytest.raises(AuthConfigError, match=message):
        create_app(settings.model_copy(update=update))


def test_a_local_passcode_turns_the_login_on(settings):
    local = settings.model_copy(
        update={"officer_passcode": SecretStr(PASSCODE), "session_secret": SecretStr("s" * 40)}
    )
    with TestClient(create_app(local), base_url="http://localhost") as client:
        response = client.get("/reports", headers=HTML, follow_redirects=False)
        assert response.status_code == 303
        assert "secure" not in sign_in(client).headers["set-cookie"].lower()  # plain http

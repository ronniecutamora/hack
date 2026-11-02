"""FastAPI app: health check, reports, incidents, officer drafts (RDANA, damaged houses, PCIC
Notices of Loss) and evidence media.

Run with: uv run uvicorn app.main:app --reload

Every page and media URL needs an officer login (app.auth), except on a localhost-only dev
server without OFFICER_PASSCODE. The app answers only to ALLOWED_HOSTS (against DNS rebinding),
and every state-changing request must come from the app's own pages (a same-origin check), so
another site open in the officer's browser cannot generate or approve drafts.
"""

import asyncio
import contextlib
import hashlib
import json
import mimetypes
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import cache, partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlmodel import Session, col, func, select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from telegram.ext import Application

from app.accuracy import WEAK_BELOW, accuracy_page
from app.agent_trace import TRACE_SINCE, draft_calls, report_traces
from app.auth import (
    LOGIN_PATH,
    OfficerAuth,
    current_officer,
    install_login_gate,
    safe_next,
)
from app.bot import build_application, start_upkeep, stop_upkeep
from app.config import Settings, get_settings
from app.dashboard import (
    active_incidents,
    ago,
    incident_trust,
    nav_counts,
    overview,
    plain_output_label,
    report_href,
)
from app.db import init_db, make_engine, make_recorder
from app.deadlines import deadline
from app.drafts import (
    DECIDED_BY_MAX,
    EVENT_NAME_MAX,
    EVENT_TYPE_MAX,
    KIND_DAMAGED_HOUSES,
    LIVE_CSV_STATUS,
    REASON_MAX,
    REASON_MIN,
    DraftError,
    DraftService,
    FillRefused,
    clean_text,
    csv_status,
    damaged_houses,
    damaged_houses_csv,
    form_view,
    output_label,
    parse_event_at,
    pending_report_ids,
    predates_pending_bucket,
    print_stamp,
    regenerate_form,
)
from app.eval_results import RESULTS_PATH
from app.facts import fact_lines
from app.fillable import PERSONAL_NOTE
from app.gazetteer import BARANGAYS, BY_NAME
from app.geo import GEO_SOURCES, boundaries_geojson
from app.incidents import IncidentService
from app.live import ChangeHub, close_on_shutdown_signals, event_stream
from app.mapdata import map_features
from app.meta.factory import make_agent_llm, make_pair_judge
from app.meta.llm import AgentLLM
from app.models import (
    MANILA,
    STATUS_NOT_A_REPORT,
    Assessment,
    Draft,
    Incident,
    Media,
    PhotoAssessment,
    Report,
    Reporter,
    manila_time,
    utcnow,
)
from app.modes import (
    GLOSSARY,
    GUIDE_COOKIE,
    MODE_COOKIE,
    MODE_MAX_AGE_S,
    MODE_PARAM,
    MODES,
    PLAIN_DONE,
    PLAIN_VERDICTS,
    current_mode,
    drafts_help,
    glossary,
    guide_steps,
    plain,
    plain_reason,
    plain_signal,
    plain_status,
    presenter,
    resolve,
    technical,
)
from app.nav import NAV, destinations, more_current, nav_current, technical_only
from app.nol import PHOTO_EVIDENCE, PHOTO_PAGE, deadline_table
from app.nol_form import commodity_for_kind
from app.pcic_lines import enabled as enabled_commodities
from app.questions import PLACE_LISTED_REASON, current, plan, questions
from app.questions import photos as question_photos
from app.rdana import RDANA_DEADLINE, EventInput
from app.review import (
    ACTION_LABELS,
    NOTE_MAX,
    OFFICER_MAX,
    PLACE_REASON_MIN,
    SIGNAL_VERDICTS,
    TREE_VERDICTS,
    ReviewError,
    ReviewService,
    officer_name,
    queue,
    review_cards,
    timeline,
)
from app.snapshots import SnapshotStore
from app.telegram_webhook import WEBHOOK_PREFIX, TelegramWebhook
from app.trust import model_spend

REPORT_LIMIT = 200
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
FORM_MAX_BYTES = 16_384
FILL_MAX_BYTES = 131_072  # a whole RDANA form with long answers, percent-encoded

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"


@cache
def static_url(name: str) -> str:
    """A static file's URL with a content hash, so browsers never run a stale script or style."""
    digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


def live_tokens(request: Request) -> str:
    """The live topic tokens as of this render, so the page's script refreshes only what changes
    after it (a change it already shows costs at most one extra refresh)."""
    hub = getattr(request.app.state, "live", None)
    return json.dumps(hub.snapshot()["tokens"] if hub else {}, separators=(",", ":"))


def nav_badges(request: Request) -> dict[str, int]:
    """The counts on the navigation (app.dashboard.nav_counts), as of this render."""
    with Session(request.app.state.engine) as session:
        return nav_counts(session)


def without_mode(request: Request) -> str:
    """The request's path and query without `?mode=`: where a mode link lands."""
    query = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != MODE_PARAM])
    return request.url.path + (f"?{query}" if query else "")


def duration(ms: float | None) -> str:
    """Model time for the officer: "850 ms", "4.2 s", "1 min 5 s"."""
    if ms is None:
        return "–"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    minutes, seconds = divmod(round(ms / 1000), 60)
    return f"{minutes} min {seconds} s"


def usd(value: float) -> str:
    """An estimated cost: whole cents when large, four decimals for single calls."""
    if value == 0:
        return "$0"
    return f"${value:.2f}" if value >= 1 else f"${value:.4f}"


templates.env.filters["manila_time"] = manila_time
templates.env.filters["duration"] = duration
templates.env.filters["usd"] = usd
templates.env.globals["live_tokens"] = live_tokens
templates.env.globals["nav_badges"] = nav_badges
templates.env.globals["render_time"] = lambda: datetime.now(MANILA).strftime("%H:%M:%S")
templates.env.globals["NAV"] = NAV
templates.env.globals["nav_current"] = nav_current
templates.env.globals["more_current"] = more_current
templates.env.globals["static_url"] = static_url
templates.env.globals["technical"] = technical
templates.env.globals["PERSONAL_NOTE"] = PERSONAL_NOTE
templates.env.globals["destinations"] = destinations
templates.env.globals["technical_only"] = technical_only
templates.env.globals["GLOSSARY"] = GLOSSARY
templates.env.globals["guide_steps"] = guide_steps
templates.env.globals["drafts_help"] = drafts_help
# The PCIC claims this office drafts (app.pcic_lines), for any page that names them.
templates.env.globals["claims"] = lambda request: enabled_commodities(request.app.state.settings)
templates.env.globals["PLAIN_VERDICTS"] = PLAIN_VERDICTS
templates.env.globals["report_link"] = report_href
templates.env.globals["plain_output_label"] = plain_output_label
templates.env.filters["plain_status"] = plain_status
templates.env.filters["plain_reason"] = plain_reason
templates.env.filters["plain_signal"] = plain_signal
templates.env.filters["plain"] = plain
templates.env.globals["current_officer"] = current_officer.get

templates.env.globals["fact_lines"] = fact_lines
templates.env.globals["TRACE_SINCE"] = TRACE_SINCE
templates.env.globals["GEO_SOURCES"] = GEO_SOURCES
templates.env.globals["BARANGAY_NAMES"] = sorted(b.name for b in BARANGAYS)
templates.env.globals["ACTION_LABELS"] = ACTION_LABELS
templates.env.globals["SIGNAL_VERDICTS"] = SIGNAL_VERDICTS
templates.env.globals["TREE_VERDICTS"] = TREE_VERDICTS
templates.env.globals["PLACE_LISTED_REASON"] = PLACE_LISTED_REASON
templates.env.globals["plan_questions"] = plan
templates.env.globals["REVIEW_LIMITS"] = {
    "officer": OFFICER_MAX,
    "note": NOTE_MAX,
    "place_reason": PLACE_REASON_MIN,
}

# Officer review actions (POST /reports/{id}/review/{action}) and the pages they return to.
REVIEW_ACTIONS = ("merge", "keep", "place", "signal", "tree", "block", "unblock", "restore")
REVIEW_RETURN = re.compile(r"/review|/incidents/\d+")
GUIDE_RETURN = ("/", "/help")  # where hiding the first-run guide may return to
DONE_MESSAGES = {
    "merge": "merged into incident #{incident}",
    "keep": "kept as its own incident #{incident}",
    "place": "barangay set and grouped again (now incident #{incident})",
    "signal": "trust signal reviewed",
    "tree": "fallen tree reviewed",
    "block": "reporter blocked: their new messages are held",
    "unblock": "reporter unblocked",
    "restore": "restored as a report and sent through the pipeline again",
}
templates.env.globals["output_label"] = output_label
templates.env.globals["deadline_table"] = deadline_table


def related_incidents(session: Session, assessments: list[Assessment]) -> dict[int, int | None]:
    """Resolve related evidence independently of the capped /reports page."""
    ids = {
        signal["related_report_id"]
        for assessment in assessments
        for signal in assessment.signals or []
        if signal.get("related_report_id")
    }
    if not ids:
        return {}
    return dict(
        session.exec(select(Report.id, Report.incident_id).where(col(Report.id).in_(ids))).all()
    )


def available_media(engine: Engine, draft: Draft) -> set[int]:
    """Media a frozen Notice of Loss annex links to that retention has not deleted yet."""
    ids = {
        media_id
        for entry in draft.content.get("annex", [])
        for photo in entry["photos"]
        for media_id in (photo["media_id"], photo.get("overlay_media_id"))
        if media_id is not None
    }
    if not ids:
        return set()
    with Session(engine) as session:
        return set(
            session.exec(
                select(Media.id).where(col(Media.id).in_(ids), col(Media.purged_at).is_(None))
            ).all()
        )


def csv_response(text: str, filename: str) -> Response:
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def form_pairs(request: Request, max_bytes: int = FORM_MAX_BYTES) -> list[tuple[str, str]]:
    """A URL-encoded HTML form body as (name, value) pairs in order, repeated names kept (a
    multi-choice posts one pair per ticked box), parsed without extra dependencies.

    An empty body is an empty form (a button-only POST); anything else must be URL-encoded.
    """
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(413, "Form too large.")
    if not body:
        return []
    if request.headers.get("content-type", "").split(";")[0].strip() != FORM_CONTENT_TYPE:
        raise HTTPException(415, f"Send the form as {FORM_CONTENT_TYPE}.")
    try:
        return parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Form is not UTF-8.") from exc


async def form_fields(request: Request) -> dict[str, str]:
    """A small URL-encoded HTML form body as a dict (the last value of a repeated name)."""
    return dict(await form_pairs(request))


def foreign_origin(request: Request) -> str | None:
    """The other site a request came from, or None when it is same-origin or does not say.

    Browsers send Origin on a cross-site form POST (Referer when they do not); either one, when
    present, must name this host. A request without both (curl, tests) is allowed: this is a
    same-origin guard, not authentication.
    """
    host = request.headers.get("host", "").lower()
    for header in ("origin", "referer"):
        if (value := request.headers.get(header)) is not None:
            return None if urlsplit(value).netloc.lower() == host else value
    return None


def report_page(
    session: Session, around: int | None, *, set_aside: bool = False
) -> list[tuple[Report, str]]:
    """Up to REPORT_LIMIT reports with pseudonyms, newest first: the most recent ones, or a
    window centered on report `around`, so an evidence link reaches any report however old.
    Messages the screen set aside as not reports (app.screening) have their own list."""
    newest_first = (col(Report.received_at).desc(), col(Report.id).desc())
    screened = col(Report.status) == STATUS_NOT_A_REPORT
    query = (
        select(Report, Reporter.pseudonym)
        .join(Reporter, col(Reporter.id) == Report.reporter_id)
        .where(screened if set_aside else ~screened)
    )
    if around is None:
        return list(session.exec(query.order_by(*newest_first).limit(REPORT_LIMIT)).all())
    target = session.get(Report, around)
    if target is None:
        raise HTTPException(404, "report not found")
    key = (target.received_at, target.id)
    later = (col(Report.received_at) > key[0]) | (
        (col(Report.received_at) == key[0]) & (col(Report.id) > key[1])
    )
    newer = session.exec(
        query.where(later)
        .order_by(col(Report.received_at), col(Report.id))
        .limit(REPORT_LIMIT // 2)
    ).all()
    older = session.exec(
        query.where(~later).order_by(*newest_first).limit(REPORT_LIMIT - len(newer))
    ).all()
    return [*reversed(newer), *older]


def incident_context(session: Session, incident_id: int) -> dict[str, Any]:
    """Everything the incident page shows, apart from the draft clocks."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    reports = session.exec(
        select(Report)
        .where(Report.incident_id == incident_id)
        .order_by(Report.received_at, Report.id)
    ).all()
    ids = [r.id for r in reports]
    assessments = {
        a.report_id: a
        for a in session.exec(select(Assessment).where(col(Assessment.report_id).in_(ids))).all()
    }
    media: defaultdict[int, list[Media]] = defaultdict(list)
    for item in session.exec(select(Media).where(col(Media.report_id).in_(ids))).all():
        if item.kind in ("photo", "overlay"):
            media[item.report_id].append(item)
    cards = {card.report.id: card for card in review_cards(session, ids)}
    traces = report_traces(session, list(reports))
    items = []
    for report in reports:
        available = [m for m in media[report.id] if m.purged_at is None]
        items.append(
            {
                "report": report,
                "assessment": assessments.get(report.id),
                "media": [m for m in available if m.kind == "overlay"] or available,
                "had_photos": bool(media[report.id]),
                "card": cards.get(report.id),
                "trace": traces[report.id],
            }
        )
    return {
        "incident": incident,
        "items": items,
        "review": [cards[i] for i in ids if i in cards and cards[i].needs_review],
        "history": timeline(session, incident_id),
        "trust": incident_trust(incident),
        "related_incidents": related_incidents(session, list(assessments.values())),
    }


def create_app(
    settings: Settings | None = None,
    *,
    summary_llm: AgentLLM | None = None,
    pair_judge: AgentLLM | None = None,
    clock: Callable[[], datetime] | None = None,
    telegram_application: Application | None = None,
    snapshots: SnapshotStore | None = None,
) -> FastAPI:
    """`summary_llm` replaces the configured agent LLM for RDANA summaries and `pair_judge` the
    duplicate judge the officer's "set place" re-grouping asks (tests, offline); `clock` (naive
    UTC) replaces the real clock for filing countdowns (tests only). With TELEGRAM_MODE=webhook
    the app also runs the bot (app.telegram_webhook); `telegram_application` replaces the one
    built from the settings (tests). With SNAPSHOT_DIR (or `snapshots`, which a deploy passes
    with its volume) the database is restored at start and snapshotted (app.snapshots)."""
    settings = settings or get_settings()
    auth = OfficerAuth(settings)  # refuses to build a public app without a passcode
    if snapshots is None and settings.snapshot_dir is not None:
        snapshots = SnapshotStore.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if snapshots is not None:  # before the engine: it may restore the database file
            await asyncio.to_thread(snapshots.open)
        app.state.engine = make_engine(settings)
        init_db(app.state.engine)
        # Building a client makes no request; a missing key surfaces as a recorded fallback.
        recorder = make_recorder(app.state.engine)
        llm = summary_llm or make_agent_llm(settings, recorder)
        judge = pair_judge or make_pair_judge(settings, recorder)
        app.state.drafts = DraftService(settings, app.state.engine, llm, clock or utcnow)
        app.state.review = ReviewService(
            app.state.engine, IncidentService(settings, app.state.engine, judge)
        )
        app.state.live = ChangeHub(settings.db_path)
        await app.state.live.start()
        restore_signals = close_on_shutdown_signals(app.state.live)
        app.state.telegram = None
        if settings.telegram_mode == "webhook":
            application = telegram_application or build_application(
                settings, engine=app.state.engine
            )
            if snapshots is not None:  # an acknowledged report is in a snapshot within seconds
                application.bot_data["after_update"] = snapshots.request
            await start_upkeep(application)  # the retention purge, whether or not Telegram starts
            # A restored message is assessed at once by the bot's own assessor (app.review).
            app.state.review.assessor = application.bot_data.get("assessor")
            secret = settings.telegram_webhook_secret.get_secret_value()
            app.state.telegram = TelegramWebhook(application, secret)
            await app.state.telegram.start()
        snapshot_loop = asyncio.create_task(snapshots.run()) if snapshots is not None else None
        yield
        restore_signals()
        if app.state.telegram is not None:
            # First, while the database is open, and bounded (app.bot): it handles the queued
            # updates, snapshots them at once, then gives assessments a short grace.
            checkpoint = (
                (lambda: asyncio.to_thread(snapshots.backup)) if snapshots is not None else None
            )
            await app.state.telegram.stop(checkpoint)
            await stop_upkeep(app.state.telegram.application)
        if snapshot_loop is not None:
            snapshot_loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshot_loop
        await app.state.live.stop()
        if summary_llm is None:
            await llm.aclose()
        if pair_judge is None:
            await judge.aclose()
        app.state.engine.dispose()
        if snapshots is not None:  # last: every write above is in the final snapshot
            await asyncio.to_thread(snapshots.close)

    app = FastAPI(title="Typhoon damage reports", lifespan=lifespan)
    app.state.auth = auth
    app.state.settings = settings  # what this deployment drafts (app.pcic_lines) reads it
    app.state.results_path = RESULTS_PATH  # the recorded accuracy results; tests point elsewhere
    # Middleware runs last-added first: host check, then same-origin, then the mode, then the
    # login gate.
    install_login_gate(app, auth)

    @app.middleware("http")
    async def presentation_mode(request: Request, call_next):
        """Simple or Technical mode for this request (app.modes). A `?mode=` link stores the
        choice for this browser and lands on the same page without the parameter."""
        asked = request.query_params.get(MODE_PARAM)
        if request.method == "GET" and asked in MODES:
            response = RedirectResponse(without_mode(request), status_code=303)
            response.set_cookie(
                MODE_COOKIE,
                asked,
                max_age=MODE_MAX_AGE_S,
                path="/",
                httponly=True,
                secure=auth.https(request),
                samesite="lax",
            )
            return response
        mode = resolve(None, request.cookies.get(MODE_COOKIE), settings.dashboard_mode)
        token = current_mode.set(mode)
        try:
            return await call_next(request)
        finally:
            current_mode.reset(token)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if request.method in STATE_CHANGING_METHODS and (origin := foreign_origin(request)):
            message = f"Refused: this request came from another site ({origin[:100]})."
            return templates.TemplateResponse(
                request, "error.html", {"message": message}, status_code=403
            )
        return await call_next(request)

    # Pages, styles and scripts travel compressed (a slow phone connection loads a page several
    # times faster); Starlette never compresses the event stream, photos or fonts.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    # Added last, so it runs first: a request for another host name never reaches a route.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(DraftError)
    async def draft_error(request: Request, exc: DraftError) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=exc.status_code
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def login_page(
        request: Request,
        next_path: str | None,
        *,
        error: str | None = None,
        officer: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        context = {
            "next": safe_next(next_path),
            "error": error,
            "officer": officer,
            "officer_max": OFFICER_MAX,
        }
        headers = {"Cache-Control": "no-store"}
        if status_code == 429:
            headers["Retry-After"] = str(auth.limiter.retry_after(auth.client_address(request)))
        return templates.TemplateResponse(
            request, "login.html", context, status_code=status_code, headers=headers
        )

    @app.get(LOGIN_PATH, response_model=None)
    def login_form(request: Request) -> HTMLResponse | RedirectResponse:
        if not auth.enabled or auth.session(request) is not None:
            return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
        return login_page(request, request.query_params.get("next"))

    @app.post(LOGIN_PATH, response_model=None)
    async def login(request: Request) -> HTMLResponse | RedirectResponse:
        if not auth.enabled:
            return RedirectResponse("/", status_code=303)
        form = await form_fields(request)
        client = auth.client_address(request)
        if wait := auth.limiter.retry_after(client):
            minutes = -(-wait // 60)
            return login_page(
                request,
                form.get("next"),
                error=f"Too many wrong passcodes. Try again in {minutes} minute"
                + ("s." if minutes != 1 else "."),
                status_code=429,
            )
        officer = " ".join(form.get("officer", "").split())[:OFFICER_MAX]
        if not auth.check_passcode(form.get("passcode", "")):
            auth.failed_login(client)
            return login_page(
                request, form.get("next"), error="Wrong passcode.", officer=officer, status_code=401
            )
        auth.limiter.succeeded(client)
        response = RedirectResponse(safe_next(form.get("next")), status_code=303)
        auth.sign_in(response, request, officer)
        return response

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        response = RedirectResponse(LOGIN_PATH if auth.enabled else "/", status_code=303)
        auth.sign_out(response, request)
        return response

    @app.post(WEBHOOK_PREFIX + "{path_token}")
    async def telegram_webhook(request: Request, path_token: str) -> Response:
        """Telegram's update delivery in webhook mode (public: it checks Telegram's secret)."""
        hook: TelegramWebhook | None = request.app.state.telegram
        if hook is None:
            raise HTTPException(404, "not found")
        return await hook.handle(request, path_token)

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-sent events naming which data changed (see app.live); read-only."""
        hub: ChangeHub = request.app.state.live
        if not hub.accepting():
            raise HTTPException(503, "Live updates are not available right now.")
        return StreamingResponse(
            event_stream(hub),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/events/token")
    def events_token(request: Request) -> JSONResponse:
        """The same snapshot as the stream's hello event, for clients that fall back to polling."""
        return JSONResponse(
            request.app.state.live.snapshot(), headers={"Cache-Control": "no-store"}
        )

    @app.get("/", response_class=HTMLResponse)
    def overview_page(request: Request) -> HTMLResponse:
        drafts: DraftService = request.app.state.drafts
        with Session(request.app.state.engine) as session:
            context = overview(session, drafts, drafts.clock())
        context["spend_cap"] = settings.daily_spend_cap_usd
        context["guide"] = request.cookies.get(GUIDE_COOKIE) != "hidden"
        context["now"] = drafts.clock()
        return templates.TemplateResponse(request, "overview.html", context)

    @app.get("/accuracy", response_class=HTMLResponse)
    def accuracy(request: Request) -> HTMLResponse:
        """The agent's measured accuracy, from the recorded eval results (app.accuracy)."""
        context = {"page": accuracy_page(request.app.state.results_path), "weak_below": WEAK_BELOW}
        return templates.TemplateResponse(request, "accuracy.html", context)

    @app.get("/map", response_class=HTMLResponse)
    def map_page(request: Request) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            collection, unplaced = map_features(session)
        context = {"features": collection["features"], "unplaced": unplaced}
        return templates.TemplateResponse(request, "map.html", context)

    @app.get("/map/incidents.geojson")
    def map_incidents(request: Request) -> JSONResponse:
        with Session(request.app.state.engine) as session:
            collection, _unplaced = map_features(session)
        return JSONResponse(
            collection, media_type="application/geo+json", headers={"Cache-Control": "no-store"}
        )

    @app.get("/map/barangays.geojson")
    def map_barangays(request: Request) -> Response:
        """The barangay boundaries, cached by the browser for a day and revalidated by ETag."""
        body = boundaries_geojson()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        headers = {"Cache-Control": "public, max-age=86400", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/geo+json", headers=headers)

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request) -> HTMLResponse:
        """How Ulat works and what its official terms mean; the first-run guide's steps."""
        hidden = request.cookies.get(GUIDE_COOKIE) == "hidden"
        context = {
            "guide_hidden": hidden,
            "terms": glossary(enabled_commodities(request.app.state.settings)),
        }
        return templates.TemplateResponse(request, "help.html", context)

    @app.post("/help/guide")
    async def guide(request: Request) -> RedirectResponse:
        """Hide the first-run guide on Home for this browser, or show it again (Help)."""
        form = await form_fields(request)
        show = form.get("guide") == "show"
        back = form.get("next") if form.get("next") in GUIDE_RETURN else "/"
        response = RedirectResponse("/" if show else back, status_code=303)
        if show:
            response.delete_cookie(GUIDE_COOKIE, path="/")
        else:
            response.set_cookie(
                GUIDE_COOKIE,
                "hidden",
                max_age=MODE_MAX_AGE_S,
                path="/",
                httponly=True,
                secure=auth.https(request),
                samesite="lax",
            )
        return response

    @app.get("/reports/set-aside", response_class=HTMLResponse)
    def set_aside(request: Request) -> HTMLResponse:
        """Messages the screen kept out as not reports, each with its reason and Restore."""
        with Session(request.app.state.engine) as session:
            rows = report_page(session, None, set_aside=True)
        return templates.TemplateResponse(
            request, "set_aside.html", {"items": [report for report, _ in rows]}
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, around: int | None = None) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            rows = report_page(session, around)
            set_aside_count = session.exec(
                select(func.count())
                .select_from(Report)
                .where(col(Report.status) == STATUS_NOT_A_REPORT)
            ).one()
            ids = [report.id for report, _ in rows]
            media = session.exec(select(Media).where(col(Media.report_id).in_(ids))).all()
            assessments = session.exec(
                select(Assessment).where(col(Assessment.report_id).in_(ids))
            ).all()
            photo_rows = session.exec(
                select(PhotoAssessment).where(col(PhotoAssessment.report_id).in_(ids))
            ).all()
            spend = model_spend(session)
            related = related_incidents(session, assessments)
            traces = report_traces(session, [report for report, _ in rows])
        media_by_id = {m.id: m for m in media}
        media_by_report: defaultdict[int, list[Media]] = defaultdict(list)
        for m in media:
            media_by_report[m.report_id].append(m)
        photos_by_report: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in photo_rows:
            photos_by_report[row.report_id].append(
                {
                    "assessment": row,
                    "photo": media_by_id.get(row.media_id),
                    "overlay": media_by_id.get(row.overlay_media_id),
                }
            )
        assessed_media = {row.media_id for row in photo_rows}
        for m in media:
            if m.kind == "photo" and m.id not in assessed_media:
                photos_by_report[m.report_id].append(
                    {"assessment": None, "photo": m, "overlay": None}
                )
        by_report = {a.report_id: a for a in assessments}
        items = [
            {
                "report": report,
                "reporter": pseudonym[:8],
                "media": media_by_report[report.id],
                "assessment": by_report.get(report.id),
                "photos": photos_by_report[report.id],
                "trace": traces[report.id],
            }
            for report, pseudonym in rows
        ]
        context = {
            "items": items,
            "agent_llm": settings.agent_llm,
            "ttl_hours": settings.raw_media_ttl_hours,
            "model_spend": spend,
            "spend_cap": settings.daily_spend_cap_usd,
            "related_incidents": related,
            "around": around,
            "set_aside": set_aside_count,
        }
        return templates.TemplateResponse(request, "reports.html", context)

    @app.get("/incidents", response_class=HTMLResponse)
    def incidents(request: Request) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            items = active_incidents(session)
            trust = {incident.id: incident_trust(incident) for incident in items}
        return templates.TemplateResponse(
            request, "incidents.html", {"items": items, "trust": trust}
        )

    def incident_page(
        request: Request, incident_id: int, *, form: dict | None = None, status_code: int = 200
    ) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            context = incident_context(session, incident_id)
        context["nol_statuses"] = request.app.state.drafts.nol_statuses(incident_id=incident_id)
        context["review_form"] = form
        return templates.TemplateResponse(
            request, "incident.html", context, status_code=status_code
        )

    def review_page(
        request: Request,
        *,
        form: dict | None = None,
        initials_error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        params = request.query_params
        with Session(request.app.state.engine) as session:
            cards = queue(session)
            related = related_incidents(
                session, [c.assessment for c in cards if c.assessment is not None]
            )
            # Simple mode asks one question at a time (app.questions); Technical lists the cards.
            asked = [] if technical() else questions(session, cards)
            shown = (
                {q.card.report.id for q in asked}
                | {q.compare.id for q in asked if q.compare is not None}
                | {card.report.id for card in cards}
            )
            photos = question_photos(session, shown)
        at = params.get("at")
        if form and asked:  # a refused answer: show its question, with the error
            at = next((q.key for q in asked if q.card.report.id == form["report_id"]), at)
        action = params.get("done", "")
        done = DONE_MESSAGES.get(action)
        if done and not technical():  # Simple mode names no report or incident numbers
            done = PLAIN_DONE.get(action)
        now = request.app.state.drafts.clock()
        context = {
            "cards": cards,
            "questions": asked,
            "current": current(asked, at),
            "photos": photos,
            "ago": lambda when: ago(when, now),
            "ask_initials": not current_officer.get() or params.get("initials") == "change",
            "initials_error": initials_error,
            "related_incidents": related,
            "review_form": form,
            # After a redirect: what the last action did (ids are digits or the line is dropped).
            "done": (
                (
                    f"Report #{params['report']}: "
                    + done.format(incident=params.get("incident", "?"))
                    if technical()
                    else done
                )
                if done
                and params.get("report", "").isdigit()
                and params.get("incident", "0").isdigit()
                else None
            ),
        }
        return templates.TemplateResponse(request, "review.html", context, status_code=status_code)

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_detail(request: Request, incident_id: int) -> HTMLResponse:
        return incident_page(request, incident_id)

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request) -> HTMLResponse:
        return review_page(request)

    @app.post("/initials", response_model=None)
    async def initials(request: Request) -> RedirectResponse | HTMLResponse:
        """Review asks once for the officer's initials when the sign-in had none, and keeps
        them for the session: every answer after that records them (app.review's audit)."""
        form = await form_fields(request)
        try:
            officer = officer_name(form.get("officer"))
        except ReviewError as exc:
            return review_page(request, initials_error=str(exc), status_code=exc.status_code)
        response = RedirectResponse("/review", status_code=303)
        auth.set_initials(response, request, officer)
        return response

    @app.post("/reports/{report_id:int}/review/{action}", response_model=None)
    async def review_action(
        request: Request, report_id: int, action: str
    ) -> RedirectResponse | HTMLResponse:
        """One officer override (see app.review); a rejected form comes back with its values."""
        if action not in REVIEW_ACTIONS:
            raise HTTPException(404, "unknown review action")
        form = await form_fields(request)
        back = form.get("next", "/review")
        if not REVIEW_RETURN.fullmatch(back):
            back = "/review"
        service: ReviewService = request.app.state.review
        officer, note = form.get("officer"), form.get("note")
        try:
            if action == "merge":
                try:
                    target = int(form.get("incident_id", ""))
                except ValueError as exc:
                    raise ReviewError("Choose the incident to merge into.") from exc
                incident = await run_in_threadpool(
                    service.merge, report_id, target, officer=officer, note=note
                )
            elif action == "keep":
                incident = await run_in_threadpool(
                    service.keep_separate, report_id, officer=officer, note=note
                )
            elif action == "place":
                incident = await service.set_place(
                    report_id, form.get("barangay"), officer=officer, reason=note
                )
            elif action == "signal":
                incident = await run_in_threadpool(
                    service.review_signal,
                    report_id,
                    form.get("signal"),
                    form.get("verdict"),
                    officer=officer,
                    note=note,
                )
            elif action == "restore":
                incident = await service.restore(report_id, officer=officer, note=note)
            elif action == "tree":
                incident = await run_in_threadpool(
                    service.review_fallen_tree,
                    report_id,
                    form.get("verdict"),
                    officer=officer,
                    note=note,
                )
            else:
                incident = await run_in_threadpool(
                    service.set_reporter_blocked,
                    report_id,
                    action == "block",
                    officer=officer,
                    note=note,
                )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewError as exc:
            kept = {"report_id": report_id, "action": action, "values": form, "error": str(exc)}
            if back.startswith("/incidents/"):
                return incident_page(
                    request, int(back.rsplit("/", 1)[1]), form=kept, status_code=exc.status_code
                )
            return review_page(request, form=kept, status_code=exc.status_code)
        if back.startswith("/incidents/") and incident is not None:
            # Follow the report: a merge moves it to another incident's page.
            return RedirectResponse(f"/incidents/{incident}#report-{report_id}", status_code=303)
        query = f"?done={action}&report={report_id}" + (
            f"&incident={incident}" if incident is not None else ""
        )
        return RedirectResponse(f"/review{query}", status_code=303)

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_page(request: Request) -> HTMLResponse:
        city, statuses, history = request.app.state.drafts.overview()
        context = {
            "nol_statuses": request.app.state.drafts.nol_statuses(city, history),
            "commodities": enabled_commodities(request.app.state.settings),
            "statuses": statuses,
            "history": history,
            "unplaced": city.unplaced,
            "has_house_counts": bool(damaged_houses(city)["rows"]),
            "rdana_deadline": deadline(RDANA_DEADLINE),
        }
        return templates.TemplateResponse(request, "drafts.html", context)

    @app.post("/drafts")
    async def generate_draft(request: Request) -> RedirectResponse:
        form = await form_fields(request)
        barangay = form.get("barangay", "")
        if barangay not in BY_NAME:
            raise DraftError("Choose one of Biñan's barangays.")
        event = EventInput(
            name=clean_text(form.get("event_name"), label="Event name", max_length=EVENT_NAME_MAX),
            type=clean_text(form.get("event_type"), label="Event type", max_length=EVENT_TYPE_MAX),
            at=parse_event_at(form.get("event_at")),
        )
        draft = await request.app.state.drafts.generate_rdana(barangay, event)
        return RedirectResponse(f"/drafts/{draft.id}", status_code=303)

    @app.post("/drafts/notice-of-loss")
    async def generate_nol_draft(request: Request) -> RedirectResponse:
        form = await form_fields(request)
        try:
            incident_id = int(form.get("incident_id", ""))
        except ValueError as exc:
            raise DraftError("Choose an incident.") from exc
        draft = await run_in_threadpool(
            request.app.state.drafts.generate_nol, form.get("commodity", ""), incident_id
        )
        return RedirectResponse(f"/drafts/{draft.id}", status_code=303)

    @app.post("/drafts/damaged-houses")
    def generate_houses_draft(request: Request) -> RedirectResponse:
        draft = request.app.state.drafts.generate_damaged_houses()
        return RedirectResponse(f"/drafts/{draft.id}", status_code=303)

    def draft_page_response(
        request: Request,
        draft_id: int,
        *,
        refused: FillRefused | None = None,
        officer: str | None = None,
    ) -> HTMLResponse:
        drafts: DraftService = request.app.state.drafts
        draft, events, newest = drafts.get(draft_id)
        with Session(request.app.state.engine) as session:
            model_calls = draft_calls(session, draft_id)
        saved = request.query_params.get("saved")
        context = {
            "draft": draft,
            "model_calls": model_calls,
            "stale": drafts.is_stale(draft),
            "predates_pending": predates_pending_bucket(draft),
            "regenerate": regenerate_form(draft),
            "nol": commodity_for_kind(draft.kind) is not None,
            "commodity": commodity_for_kind(draft.kind),
            "photo_evidence": PHOTO_EVIDENCE,
            "photo_page": PHOTO_PAGE,
            "stamp": print_stamp(draft, events),
            "clocks": drafts.clocks(draft),
            "available_media": available_media(request.app.state.engine, draft),
            "c": draft.content,
            "form": form_view(
                draft,
                drafts.clock(),
                refused=refused.result if refused else None,
                present=presenter(),
            ),
            "refused": refused,
            "fill_officer": officer if officer is not None else current_officer.get(),
            "saved": saved if saved in ("0", "1") else None,
            "today": manila_time(drafts.clock())[:10],
            "events": events,
            "newest": newest,
            "pending": pending_report_ids(draft),
            "reason_min": REASON_MIN,
            "reason_max": REASON_MAX,
            "decided_by_max": DECIDED_BY_MAX,
        }
        return templates.TemplateResponse(
            request, "draft.html", context, status_code=refused.status_code if refused else 200
        )

    @app.get("/drafts/{draft_id:int}", response_class=HTMLResponse)
    def draft_page(request: Request, draft_id: int) -> HTMLResponse:
        return draft_page_response(request, draft_id)

    @app.post("/drafts/{draft_id:int}/fill", response_model=None)
    async def fill_draft(request: Request, draft_id: int) -> HTMLResponse | RedirectResponse:
        """Save the officer's entries as the next version. A refused value shows the page again
        with everything that was typed and the message under each field; nothing is saved."""
        pairs = await form_pairs(request, FILL_MAX_BYTES)
        officer = next((v for k, v in reversed(pairs) if k == "officer"), "")
        try:
            saved = await run_in_threadpool(
                partial(
                    request.app.state.drafts.save_entries,
                    draft_id,
                    pairs,
                    officer=officer,
                    present=presenter(),
                )
            )
        except FillRefused as refused:
            return draft_page_response(request, draft_id, refused=refused, officer=officer)
        if saved is None:
            return RedirectResponse(f"/drafts/{draft_id}?saved=0", status_code=303)
        return RedirectResponse(f"/drafts/{saved.id}?saved=1", status_code=303)

    @app.get("/drafts/{draft_id:int}.csv")
    def draft_csv(request: Request, draft_id: int) -> Response:
        draft, _events, _newest = request.app.state.drafts.get(draft_id)
        if draft.kind != KIND_DAMAGED_HOUSES:
            raise HTTPException(404, "Only damaged-houses drafts have a CSV.")
        name = f"binan-damaged-houses-v{draft.version}-{draft.status}.csv"
        return csv_response(damaged_houses_csv(draft.content, csv_status(draft)), name)

    @app.post("/drafts/{draft_id:int}/{action}")
    async def decide_draft(request: Request, draft_id: int, action: str) -> RedirectResponse:
        form = await form_fields(request)
        request.app.state.drafts.decide(
            draft_id,
            action,
            reason=form.get("reason"),
            decided_by=form.get("decided_by"),
            confirm_stale=form.get("confirm_stale") == "1",
            version=int(form["version"]) if form.get("version", "").isdigit() else None,
        )
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    @app.get("/damaged-houses", response_class=HTMLResponse)
    def damaged_houses_page(request: Request) -> HTMLResponse:
        table = damaged_houses(request.app.state.drafts.evidence())
        return templates.TemplateResponse(request, "damaged_houses.html", {"t": table})

    @app.get("/damaged-houses.csv")
    def damaged_houses_download(request: Request) -> Response:
        table = damaged_houses(request.app.state.drafts.evidence())
        name = f"binan-damaged-houses-live-not-approved-{utcnow():%Y%m%d-%H%M}-utc.csv"
        return csv_response(damaged_houses_csv(table, LIVE_CSV_STATUS), name)

    @app.get("/media/{media_id}")
    def media_file(request: Request, media_id: int) -> FileResponse:
        """Serve a stored photo, overlay or voice note by id, until retention deletes it."""
        with Session(request.app.state.engine) as session:
            media = session.get(Media, media_id)
        if media is None or media.purged_at is not None:
            raise HTTPException(404, "not found or deleted after the retention period")
        root = settings.media_dir.resolve()
        path = (settings.data_dir / media.path).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, "not found")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private"})

    return app


app = create_app()

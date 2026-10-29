"""FastAPI app: health check, reports, incidents, officer drafts (RDANA, damaged houses, PCIC
Notices of Loss) and evidence media.

Run with: uv run uvicorn app.main:app --reload

There is no login yet, so the app must only listen on localhost (uvicorn's default). Add
authentication before exposing it anywhere: the pages show reports and photos. Until then, it
answers only to ALLOWED_HOSTS (against DNS rebinding) and every state-changing request must come
from the app's own pages (a same-origin check), so another site open in the officer's browser
cannot generate or approve drafts.
"""

import hashlib
import json
import mimetypes
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.dashboard import active_incidents, incident_trust, overview
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
    clean_text,
    csv_status,
    damaged_houses,
    damaged_houses_csv,
    output_label,
    parse_event_at,
    pending_report_ids,
    predates_pending_bucket,
    regenerate_form,
)
from app.gazetteer import BY_NAME
from app.live import ChangeHub, close_on_shutdown_signals, event_stream
from app.meta.factory import make_agent_llm
from app.meta.llm import AgentLLM
from app.models import (
    MANILA,
    AgentDecision,
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
from app.nol import deadline_table
from app.nol_form import COMMODITIES, commodity_for_kind
from app.rdana import RDANA_DEADLINE, EventInput
from app.trust import model_spend

REPORT_LIMIT = 200
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
FORM_MAX_BYTES = 16_384

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"

# The header navigation, in order: (path, label). A page is current for its own path and the
# paths below it (/incidents/7 is under Incidents).
NAV = (
    ("/", "Overview"),
    ("/incidents", "Incidents"),
    ("/reports", "Reports"),
    ("/drafts", "Drafts"),
    ("/damaged-houses", "Damaged houses"),
)


def nav_current(path: str, href: str) -> bool:
    return path == href or (href != "/" and path.startswith(href + "/"))


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


templates.env.filters["manila_time"] = manila_time
templates.env.globals["live_tokens"] = live_tokens
templates.env.globals["render_time"] = lambda: datetime.now(MANILA).strftime("%H:%M:%S")
templates.env.globals["NAV"] = NAV
templates.env.globals["nav_current"] = nav_current
templates.env.globals["static_url"] = static_url

GEO_SOURCES = {
    "pin": "location pin",
    "exif": "photo GPS",
    "text": "named in text",
    "transcript": "named in voice note",
    "llm": "named (read by the agent)",
    "ambiguous": "several barangays named",
    "none": "not found",
}
DEPTHS = {
    "ankle": "ankle-deep",
    "knee": "knee-deep",
    "waist": "waist-deep",
    "chest": "chest-deep",
    "above_head": "above head",
}


def fact_lines(facts: dict[str, Any] | None) -> list[str]:
    """The stated facts as short labels for the officer; unstated (null) fields are skipped."""
    if not facts:
        return []
    lines: list[str] = []
    if facts.get("is_correction"):
        lines.append("CORRECTION of an earlier report")
    if facts.get("flood_depth") in DEPTHS:
        lines.append(f"flood {DEPTHS[facts['flood_depth']]}")
    counts = [
        ("houses_totally_damaged", "houses totally damaged"),
        ("houses_partially_damaged", "houses partially damaged"),
        ("houses_flooded", "houses flooded"),
        ("families_affected", "families affected"),
        ("families_evacuated", "families evacuated"),
        ("persons_evacuated", "persons evacuated"),
        ("dead", "dead"),
        ("injured", "injured"),
        ("missing", "missing"),
        ("fish_cages_or_pens_damaged", "fish cages/pens damaged"),
        ("livestock_lost", "animals lost"),
    ]
    lines += [f"{facts[key]} {label}" for key, label in counts if facts.get(key) is not None]
    if facts.get("crop") not in (None, "none"):
        crop = facts["crop"]
        if facts.get("crop_area_hectares") is not None:
            crop += f", {facts['crop_area_hectares']:g} ha"
        if facts.get("crop_damage_percent") is not None:
            crop += f", {facts['crop_damage_percent']:g}% damaged"
        lines.append(crop)
    if facts.get("fishery_area_hectares") is not None:
        lines.append(f"{facts['fishery_area_hectares']:g} ha of fish pens, cages or ponds")
    for key, label in (
        ("hazards", "hazards"),
        ("livelihood_damage", "livelihood damage"),
        ("infrastructure", "damaged"),
        ("needs", "needs"),
    ):
        if facts.get(key):
            lines.append(f"{label}: {', '.join(v.replace('_', ' ') for v in facts[key])}")
    if facts.get("place_detail"):
        lines.append(f"place: {facts['place_detail']}")
    return lines


templates.env.globals["fact_lines"] = fact_lines
templates.env.globals["GEO_SOURCES"] = GEO_SOURCES
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


async def form_fields(request: Request) -> dict[str, str]:
    """A small URL-encoded HTML form body, parsed without extra dependencies.

    An empty body is an empty form (a button-only POST); anything else must be URL-encoded.
    """
    body = await request.body()
    if len(body) > FORM_MAX_BYTES:
        raise HTTPException(413, "Form too large.")
    if not body:
        return {}
    if request.headers.get("content-type", "").split(";")[0].strip() != FORM_CONTENT_TYPE:
        raise HTTPException(415, f"Send the form as {FORM_CONTENT_TYPE}.")
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Form is not UTF-8.") from exc
    return dict(pairs)


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


def report_page(session: Session, around: int | None) -> list[tuple[Report, str]]:
    """Up to REPORT_LIMIT reports with pseudonyms, newest first: the most recent ones, or a
    window centred on report `around`, so an evidence link reaches any report however old."""
    newest_first = (col(Report.received_at).desc(), col(Report.id).desc())
    query = select(Report, Reporter.pseudonym).join(
        Reporter, col(Reporter.id) == Report.reporter_id
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


def create_app(
    settings: Settings | None = None,
    *,
    summary_llm: AgentLLM | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """`summary_llm` replaces the configured agent LLM for RDANA summaries (tests, offline);
    `clock` (naive UTC) replaces the real clock for filing countdowns (tests only)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = make_engine(settings)
        init_db(app.state.engine)
        # Building a client makes no request; a missing key surfaces as a recorded fallback.
        llm = summary_llm or make_agent_llm(settings, make_recorder(app.state.engine))
        app.state.drafts = DraftService(settings, app.state.engine, llm, clock or utcnow)
        app.state.live = ChangeHub(settings.db_path)
        await app.state.live.start()
        restore_signals = close_on_shutdown_signals(app.state.live)
        yield
        restore_signals()
        await app.state.live.stop()
        if summary_llm is None:
            await llm.aclose()
        app.state.engine.dispose()

    app = FastAPI(title="Typhoon damage reports", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if request.method in STATE_CHANGING_METHODS and (origin := foreign_origin(request)):
            message = f"Refused: this request came from another site ({origin[:100]})."
            return templates.TemplateResponse(
                request, "error.html", {"message": message}, status_code=403
            )
        return await call_next(request)

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
        return templates.TemplateResponse(request, "overview.html", context)

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, around: int | None = None) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            rows = report_page(session, around)
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

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_detail(request: Request, incident_id: int) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
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
                for a in session.exec(
                    select(Assessment).where(col(Assessment.report_id).in_(ids))
                ).all()
            }
            media: defaultdict[int, list[Media]] = defaultdict(list)
            for item in session.exec(select(Media).where(col(Media.report_id).in_(ids))).all():
                if item.kind in ("photo", "overlay"):
                    media[item.report_id].append(item)
            decisions = session.exec(
                select(AgentDecision)
                .where(
                    (AgentDecision.incident_id == incident_id)
                    | (AgentDecision.compared_incident_id == incident_id)
                )
                .order_by(AgentDecision.created_at, AgentDecision.id)
            ).all()
            trust = incident_trust(incident)
            related = related_incidents(session, list(assessments.values()))
        items = []
        for report in reports:
            available = [m for m in media[report.id] if m.purged_at is None]
            items.append(
                {
                    "report": report,
                    "assessment": assessments.get(report.id),
                    "media": [m for m in available if m.kind == "overlay"] or available,
                    "had_photos": bool(media[report.id]),
                }
            )
        return templates.TemplateResponse(
            request,
            "incident.html",
            {
                "incident": incident,
                "items": items,
                "decisions": decisions,
                "trust": trust,
                "related_incidents": related,
                "nol_statuses": request.app.state.drafts.nol_statuses(incident_id=incident_id),
            },
        )

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_page(request: Request) -> HTMLResponse:
        city, statuses, history = request.app.state.drafts.overview()
        context = {
            "nol_statuses": request.app.state.drafts.nol_statuses(city, history),
            "commodities": list(COMMODITIES.values()),
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

    @app.get("/drafts/{draft_id:int}", response_class=HTMLResponse)
    def draft_page(request: Request, draft_id: int) -> HTMLResponse:
        draft, events, newest = request.app.state.drafts.get(draft_id)
        context = {
            "draft": draft,
            "stale": request.app.state.drafts.is_stale(draft),
            "predates_pending": predates_pending_bucket(draft),
            "regenerate": regenerate_form(draft),
            "nol": commodity_for_kind(draft.kind) is not None,
            "clocks": request.app.state.drafts.clocks(draft),
            "available_media": available_media(request.app.state.engine, draft),
            "c": draft.content,
            "events": events,
            "newest": newest,
            "pending": pending_report_ids(draft),
            "reason_min": REASON_MIN,
            "reason_max": REASON_MAX,
            "decided_by_max": DECIDED_BY_MAX,
        }
        return templates.TemplateResponse(request, "draft.html", context)

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

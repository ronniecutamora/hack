"""FastAPI app: health check, the officer's report list with assessments, and evidence media.

Run with: uv run uvicorn app.main:app --reload

There is no login yet, so the app must only listen on localhost (uvicorn's default). Add
authentication before exposing it anywhere: the pages show reports and photos.
"""

import mimetypes
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

from app.config import Settings, get_settings
from app.db import init_db, make_engine
from app.models import (
    AgentDecision,
    Assessment,
    Incident,
    Media,
    PhotoAssessment,
    Report,
    Reporter,
    manila_time,
)
from app.trust import model_spend

REPORT_LIMIT = 200

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


templates.env.filters["manila_time"] = manila_time

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
    for key, label in (("hazards", "hazards"), ("infrastructure", "damaged"), ("needs", "needs")):
        if facts.get(key):
            lines.append(f"{label}: {', '.join(v.replace('_', ' ') for v in facts[key])}")
    if facts.get("place_detail"):
        lines.append(f"place: {facts['place_detail']}")
    return lines


templates.env.globals["fact_lines"] = fact_lines
templates.env.globals["GEO_SOURCES"] = GEO_SOURCES


def incident_trust(incident: Incident) -> dict[str, int | None]:
    """The rollup owns trust counts; legacy incidents show unknown until refreshed."""
    return {
        "reporter_count": incident.latest_facts.get("reporter_count"),
        "flagged_report_count": incident.latest_facts.get("flagged_report_count", 0),
    }


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = make_engine(settings)
        init_db(app.state.engine)
        yield
        app.state.engine.dispose()

    app = FastAPI(title="Typhoon damage reports", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            rows = session.exec(
                select(Report, Reporter.pseudonym)
                .join(Reporter, col(Reporter.id) == Report.reporter_id)
                .order_by(col(Report.received_at).desc())
                .limit(REPORT_LIMIT)
            ).all()
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
        }
        return templates.TemplateResponse(request, "reports.html", context)

    @app.get("/incidents", response_class=HTMLResponse)
    def incidents(request: Request) -> HTMLResponse:
        with Session(request.app.state.engine) as session:
            items = session.exec(
                select(Incident)
                .where(Incident.active == True)  # noqa: E712
                .order_by(col(Incident.last_seen).desc(), col(Incident.id).desc())
            ).all()
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
            },
        )

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

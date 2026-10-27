"""FastAPI app: health check and a minimal server-rendered list of reports (dashboard seed).

Run with: uv run uvicorn app.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, func, select

from app.config import Settings, get_settings
from app.db import init_db, make_engine
from app.models import Media, Report, Reporter

MANILA = timezone(timedelta(hours=8))  # Philippine time, no daylight saving
REPORT_LIMIT = 200

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def manila_time(value: datetime) -> str:
    """Format a naive-UTC datetime as Philippine local time."""
    return value.replace(tzinfo=UTC).astimezone(MANILA).strftime("%Y-%m-%d %H:%M")


templates.env.filters["manila_time"] = manila_time


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
                select(Report, Reporter.pseudonym, func.count(col(Media.id)))
                .join(Reporter, col(Reporter.id) == Report.reporter_id)
                .outerjoin(Media, col(Media.report_id) == col(Report.id))
                .group_by(col(Report.id))
                .order_by(col(Report.received_at).desc())
                .limit(REPORT_LIMIT)
            ).all()
        items = [
            {"report": report, "reporter": pseudonym[:8], "media_count": media_count}
            for report, pseudonym, media_count in rows
        ]
        return templates.TemplateResponse(request, "reports.html", {"items": items})

    return app


app = create_app()

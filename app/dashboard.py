"""Read models shared by the officer pages, so a number on the overview is the same number on
the page it links to: the overview counts with the very queries and services those pages use."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, col, func, select

from app.deadlines import Countdown
from app.drafts import DraftService
from app.models import DRAFT_OPEN, STATUS_HELD, Assessment, Incident, Report
from app.trust import model_spend

STATUS_NEEDS_OFFICER = "needs_officer"
LATEST_REPORTS = 10
ATTENTION_LIMIT = 8
REPORT_WINDOW = timedelta(hours=24)


def active_incidents(session: Session) -> list[Incident]:
    """The incidents /incidents lists: active groupings, newest activity first."""
    return list(
        session.exec(
            select(Incident)
            .where(Incident.active == True)  # noqa: E712
            .order_by(col(Incident.last_seen).desc(), col(Incident.id).desc())
        ).all()
    )


def incident_trust(incident: Incident) -> dict[str, int | None]:
    """The rollup owns trust counts; legacy incidents show unknown until refreshed."""
    return {
        "reporter_count": incident.latest_facts.get("reporter_count"),
        "flagged_report_count": incident.latest_facts.get("flagged_report_count", 0),
    }


def needs_attention(incident: Incident) -> bool:
    """Grouping waits for the officer, or a report in it carries a high or medium trust signal."""
    return incident.status == STATUS_NEEDS_OFFICER or bool(
        incident_trust(incident)["flagged_report_count"]
    )


@dataclass(frozen=True)
class NextDeadline:
    """The soonest filing deadline still ahead, and how many have already passed."""

    label: str
    countdown: Countdown | None
    anchor: str | None
    incident_id: int | None
    overdue: int
    total: int


def next_deadline(statuses: list[Any]) -> NextDeadline:
    """Over every Notice of Loss clock on /drafts (approval does not stop a clock, filing does)."""
    clocks = [(s, row, c) for s in statuses for row, c in s.clocks]
    ahead = sorted((c.due_at, i) for i, (_s, _row, c) in enumerate(clocks) if not c.overdue)
    overdue = sum(1 for _s, _row, c in clocks if c.overdue)
    if not ahead:
        return NextDeadline("Filing deadline", None, None, None, overdue, len(clocks))
    status, row, countdown = clocks[ahead[0][1]]
    return NextDeadline(
        row["label"],
        countdown,
        status.anchor,
        status.incident.incident_id,
        overdue,
        len(clocks),
    )


def overview(session: Session, drafts: DraftService, now: datetime) -> dict[str, Any]:
    """Everything the overview page shows, from the helpers its linked pages use."""
    incidents = active_incidents(session)
    city, _statuses, history = drafts.overview()
    report_counts = dict(
        session.exec(select(Report.status, func.count()).group_by(Report.status)).all()
    )
    recent = session.exec(
        select(func.count()).select_from(Report).where(Report.received_at > now - REPORT_WINDOW)
    ).one()
    attention = sorted(
        (i for i in incidents if needs_attention(i)),
        key=lambda i: (i.status != STATUS_NEEDS_OFFICER, -i.last_seen.timestamp(), -(i.id or 0)),
    )[:ATTENTION_LIMIT]
    latest = list(
        session.exec(
            select(Report, Assessment)
            .join(Assessment, col(Assessment.report_id) == Report.id, isouter=True)
            .order_by(col(Report.received_at).desc(), col(Report.id).desc())
            .limit(LATEST_REPORTS)
        ).all()
    )
    return {
        "incidents_open": len(incidents),
        "incidents_review": sum(1 for i in incidents if i.status == STATUS_NEEDS_OFFICER),
        "reports_total": sum(report_counts.values()),
        "reports_recent": recent,
        "reports_held": report_counts.get(STATUS_HELD, 0),
        "drafts_open": sum(1 for d in history if d.status == DRAFT_OPEN),
        "next_deadline": next_deadline(drafts.nol_statuses(city, history)),
        "model_spend": model_spend(session),
        "attention": attention,
        "attention_total": sum(1 for i in incidents if needs_attention(i)),
        "trust": {i.id: incident_trust(i) for i in attention},
        "latest": latest,
    }

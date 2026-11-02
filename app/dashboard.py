"""Read models shared by the officer pages, so a number on the overview is the same number on
the page it links to: the overview counts with the very queries and services those pages use."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, or_
from sqlmodel import Session, col, func, select

from app.deadlines import Countdown
from app.drafts import KIND_RDANA, DraftService
from app.models import (
    DRAFT_APPROVED,
    DRAFT_OPEN,
    STATUS_HELD,
    STATUS_NOT_A_REPORT,
    Assessment,
    Incident,
    ModelCall,
    Report,
    utcnow,
)
from app.modes import plain_reason
from app.nol_form import commodity_for_kind
from app.review import ReviewCard, queue
from app.screening import counted
from app.trace import ModelFamily, model_family
from app.trust import SPEND_WINDOW, model_spend

STATUS_NEEDS_OFFICER = "needs_officer"
LATEST_REPORTS = 10
ATTENTION_LIMIT = 8
REPORT_WINDOW = timedelta(hours=24)
NEW_REPORT_WINDOW = timedelta(hours=6)  # Home lists reports this recent as news
NEW_REPORTS_SHOWN = 3
SOON = timedelta(hours=24)  # a filing deadline closer than this goes to the top of Home
GROUP_ABOVE = 2  # more barangays than this needing a draft become one to-do item
CAUGHT_UP_INCIDENTS = 5


def report_href(report_id: int, incident_id: int | None = None) -> str:
    """A stable link to one report: its place on the incident timeline, else the reports page
    around it (templates use it through the report_href macro in _links.html)."""
    if incident_id:
        return f"/incidents/{incident_id}#report-{report_id}"
    return f"/reports?around={report_id}#report-{report_id}"


def plain_output_label(draft: Any) -> str:
    """How Simple mode names a draft's output: the form and the place, no incident number
    (app.drafts.output_label is Technical mode's)."""
    if (commodity := commodity_for_kind(draft.kind)) is not None:
        return f"PCIC Notice of Loss ({commodity.name}) · {draft.barangay or 'place unknown'}"
    if draft.kind == KIND_RDANA:
        return f"RDANA · {draft.barangay}"
    return "Damaged houses · city"


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


@dataclass(frozen=True)
class ModelUsage:
    model: str
    family: ModelFamily | None
    calls: int
    cached_calls: int
    cost: float
    median_ms: float | None  # over live (uncached) calls; a cache hit takes no model time

    @property
    def cache_rate(self) -> float:
        return self.cached_calls / self.calls if self.calls else 0.0


def model_usage(session: Session, now: datetime | None = None) -> list[ModelUsage]:
    """Calls, cache hits, cost and median live latency per model over the spend cap's rolling
    window (app.trust.SPEND_WINDOW), in one query; the `at` index bounds it."""
    now = now or utcnow()
    partition = (ModelCall.model, ModelCall.cached)
    recent = (
        select(
            ModelCall.model,
            ModelCall.cached,
            ModelCall.cost_estimate,
            ModelCall.latency_ms,
            func.row_number()
            .over(partition_by=partition, order_by=ModelCall.latency_ms)
            .label("position"),
            func.count().over(partition_by=partition).label("size"),
        )
        .where(ModelCall.at > now - SPEND_WINDOW, ModelCall.at <= now)
        .subquery()
    )
    # The median's one or two middle positions: 2 * position is size + 1 (odd), or size or
    # size + 2 (even); no integer division needed.
    middle = or_(
        recent.c.position * 2 == recent.c.size,
        recent.c.position * 2 == recent.c.size + 1,
        recent.c.position * 2 == recent.c.size + 2,
    )
    rows = session.exec(
        select(
            recent.c.model,
            func.count(),
            func.sum(case((recent.c.cached, 1), else_=0)),
            func.total(recent.c.cost_estimate),
            func.avg(case((~recent.c.cached & middle, recent.c.latency_ms))),
        )
        .group_by(recent.c.model)
        .order_by(func.count().desc(), recent.c.model)
    ).all()
    return [
        ModelUsage(model, model_family(model), calls, cached, cost, median)
        for model, calls, cached, cost, median in rows
    ]


def nav_counts(session: Session) -> dict[str, int]:
    """The counts on navigation destinations (NavItem.badge): the /review queue, counted by
    that page's own helper."""
    return {"review": len(queue(session))}


def overview(session: Session, drafts: DraftService, now: datetime) -> dict[str, Any]:
    """Everything Home shows (the to-do list, and the overview's figures in Technical mode),
    from the helpers its linked pages use."""
    incidents = active_incidents(session)
    city, statuses, history = drafts.overview()
    nol_statuses = drafts.nol_statuses(city, history)
    report_counts = dict(
        session.exec(select(Report.status, func.count()).group_by(Report.status)).all()
    )
    recent = session.exec(
        select(func.count())
        .select_from(Report)
        .where(Report.received_at > now - REPORT_WINDOW, counted())
    ).one()
    attention = sorted(
        (i for i in incidents if needs_attention(i)),
        key=lambda i: (i.status != STATUS_NEEDS_OFFICER, -i.last_seen.timestamp(), -(i.id or 0)),
    )[:ATTENTION_LIMIT]
    latest = list(
        session.exec(
            select(Report, Assessment)
            .join(Assessment, col(Assessment.report_id) == Report.id, isouter=True)
            .where(counted())
            .order_by(col(Report.received_at).desc(), col(Report.id).desc())
            .limit(LATEST_REPORTS)
        ).all()
    )
    waiting = queue(session)
    return {
        "incidents_open": len(incidents),
        "incidents_review": sum(1 for i in incidents if i.status == STATUS_NEEDS_OFFICER),
        # The /review queue, counted by the same helper as that page.
        "review_total": len(waiting),
        "review_grouping": sum(1 for card in waiting if card.grouping),
        # Messages screened out as not reports are not reports (app.screening).
        "reports_total": sum(report_counts.values()) - report_counts.get(STATUS_NOT_A_REPORT, 0),
        "not_reports": report_counts.get(STATUS_NOT_A_REPORT, 0),
        "reports_recent": recent,
        "reports_held": report_counts.get(STATUS_HELD, 0),
        "drafts_open": sum(1 for d in history if d.status == DRAFT_OPEN),
        "next_deadline": next_deadline(nol_statuses),
        "model_spend": model_spend(session),
        # Same window and clock as the spend tile, so the strip's costs add up to it.
        "model_usage": model_usage(session),
        "attention": attention,
        "attention_total": sum(1 for i in incidents if needs_attention(i)),
        "trust": {i.id: incident_trust(i) for i in attention},
        "latest": latest,
        "todo": todo(waiting, nol_statuses, statuses, history, latest, now),
        # "All caught up": the latest incidents instead of an empty page.
        "recent_incidents": incidents[:CAUGHT_UP_INCIDENTS],
    }


# Home's to-do list (Simple mode). Each item is one card with one primary button, in plain words;
# the urgency rank sorts them (lower first), then the deadline or the newest first.
URGENT, SOON_DUE, REVIEW, APPROVE, LATER_DUE, MAKE, NEWS = range(7)


@dataclass(frozen=True)
class TodoItem:
    rank: int
    title: str
    detail: str
    href: str
    action: str  # the button: what happens when it is tapped
    icon: str
    tone: str = ""  # "alert" | "warn" | "" (the card's colour only repeats what the words say)
    due: Countdown | None = None
    order: float = 0.0  # within a rank: lower first

    @property
    def key(self) -> tuple[int, float]:
        return (self.rank, self.order)

    @property
    def news(self) -> bool:
        """Something to know rather than to do (its button is the quieter tonal one)."""
        return self.rank == NEWS


def ago(at: datetime, now: datetime) -> str:
    """How long ago, the way people say it: "just now", "2 min ago", "an hour ago", "yesterday"."""
    seconds = max(0, int((now - at).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    hours = seconds // 3600
    if hours < 24:
        return "an hour ago" if hours == 1 else f"{hours} h ago"
    days = hours // 24
    return "yesterday" if days == 1 else f"{days} days ago"


def _review_item(cards: list[ReviewCard]) -> TodoItem | None:
    if not cards:
        return None
    reasons: dict[str, int] = {}
    for card in cards:
        for reason in card.reasons:
            plain = plain_reason(reason)
            reasons[plain] = reasons.get(plain, 0) + 1
    detail = "; ".join(
        f"{reason.lower()} ({count})" if count > 1 else reason.lower()
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])
    )
    n = len(cards)
    return TodoItem(
        REVIEW,
        f"{n} report{'s need' if n != 1 else ' needs'} your check",
        detail[:1].upper() + detail[1:],
        "/review",
        "Review",
        "fact_check",
        "warn",
    )


def _nol_items(statuses: list[Any], now: datetime) -> list[TodoItem]:
    """A Notice of Loss whose filing clock runs and that still needs the officer: a draft to
    approve, or none yet (or a rejected or out-of-date one) to make. Approved ones are done."""
    items = []
    for status in statuses:
        if not status.clocks:
            continue
        _row, clock = status.clocks[0]
        latest = status.latest
        if latest is not None and latest.status == DRAFT_APPROVED and not status.stale:
            continue
        place = status.incident.barangay or "Place unknown"
        name = f"{place} {status.commodity.name}"
        rank = URGENT if clock.overdue else SOON_DUE if clock.due_at - now < SOON else LATER_DUE
        if latest is not None and latest.status == DRAFT_OPEN:
            title, href, action = (
                f"{name}: Notice of Loss ready to approve",
                f"/drafts/{latest.id}",
                "Check and approve",
            )
        else:  # the countdown is shown beside the title (it keeps ticking on the page)
            title, href, action = (
                f"{name}: Notice of Loss",
                f"/drafts#{status.anchor}",
                "Make the draft",
            )
        items.append(
            TodoItem(
                rank,
                title,
                "The fisherfolk must file it with PCIC in time. Ulat drafts it from the "
                "reports and photos; it is filed only by them.",
                href,
                action,
                "schedule",
                "alert" if clock.overdue else "warn",
                clock,
                clock.due_at.timestamp(),
            )
        )
    return items


def _draft_items(statuses: list[Any], history: list[Any]) -> list[TodoItem]:
    """RDANA and damaged-houses drafts to approve, and barangays with reports but no current
    RDANA draft (grouped into one item when there are many)."""
    items = []
    for d in history:
        if d.status != DRAFT_OPEN or commodity_for_kind(d.kind) is not None:
            continue
        where = d.barangay if d.kind == KIND_RDANA else "City"
        form = "RDANA draft" if d.kind == KIND_RDANA else "damaged-houses table"
        items.append(
            TodoItem(
                APPROVE,
                f"{where}: {form} ready to approve",
                "Check what Ulat filled in from the reports, then approve it or say what is wrong.",
                f"/drafts/{d.id}",
                "Check and approve",
                "description",
                order=-d.created_at.timestamp(),
            )
        )
    open_rdana = {d.barangay for d in history if d.status == DRAFT_OPEN and d.kind == KIND_RDANA}
    missing = [
        s
        for s in statuses
        if s.evidence and s.barangay not in open_rdana and (s.latest is None or s.stale)
    ]
    if len(missing) > GROUP_ABOVE:
        items.append(
            TodoItem(
                MAKE,
                f"{len(missing)} barangays have new reports for the RDANA form",
                ", ".join(s.barangay for s in missing),
                "/drafts",
                "Make the drafts",
                "description",
            )
        )
    else:
        for s in missing:
            items.append(
                TodoItem(
                    MAKE,
                    f"{s.barangay}: {'update' if s.latest else 'make'} the RDANA draft",
                    "New reports came in after the last draft."
                    if s.latest
                    else "Reports from this barangay are ready to fill in the form.",
                    f"/drafts#barangay-{s.barangay.replace(' ', '-')}",
                    "Make the draft",
                    "description",
                )
            )
    return items


def _news_items(latest: list[tuple[Report, Any]], now: datetime) -> list[TodoItem]:
    items = []
    for report, _assessment in latest:
        if now - report.received_at > NEW_REPORT_WINDOW or len(items) >= NEW_REPORTS_SHOWN:
            break
        items.append(
            TodoItem(
                NEWS,
                f"New report from {report.barangay or 'a place not found yet'}",
                ago(report.received_at, now),
                report_href(report.id, report.incident_id),
                "Open",
                "forum",
                order=-report.received_at.timestamp(),
            )
        )
    return items


def todo(
    cards: list[ReviewCard],
    nol_statuses: list[Any],
    statuses: list[Any],
    history: list[Any],
    latest: list[tuple[Report, Any]],
    now: datetime,
) -> list[TodoItem]:
    """Home's to-do list, most urgent first, from the same services as the pages it links to."""
    items = _nol_items(nol_statuses, now) + _draft_items(statuses, history)
    items += _news_items(latest, now)
    if (review := _review_item(cards)) is not None:
        items.append(review)
    return sorted(items, key=lambda item: item.key)

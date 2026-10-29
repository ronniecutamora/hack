"""Officer drafts: generate versioned RDANA forms, damaged-houses tables and PCIC Notices of Loss,
then approve or reject them.

A draft is generated once and never edited. Generating again makes version n+1 and marks the
previous undecided draft of the same output `superseded`; approved and rejected drafts stay as
they were decided. Every generate, supersede, approve and reject writes an append-only
DraftEvent. No database transaction is held across the summary model call.

Versioning, staleness, approval and the audit are the same for every output; an output is
identified by its kind, barangay and incident. Notice of Loss kinds come from the commodity
entries in app.nol_form, so this module never names a commodity.
"""

import csv
import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.config import Settings
from app.counting import COUNTING_RULE, BarangayEvidence, CityEvidence, IncidentEvidence, collect
from app.deadlines import Countdown
from app.meta.llm import AgentLLM
from app.models import (
    DRAFT_APPROVED,
    DRAFT_OPEN,
    DRAFT_REJECTED,
    DRAFT_SUPERSEDED,
    MANILA,
    Draft,
    DraftEvent,
    manila_time,
    utcnow,
)
from app.nol import (
    annex,
    countdowns,
    deadline_rows,
    eligible_incidents,
    fill_nol,
    occurred_at,
)
from app.nol import fingerprint as nol_fingerprint
from app.nol_form import COMMODITIES, Commodity, commodity_for_kind
from app.rdana import EventInput, count_field, fill_rdana, summary_counts
from app.rdana_form import CITY, PROVINCE, REGION
from app.rdana_summary import summarize
from app.trust import model_spend

KIND_RDANA = "rdana"
KIND_DAMAGED_HOUSES = "damaged_houses"
KINDS = (KIND_RDANA, KIND_DAMAGED_HOUSES)
DECISIONS = {"approve": DRAFT_APPROVED, "reject": DRAFT_REJECTED}
REASON_MIN, REASON_MAX = 5, 1000
DECIDED_BY_MAX = 120
EVENT_NAME_MAX, EVENT_TYPE_MAX = 160, 80
CSV_HEADER = (
    "Status",
    "Region",
    "Province",
    "City/Municipality",
    "Barangay",
    "Total",
    "Totally",
    "Partially",
    "Evidence report ids",
    "Stated by only one reporter",
    "From flagged reports",
    "Pending grouping review",
    "Pending report ids",
)
LIVE_CSV_STATUS = "LIVE (NOT APPROVED)"
# An officer's event date this long before the first report is more likely a typo than a
# typhoon nobody reported for a month; RDANA forms are due within days (app/data/deadlines.json).
EVENT_MAX_DAYS_BEFORE_FIRST_REPORT = 30
CITY_TOTAL = "City total"
CSV_BOM = "\ufeff"  # spreadsheets then read the file as UTF-8 ("Biñan")


class DraftError(Exception):
    status_code = 422


class DraftNotFound(DraftError):
    status_code = 404


class DraftConflict(DraftError):
    status_code = 409


def clean_text(
    value: str | None, *, label: str, max_length: int, min_length: int = 0
) -> str | None:
    """Trimmed single-spaced text or None; DraftError when too short or too long."""
    text = " ".join((value or "").split())
    if len(text) > max_length:
        raise DraftError(f"{label} must be at most {max_length} characters.")
    if len(text) < min_length:
        raise DraftError(f"{label} must be at least {min_length} characters.")
    return text or None


def city_fingerprint(city: CityEvidence) -> str:
    joined = "|".join(f"{n}:{e.fingerprint()}" for n, e in city.barangays.items())
    return hashlib.sha256(joined.encode()).hexdigest()


def current_fingerprint(city: CityEvidence, draft: Draft) -> str | None:
    """The fingerprint a draft of this output would get now; None when no evidence is left."""
    if draft.kind == KIND_DAMAGED_HOUSES:
        return city_fingerprint(city)
    if draft.incident_id is not None:  # a Notice of Loss: its incident plus that incident's photos
        incident = city.incident(draft.incident_id)
        return nol_fingerprint(incident) if incident else None
    evidence = city.barangays.get(draft.barangay or "")
    return evidence.fingerprint() if evidence else None


def _pending_holder(draft: Draft) -> dict[str, Any]:
    """The part of a draft's content that records the reports pending grouping review."""
    return draft.content.get("total", {}) if draft.kind == KIND_DAMAGED_HOUSES else draft.content


def predates_pending_bucket(draft: Draft) -> bool:
    """Generated before figures pending grouping review were kept apart: its totals may count
    them, so it is never current (see `is_stale`)."""
    return "pending_report_ids" not in _pending_holder(draft)


def is_stale(draft: Draft, city: CityEvidence) -> bool:
    """The evidence changed after the draft was generated, or it predates the pending bucket."""
    if predates_pending_bucket(draft):
        return True
    return draft.evidence_fingerprint != current_fingerprint(city, draft)


def same_output(draft: Draft):
    """SQL condition for every version of the same output as `draft`: a per-incident output is
    its incident's, wherever the incident is placed now; the others are per barangay or city."""
    if draft.incident_id is not None:
        return (Draft.kind == draft.kind) & (Draft.incident_id == draft.incident_id)
    barangay = Draft.barangay
    return (Draft.kind == draft.kind) & (
        barangay.is_(None) if draft.barangay is None else barangay == draft.barangay
    )


def output_label(draft: Draft) -> str:
    """How the drafts list names a draft's output."""
    if (commodity := commodity_for_kind(draft.kind)) is not None:
        where = f" · {draft.barangay}" if draft.barangay else ""
        return f"PCIC Notice of Loss ({commodity.name}) · incident #{draft.incident_id}{where}"
    if draft.kind == KIND_RDANA:
        return f"RDANA · {draft.barangay}"
    return "Damaged houses · city"


def regenerate_form(draft: Draft) -> tuple[str, dict[str, str]]:
    """Where to post, and with which fields, to generate a new version of this output."""
    if (commodity := commodity_for_kind(draft.kind)) is not None:
        return "/drafts/notice-of-loss", {
            "commodity": commodity.key,
            "incident_id": str(draft.incident_id),
        }
    if draft.kind == KIND_DAMAGED_HOUSES:
        return "/drafts/damaged-houses", {}
    fields = {"barangay": draft.barangay or ""}
    if draft.event_name:
        fields["event_name"] = draft.event_name
    if draft.event_type:
        fields["event_type"] = draft.event_type
    if draft.event_at:
        fields["event_at"] = manila_time(draft.event_at).replace(" ", "T")
    return "/drafts", fields


HOUSE_COLUMNS = {"totally": "houses_totally_damaged", "partially": "houses_partially_damaged"}


def damaged_houses(city: CityEvidence) -> dict[str, Any]:
    """DROMIC damaged-houses rows per barangay (counting rule applied) and the city total.

    Only barangays whose reports state a house-damage count are listed; an unstated count is
    empty, never 0. Total is the sum of the confirmed counts. Figures waiting for grouping
    review are never in Total: the Pending column shows them beside it.
    """
    rows = []
    for evidence in city.barangays.values():
        counts = {k: evidence.count(name) for k, name in HOUSE_COLUMNS.items()}
        stated = [c for c in counts.values() if c is not None]
        if not stated:
            continue
        confirmed = [c for c in stated if c.confirmed]
        pending = [c for c in stated if c.pending]
        rows.append(
            {
                "barangay": evidence.barangay,
                "total": sum(c.value for c in confirmed) if confirmed else None,
                **{k: c.value if c and c.confirmed else None for k, c in counts.items()},
                "single_source": sum(c.single_source_value for c in confirmed),
                "flagged": sum(c.flagged_value for c in confirmed),
                "pending": sum(c.pending_value for c in pending) if pending else None,
                "pending_report_ids": sorted({r for c in pending for r in c.pending_report_ids}),
                "report_ids": sorted({r for c in confirmed for r in c.report_ids}),
                "evidence": [e for c in stated for e in count_field(c)["evidence"]],
            }
        )

    def column(name: str) -> int | None:
        values = [r[name] for r in rows if r[name] is not None]
        return sum(values) if values else None

    def ids(name: str) -> list[int]:
        return sorted({r for row in rows for r in row[name]})

    return {
        "region": REGION,
        "province": PROVINCE,
        "city": CITY,
        "rows": rows,
        "total": {
            **{
                name: column(name)
                for name in ("total", *HOUSE_COLUMNS, "single_source", "flagged", "pending")
            },
            "report_ids": ids("report_ids"),
            "pending_report_ids": ids("pending_report_ids"),
        },
        "counting_rule": COUNTING_RULE,
        "unplaced_incidents": len(city.unplaced),
    }


def csv_status(draft: Draft) -> str:
    """The Status cell of a snapshot's CSV, e.g. "APPROVED v3" or "DRAFT v2 (NOT APPROVED)"."""
    status = f"{draft.status.upper()} v{draft.version}"
    return status if draft.status == DRAFT_APPROVED else f"{status} (NOT APPROVED)"


def damaged_houses_csv(table: dict[str, Any], status: str) -> str:
    """CSV in the DROMIC column order, with a UTF-8 BOM so spreadsheets read "Biñan" right.

    Every row starts with the file's status, so a copied row still says whether it is the live,
    unapproved table or which snapshot it came from. Snapshots made before a column existed
    leave it empty; their stored values are never recomputed.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)

    def cell(value: int | None) -> str:
        return "" if value is None else f"{value:g}"

    for row in [*table["rows"], {"barangay": CITY_TOTAL, **table["total"]}]:
        writer.writerow(
            [
                status,
                table["region"],
                table["province"],
                table["city"],
                row["barangay"],
                cell(row["total"]),
                cell(row["totally"]),
                cell(row["partially"]),
                ";".join(str(r) for r in row["report_ids"]),
                cell(row.get("single_source") if row["total"] is not None else None),
                cell(row.get("flagged") if row["total"] is not None else None),
                cell(row.get("pending")),
                ";".join(str(r) for r in row.get("pending_report_ids", [])),
            ]
        )
    return CSV_BOM + out.getvalue()


def pending_report_ids(draft: Draft) -> list[int]:
    """Reports whose figures waited for grouping review when the draft was generated."""
    return _pending_holder(draft).get("pending_report_ids", [])


@dataclass
class BarangayStatus:
    barangay: str
    evidence: BarangayEvidence | None
    latest: Draft | None
    approved: Draft | None

    stale: bool  # the counted evidence changed after the latest draft was generated


@dataclass
class NolStatus:
    """One incident eligible for a commodity's Notice of Loss, its clock and its drafts."""

    commodity: Commodity
    incident: IncidentEvidence
    clocks: list[tuple[dict[str, Any], Countdown]]  # the Notice of Loss deadline first
    latest: Draft | None = None
    approved: Draft | None = None
    stale: bool = False
    flagged_report_ids: list[int] = field(default_factory=list)  # high/medium trust signals

    @property
    def anchor(self) -> str:
        return f"nol-{self.commodity.key}-{self.incident.incident_id}"


class DraftService:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        llm: AgentLLM | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """`clock` (naive UTC) sets countdowns and Notice of Loss draft times; tests inject one."""
        self.settings, self.engine, self.llm, self.clock = settings, engine, llm, clock

    def is_stale(self, draft: Draft) -> bool:
        """The counted evidence changed after this draft was generated."""
        return is_stale(draft, self.evidence())

    def evidence(self) -> CityEvidence:
        with Session(self.engine) as session:
            return collect(session)

    def spend_block(self) -> str | None:
        with Session(self.engine) as session:
            spend = model_spend(session)
        cap = self.settings.daily_spend_cap_usd
        if spend >= cap:
            return f"Spend cap reached (${spend:.2f} of ${cap:.2f} in 24 h); no model call."
        return None

    async def generate_rdana(self, barangay: str, event: EventInput | None = None) -> Draft:
        event = event or EventInput()
        evidence = self.evidence().barangays.get(barangay)
        if evidence is None:
            raise DraftError(f"No assessed reports are placed in {barangay!r}.")
        if event.at is not None:
            check_event_at(event.at, evidence.members[0].at, utcnow())
        counts = summary_counts(evidence)
        summary = await summarize(self.llm, evidence, counts, blocked=self.spend_block)
        drafted_at = utcnow()
        content = fill_rdana(evidence, event, drafted_at, summary)
        draft = Draft(
            kind=KIND_RDANA,
            barangay=barangay,
            version=0,
            event_name=event.name,
            event_type=event.type,
            event_at=event.at,
            content=content,
            evidence_fingerprint=evidence.fingerprint(),
            summary_model=summary.model,
            summary_fallback=summary.fallback,
            summary_validation=summary.attempts,
            created_at=drafted_at,
        )
        detail = {
            "reports": len(evidence.report_ids),
            "incidents": len(evidence.incidents),
            "summary_model": summary.model,
            "summary_fallback": summary.fallback,
            "fallback_reason": summary.fallback_reason,
        }
        return self._insert(draft, detail)

    def generate_damaged_houses(self) -> Draft:
        city = self.evidence()
        table = damaged_houses(city)
        if not table["rows"]:
            raise DraftError("No report states a number of damaged houses yet.")
        now = utcnow()
        draft = Draft(
            kind=KIND_DAMAGED_HOUSES,
            barangay=None,
            version=0,
            content={**table, "drafted_at": manila_time(now)},
            evidence_fingerprint=city_fingerprint(city),
            created_at=now,
        )
        return self._insert(draft, {"rows": len(table["rows"]), "total": table["total"]["total"]})

    def generate_nol(self, commodity_key: str, incident_id: int) -> Draft:
        """One incident's Notice of Loss for a commodity. Deterministic: no model call."""
        commodity = COMMODITIES.get(commodity_key)
        if commodity is None:
            raise DraftError(f"Unknown insurance commodity {commodity_key!r}.")
        with Session(self.engine) as session:
            city = collect(session)
            incident = next(
                (i for i in eligible_incidents(commodity, city) if i.incident_id == incident_id),
                None,
            )
            if incident is None:
                raise DraftError(
                    f"Incident #{incident_id} has no assessed report of {commodity.name} damage."
                )
            entries = annex(session, incident)
        drafted_at = self.clock()
        draft = Draft(
            kind=commodity.kind,
            barangay=incident.barangay or None,
            incident_id=incident_id,
            version=0,
            content=fill_nol(commodity, incident, entries, drafted_at),
            evidence_fingerprint=nol_fingerprint(incident),
            created_at=drafted_at,
        )
        detail = {"reports": len(incident.report_ids), "commodity": commodity.key}
        return self._insert(draft, detail)

    def _insert(self, draft: Draft, detail: dict[str, Any]) -> Draft:
        """Number the version and supersede open drafts in one write transaction."""
        with Session(self.engine, expire_on_commit=False) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            same = same_output(draft)
            latest = session.exec(select(func.max(Draft.version)).where(same)).one()
            draft.version = (latest or 0) + 1
            session.add(draft)
            session.flush()
            for old in session.exec(select(Draft).where(same, Draft.status == DRAFT_OPEN)).all():
                if old.id == draft.id:
                    continue
                old.status = DRAFT_SUPERSEDED
                session.add(old)
                session.add(
                    DraftEvent(
                        draft_id=old.id,
                        action=DRAFT_SUPERSEDED,
                        reason=f"Version {draft.version} was generated.",
                        detail={"superseded_by": draft.id},
                    )
                )
            session.add(
                DraftEvent(
                    draft_id=draft.id,
                    action="generated",
                    detail={"version": draft.version, **detail},
                    created_at=draft.created_at,
                )
            )
            session.commit()
            return draft

    def decide(
        self,
        draft_id: int,
        action: str,
        *,
        reason: str | None,
        decided_by: str | None,
        confirm_stale: bool = False,
    ) -> Draft:
        """Approve or reject an open draft once.

        Staleness is checked again here, inside the write transaction: approving a draft whose
        evidence changed since it was generated needs `confirm_stale`, and the audit records it.
        """
        if action not in DECISIONS:
            raise DraftNotFound(f"Unknown decision {action!r}.")
        status = DECISIONS[action]
        reason = clean_text(
            reason,
            label="A rejection reason" if status == DRAFT_REJECTED else "The reason",
            max_length=REASON_MAX,
            min_length=REASON_MIN if status == DRAFT_REJECTED else 0,
        )
        decided_by = clean_text(decided_by, label="Decided by", max_length=DECIDED_BY_MAX)
        with Session(self.engine, expire_on_commit=False) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise DraftNotFound("Draft not found.")
            if draft.status != DRAFT_OPEN:
                raise DraftConflict(
                    f"Draft {draft_id} is {draft.status}; only a draft can be decided."
                )
            stale = is_stale(draft, collect(session))
            if status == DRAFT_APPROVED and stale and not confirm_stale:
                why = (
                    f"Version {draft.version} was generated before figures pending grouping "
                    "review were kept out of the totals."
                    if predates_pending_bucket(draft)
                    else f"The evidence changed after version {draft.version} was generated."
                )
                raise DraftConflict(
                    f"{why} Generate a new version, or confirm that you approve this version "
                    "as it is."
                )
            pending = pending_report_ids(draft)
            if status == DRAFT_APPROVED and pending and len(reason or "") < REASON_MIN:
                raise DraftError(
                    f"Figures from reports {', '.join(map(str, pending))} wait for grouping "
                    "review and are not in the totals. Write why you approve this version "
                    f"anyway (at least {REASON_MIN} characters)."
                )
            draft.status, draft.decided_at = status, utcnow()
            draft.decision_reason, draft.decided_by = reason, decided_by
            session.add(draft)
            session.add(
                DraftEvent(
                    draft_id=draft.id,
                    action=status,
                    reason=reason,
                    actor=decided_by,
                    detail={"stale": stale, **({"pending_report_ids": pending} if pending else {})},
                    created_at=draft.decided_at,
                )
            )
            session.commit()
            return draft

    def get(self, draft_id: int) -> tuple[Draft, list[DraftEvent], Draft | None]:
        """A draft, its audit trail and the newest version of the same output."""
        with Session(self.engine) as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise DraftNotFound("Draft not found.")
            events = session.exec(
                select(DraftEvent)
                .where(DraftEvent.draft_id == draft_id)
                .order_by(DraftEvent.created_at, DraftEvent.id)
            ).all()
            newest = session.exec(
                select(Draft).where(same_output(draft)).order_by(Draft.version.desc())
            ).first()
            return draft, list(events), newest

    def overview(self) -> tuple[CityEvidence, list[BarangayStatus], list[Draft]]:
        """Barangays with evidence or drafts, each with its latest and approved RDANA draft."""
        with Session(self.engine) as session:
            city = collect(session)
            drafts = list(
                session.exec(select(Draft).order_by(Draft.created_at.desc(), Draft.id.desc())).all()
            )
        statuses = []
        names = set(city.barangays) | {d.barangay for d in drafts if d.kind == KIND_RDANA}
        for name in sorted(n for n in names if n):
            mine = [d for d in drafts if d.kind == KIND_RDANA and d.barangay == name]
            latest = max(mine, key=lambda d: d.version, default=None)
            statuses.append(
                BarangayStatus(
                    name,
                    city.barangays.get(name),
                    latest,
                    max(
                        (d for d in mine if d.status == DRAFT_APPROVED),
                        key=lambda d: d.version,
                        default=None,
                    ),
                    latest is not None and is_stale(latest, city),
                )
            )
        return city, statuses, drafts

    def clocks(self, draft: Draft) -> list[tuple[dict[str, Any], Countdown]]:
        """A Notice of Loss draft's deadlines as stored, counted down to now; approval does not
        stop them."""
        return countdowns(draft.content.get("deadlines", []), self.clock())

    def nol_statuses(
        self,
        city: CityEvidence | None = None,
        drafts: list[Draft] | None = None,
        incident_id: int | None = None,
    ) -> list[NolStatus]:
        """Every incident eligible for a Notice of Loss, per commodity, with its live clock."""
        if city is None or drafts is None:
            with Session(self.engine) as session:
                city = collect(session)
                per_incident = select(Draft).where(col(Draft.incident_id).is_not(None))
                drafts = list(session.exec(per_incident).all())
        now = self.clock()
        statuses = []
        for commodity in COMMODITIES.values():
            for incident in eligible_incidents(commodity, city):
                if incident_id is not None and incident.incident_id != incident_id:
                    continue
                mine = [
                    d
                    for d in drafts
                    if d.kind == commodity.kind and d.incident_id == incident.incident_id
                ]
                latest = max(mine, key=lambda d: d.version, default=None)
                approved = [d for d in mine if d.status == DRAFT_APPROVED]
                rows = deadline_rows(commodity, occurred_at(incident))
                statuses.append(
                    NolStatus(
                        commodity,
                        incident,
                        countdowns(rows, now),
                        latest,
                        max(approved, key=lambda d: d.version, default=None),
                        latest is not None and is_stale(latest, city),
                        sorted({m.id for m in incident.members if m.flagged}),
                    )
                )
        return statuses


def check_event_at(at: datetime, first_report: datetime, now: datetime) -> None:
    """Reject an officer's event time in the future or far before the first report (naive UTC).

    A time after the first report is allowed (the officer may mean a later landfall) and the
    form shows it as a warning.
    """
    if at > now:
        raise DraftError(
            f"Date and time of event ({manila_time(at)}) is in the future (Philippine time)."
        )
    if at < first_report - timedelta(days=EVENT_MAX_DAYS_BEFORE_FIRST_REPORT):
        raise DraftError(
            f"Date and time of event ({manila_time(at)}) is more than "
            f"{EVENT_MAX_DAYS_BEFORE_FIRST_REPORT} days before the earliest report "
            f"({manila_time(first_report)}). Check the date."
        )


def parse_event_at(value: str | None) -> datetime | None:
    """An HTML datetime-local value in Philippine time, as naive UTC."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        local = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DraftError("Date and time of event must look like 2026-10-03T19:10.") from exc
    if local.tzinfo is not None:
        raise DraftError("Date and time of event is Philippine time; leave out the offset.")
    return local.replace(tzinfo=MANILA).astimezone(UTC).replace(tzinfo=None)

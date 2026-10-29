"""Officer review: what waits for a human, and the audited overrides that resolve it.

The agent marks what it cannot decide (an uncertain or failed duplicate check, an unknown place),
flags trust evidence and cannot see fallen trees. The officer resolves each of these here:

- merge a report into an incident, or keep it as its own incident (splitting it out into a new
  one when its incident has other reports, which undoes a wrong merge);
- set or correct its barangay (with a reason), after which it is grouped again;
- review a trust signal (genuine or a problem) and confirm or rule out a fallen tree;
- block or unblock the reporter, with the same function as `python -m app.trust`.

Every override is one append-only `OfficerAction` row (triggers reject updates and deletes),
written in the same transaction as the state it changes, so the audit and the state never
disagree. Two overrides are state the agent itself obeys, kept on the report: a grouping pin
(`Report.officer_incident_id`) that re-grouping never moves, and a barangay
(`Report.officer_barangay`) that re-assessment never overwrites. Signal and fallen-tree
verdicts are read from the log (`app.incidents.officer_reviews`, latest wins).

The counts, the pending bucket and draft staleness follow from the changed rows, and the live
dashboard sees the new audit row: nothing here notifies anyone.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.gazetteer import BY_NAME
from app.incidents import (
    IncidentService,
    incident_title,
    observations,
    officer_reviews,
    refresh_incidents,
    review_report_ids,
)
from app.models import (
    OFFICER_BLOCKED,
    OFFICER_FALLEN_TREE,
    OFFICER_KEPT_SEPARATE,
    OFFICER_MERGED,
    OFFICER_SET_PLACE,
    OFFICER_SIGNAL_REVIEWED,
    OFFICER_SPLIT,
    OFFICER_UNBLOCKED,
    STATUS_ASSESSED,
    AgentDecision,
    Assessment,
    Incident,
    OfficerAction,
    PhotoAssessment,
    Report,
    Reporter,
)
from app.trust import flagging, set_blocked, signal_key

OFFICER_MAX = 40  # initials or a short designation
NOTE_MAX = 500
PLACE_REASON_MIN = 5
SIGNAL_VERDICTS = {"genuine": "checked: genuine", "problem": "checked: a problem"}
TREE_VERDICTS = {"confirmed": "fallen tree confirmed", "absent": "no fallen tree"}
# The officer actions that put a report in an incident (and pin it there).
GROUPING_ACTIONS = (OFFICER_MERGED, OFFICER_KEPT_SEPARATE, OFFICER_SPLIT)
# How each officer action reads in the incident timeline.
ACTION_LABELS = {
    OFFICER_MERGED: "merged by the officer",
    OFFICER_KEPT_SEPARATE: "kept as its own incident by the officer",
    OFFICER_SPLIT: "split into its own incident by the officer",
    OFFICER_SET_PLACE: "place set by the officer",
    OFFICER_SIGNAL_REVIEWED: "trust signal reviewed",
    OFFICER_FALLEN_TREE: "fallen tree reviewed",
    OFFICER_BLOCKED: "reporter blocked",
    OFFICER_UNBLOCKED: "reporter unblocked",
}


class ReviewError(ValueError):
    """A rejected officer action; the page shows the message beside the officer's form."""

    status_code = 400


def clean(value: str | None, *, label: str, max_length: int, min_length: int = 0) -> str | None:
    """Trimmed single-spaced text or None; ReviewError when too short or too long."""
    text = " ".join((value or "").split())
    if len(text) > max_length:
        raise ReviewError(f"{label} must be at most {max_length} characters.")
    if len(text) < min_length:
        raise ReviewError(f"{label} must be at least {min_length} characters.")
    return text or None


def officer_name(value: str | None) -> str:
    """Required until there is a login: the audit must say who decided."""
    name = clean(value, label="Your initials", max_length=OFFICER_MAX)
    if name is None:
        raise ReviewError("Write your initials: the audit must say who decided.")
    return name


@dataclass
class ReviewCard:
    """One report with everything the officer needs to resolve it."""

    report: Report
    assessment: Assessment | None
    incident: Incident | None
    reporter: str  # the pseudonym's first 8 characters, as on /reports
    blocked: bool
    grouping: bool  # its grouping waits for the officer
    comparisons: list[AgentDecision]  # the agent's latest grouping run for this report
    targets: list[Incident]  # where the officer may merge it
    signals: list[tuple[str, dict[str, Any], str | None]]  # (key, signal, verdict) to review
    tree_suggested: bool  # a photo judgment or the text mentions a fallen tree
    tree_verdict: bool | None
    # Its incident has other reports, so "keep separate" splits it out into a new incident.
    splits: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def open_signals(self) -> list[tuple[str, dict[str, Any], str | None]]:
        return [s for s in self.signals if s[2] is None]

    @property
    def needs_review(self) -> bool:
        return bool(self.reasons)


def latest_run(decisions: list[AgentDecision]) -> list[AgentDecision]:
    """The rows of the report's last grouping run. A run is written in one write transaction,
    so its rows have consecutive ids; the run before it ended at a lower, non-adjacent id or
    belongs to a different moment."""
    run: list[AgentDecision] = []
    for decision in sorted(decisions, key=lambda d: d.id, reverse=True):
        if run and (
            decision.id != run[-1].id - 1
            or abs((run[-1].created_at - decision.created_at).total_seconds()) > 5
        ):
            break
        run.append(decision)
    return sorted(run, key=lambda d: d.id)


def merge_targets(session: Session, report: Report, compared: set[int]) -> list[Incident]:
    """Active incidents the agent compared this report with, or in its barangay, but not its own."""
    query = select(Incident).where(Incident.active == True)  # noqa: E712
    conditions = [col(Incident.id).in_(compared)] if compared else []
    if report.barangay:
        conditions.append(Incident.barangay == report.barangay)
    if not conditions:
        return []
    clause = conditions[0] if len(conditions) == 1 else conditions[0] | conditions[1]
    return [
        i
        for i in session.exec(
            query.where(clause).order_by(col(Incident.last_seen).desc(), col(Incident.id).desc())
        ).all()
        if i.id != report.incident_id
    ]


def member_counts(session: Session, incident_ids: set[int | None]) -> dict[int, int]:
    """Reports per incident, for these incidents."""
    ids = incident_ids - {None}
    if not ids:
        return {}
    return dict(
        session.exec(
            select(Report.incident_id, func.count())
            .where(col(Report.incident_id).in_(ids))
            .group_by(Report.incident_id)
        ).all()
    )


def tree_suggested(assessment: Assessment | None, photos: list[PhotoAssessment]) -> bool:
    hazards = ((assessment.facts or {}).get("hazards") or []) if assessment else []
    return "fallen_tree" in hazards or any((p.judgment or {}).get("fallen_tree") for p in photos)


def review_cards(session: Session, report_ids: list[int]) -> list[ReviewCard]:
    """Cards for these reports, in the given order (missing ids are skipped)."""
    if not report_ids:
        return []
    reports = {
        r.id: r for r in session.exec(select(Report).where(col(Report.id).in_(report_ids))).all()
    }
    assessments = {
        a.report_id: a
        for a in session.exec(
            select(Assessment).where(col(Assessment.report_id).in_(report_ids))
        ).all()
    }
    photos: dict[int, list[PhotoAssessment]] = {}
    for photo in session.exec(
        select(PhotoAssessment).where(col(PhotoAssessment.report_id).in_(report_ids))
    ).all():
        photos.setdefault(photo.report_id, []).append(photo)
    decisions: dict[int, list[AgentDecision]] = {}
    for decision in session.exec(
        select(AgentDecision).where(col(AgentDecision.report_id).in_(report_ids))
    ).all():
        decisions.setdefault(decision.report_id, []).append(decision)
    reporters = {
        r.id: r
        for r in session.exec(
            select(Reporter).where(col(Reporter.id).in_({r.reporter_id for r in reports.values()}))
        ).all()
    }
    waiting = review_report_ids(session)
    signal_reviews, tree_reviews = officer_reviews(session)
    sizes = member_counts(session, {r.incident_id for r in reports.values()})
    cards = []
    for report_id in report_ids:
        report = reports.get(report_id)
        if report is None:
            continue
        assessment = assessments.get(report_id)
        everything = decisions.get(report_id, [])
        compared = {d.compared_incident_id for d in everything if d.compared_incident_id}
        reporter = reporters[report.reporter_id]
        signals = [
            (key, s, signal_reviews.get((report_id, key)))
            for s in (assessment.signals or [] if assessment else [])
            if flagging(s) and (key := signal_key(s))
        ]
        card = ReviewCard(
            report=report,
            assessment=assessment,
            incident=session.get(Incident, report.incident_id) if report.incident_id else None,
            reporter=reporter.pseudonym[:8],
            blocked=reporter.blocked,
            grouping=report_id in waiting and report.status == STATUS_ASSESSED,
            comparisons=[d for d in latest_run(everything) if d.compared_incident_id],
            targets=merge_targets(session, report, compared),
            signals=signals,
            tree_suggested=tree_suggested(assessment, photos.get(report_id, [])),
            tree_verdict=tree_reviews.get(report_id),
            splits=sizes.get(report.incident_id, 0) > 1,
        )
        if card.grouping:
            card.reasons.append(
                "duplicate check failed" if report.grouping_error else "grouping uncertain"
            )
            if not report.barangay:
                card.reasons.append("place unknown")
        if card.open_signals:
            n = len(card.open_signals)
            card.reasons.append(f"{n} trust signal{'s' if n != 1 else ''} to check")
        if card.tree_suggested and card.tree_verdict is None:
            card.reasons.append("fallen tree to confirm")
        cards.append(card)
    return cards


def queue(session: Session) -> list[ReviewCard]:
    """Every report waiting for the officer: grouping first, then trust and trees, newest first."""
    signal_reviews, tree_reviews = officer_reviews(session)
    candidates = set(review_report_ids(session))
    for report_id, signals in session.exec(select(Assessment.report_id, Assessment.signals)).all():
        if any(
            flagging(s) and (report_id, signal_key(s)) not in signal_reviews for s in signals or []
        ):
            candidates.add(report_id)
    for report_id, facts in session.exec(select(Assessment.report_id, Assessment.facts)).all():
        if report_id not in tree_reviews and "fallen_tree" in ((facts or {}).get("hazards") or []):
            candidates.add(report_id)
    for report_id, judgment in session.exec(
        select(PhotoAssessment.report_id, PhotoAssessment.judgment)
    ).all():
        if report_id not in tree_reviews and (judgment or {}).get("fallen_tree"):
            candidates.add(report_id)
    ordered = session.exec(
        select(Report.id)
        .where(col(Report.id).in_(candidates))
        .order_by(col(Report.received_at).desc(), col(Report.id).desc())
    ).all()
    cards = [c for c in review_cards(session, list(ordered)) if c.needs_review]
    return sorted(cards, key=lambda c: not c.grouping)  # stable: newest first within each


def timeline(session: Session, incident_id: int) -> list[dict[str, Any]]:
    """Agent decisions and officer actions about one incident, oldest first, labelled by actor."""
    decisions = session.exec(
        select(AgentDecision).where(
            (AgentDecision.incident_id == incident_id)
            | (AgentDecision.compared_incident_id == incident_id)
        )
    ).all()
    member_ids = session.exec(select(Report.id).where(Report.incident_id == incident_id)).all()
    actions = session.exec(
        select(OfficerAction).where(
            (OfficerAction.incident_id == incident_id)
            | (OfficerAction.from_incident_id == incident_id)
            | col(OfficerAction.report_id).in_(member_ids)
        )
    ).all()
    entries = [{"agent": d, "at": d.created_at, "order": (0, d.id)} for d in decisions]
    entries += [{"officer": a, "at": a.created_at, "order": (1, a.id)} for a in actions]
    return sorted(entries, key=lambda e: (e["at"], e["order"]))


class ReviewService:
    """The officer's overrides. `incidents` re-groups a report after its place is set."""

    def __init__(self, engine: Engine, incidents: IncidentService) -> None:
        self.engine = engine
        self.incidents = incidents

    @contextmanager
    def _write(self) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()

    @staticmethod
    def _report(session: Session, report_id: int, *, assessed: bool = False) -> Report:
        report = session.get(Report, report_id)
        if report is None:
            raise LookupError("report not found")
        if assessed and report.status != STATUS_ASSESSED:
            raise ReviewError(
                f"Report #{report_id} is {report.status.replace('_', ' ')}, not assessed: "
                "it can be grouped once its assessment finishes."
            )
        return report

    def _resolve(self, session: Session, report: Report, incident_id: int) -> None:
        """Pin the report's grouping where the officer put it; nothing waits any more."""
        report.incident_id = report.officer_incident_id = incident_id
        report.grouping_needs_officer = False
        report.grouping_error = None
        session.add(report)

    def merge(
        self, report_id: int, incident_id: int, *, officer: str | None, note: str | None = None
    ) -> int:
        officer = officer_name(officer)
        note = clean(note, label="Note", max_length=NOTE_MAX)
        with self._write() as session:
            report = self._report(session, report_id, assessed=True)
            compared = set(
                session.exec(
                    select(AgentDecision.compared_incident_id).where(
                        AgentDecision.report_id == report_id,
                        col(AgentDecision.compared_incident_id).is_not(None),
                    )
                ).all()
            )
            if incident_id not in {i.id for i in merge_targets(session, report, compared)}:
                raise ReviewError(
                    "Choose an active incident the agent compared this report with, or one in "
                    "the same barangay."
                )
            previous = report.incident_id
            self._resolve(session, report, incident_id)
            session.add(
                OfficerAction(
                    kind=OFFICER_MERGED,
                    report_id=report_id,
                    incident_id=incident_id,
                    from_incident_id=previous,
                    officer=officer,
                    note=note,
                )
            )
            session.flush()
            refresh_incidents(session, {previous, incident_id})
        return incident_id

    def keep_separate(self, report_id: int, *, officer: str | None, note: str | None = None) -> int:
        """Pin the report to an incident of its own. If its incident has other reports (a wrong
        merge), it is split out into a new incident; both incidents' rollups are recomputed and
        the audit records a split from the old one. Returns the report's incident."""
        officer = officer_name(officer)
        note = clean(note, label="Note", max_length=NOTE_MAX)
        with self._write() as session:
            report = self._report(session, report_id, assessed=True)
            previous = report.incident_id
            splits = member_counts(session, {previous}).get(previous, 0) > 1
            if previous is None or splits:  # no incident yet, or one it shares: a new one
                member = observations(session)[report_id]
                incident = Incident(
                    title=incident_title(member),
                    barangay=report.barangay,
                    first_seen=report.received_at,
                    last_seen=report.received_at,
                )
                session.add(incident)
                session.flush()
                incident_id = incident.id
            else:
                incident_id = previous
            self._resolve(session, report, incident_id)
            session.add(
                OfficerAction(
                    kind=OFFICER_SPLIT if splits else OFFICER_KEPT_SEPARATE,
                    report_id=report_id,
                    incident_id=incident_id,
                    from_incident_id=previous if splits else None,
                    officer=officer,
                    note=note,
                )
            )
            session.flush()
            refresh_incidents(session, {previous, incident_id})
        return incident_id

    async def set_place(
        self, report_id: int, barangay: str | None, *, officer: str | None, reason: str | None
    ) -> int | None:
        """Fix the barangay, release any grouping pin and group the report again (the pair
        judge may be asked; a failure leaves it in the queue with the error, like the bot)."""
        officer = officer_name(officer)
        reason = clean(reason, label="Reason", max_length=NOTE_MAX, min_length=PLACE_REASON_MIN)
        if barangay not in BY_NAME:
            raise ReviewError("Choose one of Biñan's barangays.")
        with self._write() as session:
            report = self._report(session, report_id)
            previous = report.barangay
            report.barangay = report.officer_barangay = barangay
            report.officer_incident_id = None  # the new place decides the grouping again
            report.grouping_fingerprint = None
            session.add(report)
            assessment = session.exec(
                select(Assessment).where(Assessment.report_id == report_id)
            ).first()
            if assessment is not None:
                assessment.geo_source = "officer"
                assessment.geo_detail = f"set by an officer; before: {previous or 'not found'}"
                session.add(assessment)
            session.add(
                OfficerAction(
                    kind=OFFICER_SET_PLACE,
                    report_id=report_id,
                    incident_id=report.incident_id,
                    officer=officer,
                    note=reason,
                    detail={"barangay": barangay, "previous": previous},
                )
            )
            session.flush()
            refresh_incidents(session, {report.incident_id})
        grouped = await self.incidents.group(report_id)
        return grouped.incident_id if grouped else None

    def review_signal(
        self,
        report_id: int,
        key: str | None,
        verdict: str | None,
        *,
        officer: str | None,
        note: str | None = None,
    ) -> int | None:
        officer = officer_name(officer)
        note = clean(note, label="Note", max_length=NOTE_MAX)
        if verdict not in SIGNAL_VERDICTS:
            raise ReviewError("Choose whether the signal is genuine or a problem.")
        with self._write() as session:
            report = self._report(session, report_id)
            assessment = session.exec(
                select(Assessment).where(Assessment.report_id == report_id)
            ).first()
            signal = next(
                (
                    s
                    for s in (assessment.signals if assessment else None) or []
                    if signal_key(s) == key
                ),
                None,
            )
            if signal is None:
                raise ReviewError("That trust signal is no longer on this report; reload the page.")
            session.add(
                OfficerAction(
                    kind=OFFICER_SIGNAL_REVIEWED,
                    report_id=report_id,
                    incident_id=report.incident_id,
                    officer=officer,
                    note=note,
                    detail={"key": key, "code": signal.get("code"), "verdict": verdict},
                )
            )
            session.flush()
            refresh_incidents(session, {report.incident_id})  # its flagged count
        return report.incident_id

    def review_fallen_tree(
        self, report_id: int, verdict: str | None, *, officer: str | None, note: str | None = None
    ) -> int | None:
        officer = officer_name(officer)
        note = clean(note, label="Note", max_length=NOTE_MAX)
        if verdict not in TREE_VERDICTS:
            raise ReviewError("Choose whether a fallen tree is there or not.")
        with self._write() as session:
            report = self._report(session, report_id)
            session.add(
                OfficerAction(
                    kind=OFFICER_FALLEN_TREE,
                    report_id=report_id,
                    incident_id=report.incident_id,
                    officer=officer,
                    note=note,
                    detail={"verdict": verdict},
                )
            )
        return report.incident_id

    def set_reporter_blocked(
        self, report_id: int, blocked: bool, *, officer: str | None, note: str | None = None
    ) -> int | None:
        """Block or unblock the report's reporter with the CLI's own `set_blocked`."""
        officer = officer_name(officer)
        note = clean(note, label="Note", max_length=NOTE_MAX)
        with Session(self.engine, expire_on_commit=False) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            report = self._report(session, report_id)
            reporter = session.get(Reporter, report.reporter_id)
            session.add(
                OfficerAction(
                    kind=OFFICER_BLOCKED if blocked else OFFICER_UNBLOCKED,
                    report_id=report_id,
                    incident_id=report.incident_id,
                    reporter_id=reporter.id,
                    officer=officer,
                    note=note,
                )
            )
            try:
                set_blocked(session, reporter.pseudonym, blocked)  # commits the audit row too
            except ValueError as exc:
                raise ReviewError(str(exc)) from exc
        return report.incident_id

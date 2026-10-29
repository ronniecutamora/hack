"""Per-barangay evidence for officer outputs, counted without double counting.

Reports in one incident describe the same houses and people, so their numbers are never added
together. Per incident and per count, the latest correction replaces earlier figures; otherwise
(and for figures reported after that correction) the maximum stated value is used. Incident
values are then summed across the barangay's incidents. Every count keeps the incidents and
reports it came from, and how much of it rests on a single reporter or on flagged reports (high
or medium trust signals).

Reports whose grouping waits for officer review (an incident marked `needs_officer` after an
uncertain or failed pair judgment, a report with a grouping error, or a report never grouped)
may describe the same houses as each other or as a confirmed incident. Their values are never
added to a figure: they form a separate pending bucket, counted as if all of them were one
incident, and shown beside the confirmed figure.

A value is single-source when fewer than two different reporters stated a value at least as high
(after the latest correction): one reporter's 40 is not corroborated by others' 2.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from app.incidents import Observation, observations, rollup
from app.models import STATUS_ASSESSED, Incident, Report

COUNT_FIELDS = (
    "houses_totally_damaged",
    "houses_partially_damaged",
    "families_affected",
    "families_evacuated",
    "persons_evacuated",
    "dead",
    "injured",
    "missing",
    "fish_cages_or_pens_damaged",
)
PENDING_STATUSES = ("needs_officer", "ungrouped")
COUNTING_RULE = (
    "Counting rule: reports in one incident describe the same houses and people, so they are "
    "never added together. Per incident, the latest correction replaces earlier figures; "
    "otherwise, and for figures reported after that correction, the highest reported value is "
    "used. Incident values are then summed across the barangay's incidents. Reports waiting "
    "for grouping review (an uncertain or failed duplicate check, or not grouped yet) are "
    "never added: they are shown beside the figure as pending, counted as if they were one "
    "incident. Counts are reported figures for the RDANA team to verify, not verified damage."
)


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class IncidentEvidence:
    """One incident's counted reports (or one ungrouped report), oldest first."""

    incident_id: int | None
    barangay: str
    status: str  # open | needs_officer | ungrouped
    members: tuple[Observation, ...]
    grouping_error_report_ids: tuple[int, ...] = ()

    @property
    def pending(self) -> bool:
        """Grouping waits for the officer, so its reports may duplicate other incidents."""
        return self.status in PENDING_STATUSES or bool(self.grouping_error_report_ids)

    @property
    def key(self) -> str:
        if self.incident_id is not None:
            return f"incident {self.incident_id}"
        return f"report {self.report_ids[0]}"

    @property
    def report_ids(self) -> list[int]:
        return [m.id for m in self.members]

    @property
    def reporter_count(self) -> int:
        return len({m.reporter_id for m in self.members if m.reporter_id is not None})

    @property
    def flagged_report_ids(self) -> list[int]:
        return [m.id for m in self.members if m.flagged]

    def facts(self, member: Observation) -> dict[str, Any]:
        """Schema-validated facts only (the redacted context), never the raw stored dict."""
        return member.context["facts"]

    def depth(self) -> dict[str, str]:
        summary = rollup(list(self.members))
        return {"latest": summary["latest_depth"], "max": summary["max_depth"]}

    def place(self) -> str | None:
        """Latest redacted place detail (names of people removed)."""
        places = (self.facts(m).get("place_detail") for m in reversed(self.members))
        return next((p for p in places if p), None)

    def reports_listing(self, list_field: str, value: str) -> list[int]:
        return [m.id for m in self.members if value in (self.facts(m).get(list_field) or [])]

    def trust(self) -> dict[str, Any]:
        return {
            "reporter_count": self.reporter_count,
            "flagged_report_count": len(self.flagged_report_ids),
        }

    def fingerprint_data(self) -> dict[str, Any]:
        """What a draft built from this incident depends on (see BarangayEvidence.fingerprint)."""
        return {
            "incident": self.incident_id,
            "status": self.status,
            "reporters": self.reporter_count,
            "reports": [
                [m.id, m.at.isoformat(), m.point, m.flagged, self.facts(m)] for m in self.members
            ],
            # Only when present, so fingerprints of drafts made before this key still match.
            **(
                {"grouping_errors": list(self.grouping_error_report_ids)}
                if self.grouping_error_report_ids
                else {}
            ),
        }

    def fingerprint(self) -> str:
        return digest(self.fingerprint_data())


@dataclass(frozen=True)
class Contribution:
    """One incident's value for one count and the reports behind it."""

    incident: IncidentEvidence
    value: int
    rule: str  # maximum | latest correction | maximum since the latest correction
    source_report_id: int
    stating_report_ids: tuple[int, ...]
    flagged: bool  # the value rests only on reports with high/medium trust signals
    single_source: bool  # fewer than two reporters stated this value or more


@dataclass(frozen=True)
class Count:
    """A confirmed figure (summed over confirmed incidents) and the pending bucket beside it."""

    field: str
    contributions: tuple[Contribution, ...]  # confirmed incidents only
    pending: tuple[Contribution, ...] = ()  # per incident waiting for grouping review
    pending_value: int | None = None  # the counting rule over all pending reports as one

    @property
    def confirmed(self) -> bool:
        return bool(self.contributions)

    @property
    def value(self) -> int:
        return sum(c.value for c in self.contributions)

    @property
    def single_source_value(self) -> int:
        return sum(c.value for c in self.contributions if c.single_source)

    @property
    def flagged_value(self) -> int:
        return sum(c.value for c in self.contributions if c.flagged)

    @property
    def report_ids(self) -> list[int]:
        return sorted({r for c in self.contributions for r in c.stating_report_ids})

    @property
    def pending_report_ids(self) -> list[int]:
        return sorted({r for c in self.pending for r in c.stating_report_ids})


def incident_value(incident: IncidentEvidence, name: str) -> Contribution | None:
    """Apply the counting rule to one incident; None when no report states the value."""
    stated = [m for m in incident.members if incident.facts(m).get(name) is not None]
    if not stated:
        return None
    corrections = [i for i, m in enumerate(stated) if incident.facts(m).get("is_correction")]
    window = stated[corrections[-1] :] if corrections else stated
    value = max(incident.facts(m)[name] for m in window)
    backers = {
        m.reporter_id
        for m in window
        if incident.facts(m)[name] >= value and m.reporter_id is not None
    }
    # A tie with an unflagged report means the value does not rest on flagged evidence alone.
    source = max(
        (m for m in window if incident.facts(m)[name] == value),
        key=lambda m: (not m.flagged, m.at, m.id),
    )
    if not corrections:
        rule = "maximum"
    elif value == incident.facts(stated[corrections[-1]])[name]:
        rule = "latest correction"
    else:
        rule = "maximum since the latest correction"
    return Contribution(
        incident,
        value,
        rule,
        source.id,
        tuple(m.id for m in stated),
        source.flagged,
        len(backers) < 2,
    )


def count(incidents: list[IncidentEvidence], name: str) -> Count | None:
    """None when no report states the value; pending incidents never add to the figure."""
    confirmed = tuple(
        c for i in incidents if not i.pending and (c := incident_value(i, name)) is not None
    )
    waiting = [i for i in incidents if i.pending]
    pending = tuple(c for i in waiting if (c := incident_value(i, name)) is not None)
    if not confirmed and not pending:
        return None
    pooled = None
    if pending:
        members = sorted((m for i in waiting for m in i.members), key=lambda m: (m.at, m.id))
        pool = IncidentEvidence(None, waiting[0].barangay, "pending", tuple(members))
        pooled = incident_value(pool, name)
    return Count(name, confirmed, pending, pooled.value if pooled else None)


@dataclass
class BarangayEvidence:
    barangay: str
    incidents: list[IncidentEvidence]
    unassessed_report_ids: list[int] = field(default_factory=list)

    @property
    def report_ids(self) -> list[int]:
        return sorted(r for incident in self.incidents for r in incident.report_ids)

    @property
    def members(self) -> list[Observation]:
        return sorted(
            (m for incident in self.incidents for m in incident.members),
            key=lambda m: (m.at, m.id),
        )

    @property
    def reporter_count(self) -> int:
        return len({m.reporter_id for m in self.members if m.reporter_id is not None})

    def count(self, name: str) -> Count | None:
        return count(self.incidents, name)

    def pending_report_ids(self) -> list[int]:
        """Reports whose stated counts wait for grouping review, over every count."""
        return sorted(
            {r for name in COUNT_FIELDS if (c := self.count(name)) for r in c.pending_report_ids}
        )

    def fingerprint(self) -> str:
        """Changes whenever the counted evidence changes, so stale drafts can be flagged."""
        return digest([incident.fingerprint_data() for incident in self.incidents])


@dataclass
class CityEvidence:
    barangays: dict[str, BarangayEvidence]
    unplaced: list[IncidentEvidence]  # no barangay: the officer must place them first

    def incidents(self) -> list[IncidentEvidence]:
        """Every incident and ungrouped report, placed or not, oldest first."""
        everything = [i for b in self.barangays.values() for i in b.incidents] + self.unplaced
        return sorted(everything, key=lambda i: (i.members[0].at, i.members[0].id))

    def incident(self, incident_id: int) -> IncidentEvidence | None:
        return next((i for i in self.incidents() if i.incident_id == incident_id), None)


def collect(session: Session) -> CityEvidence:
    """Group every assessed report by incident and barangay in one pass over the database."""
    assessed = set(session.exec(select(Report.id).where(Report.status == STATUS_ASSESSED)).all())
    errors = set(
        session.exec(select(Report.id).where(col(Report.grouping_error).is_not(None))).all()
    )
    members: defaultdict[tuple[str, int], list[Observation]] = defaultdict(list)
    for observation in observations(session).values():
        if observation.id not in assessed:
            continue
        if observation.incident_id is not None:
            members[("incident", observation.incident_id)].append(observation)
        else:
            members[("report", observation.id)].append(observation)
    incident_ids = [key[1] for key in members if key[0] == "incident"]
    incidents = {
        i.id: i
        for i in session.exec(select(Incident).where(col(Incident.id).in_(incident_ids))).all()
    }
    by_barangay: defaultdict[str, list[IncidentEvidence]] = defaultdict(list)
    unplaced = []
    for (kind, ident), rows in members.items():
        rows.sort(key=lambda m: (m.at, m.id))
        failed = tuple(m.id for m in rows if m.id in errors)
        if kind == "incident" and ident in incidents:
            row = incidents[ident]
            evidence = IncidentEvidence(ident, row.barangay or "", row.status, tuple(rows), failed)
        else:
            barangay = rows[0].barangay or ""
            evidence = IncidentEvidence(None, barangay, "ungrouped", tuple(rows), failed)
        if evidence.barangay:
            by_barangay[evidence.barangay].append(evidence)
        else:
            unplaced.append(evidence)
    pending: defaultdict[str, list[int]] = defaultdict(list)
    for report_id, barangay in session.exec(
        select(Report.id, Report.barangay).where(
            Report.status != STATUS_ASSESSED, col(Report.barangay).is_not(None)
        )
    ).all():
        pending[barangay].append(report_id)
    barangays = {
        name: BarangayEvidence(
            name,
            sorted(items, key=lambda i: (i.members[0].at, i.members[0].id)),
            sorted(pending.get(name, [])),
        )
        for name, items in sorted(by_barangay.items())
    }
    return CityEvidence(barangays, sorted(unplaced, key=lambda i: i.members[0].id))

"""Per-barangay evidence for officer outputs, counted without double counting.

Reports in one incident describe the same houses and people, so their numbers are never added
together. Per incident and per count, the latest correction replaces earlier figures; otherwise
(and for figures reported after that correction) the maximum stated value is used. Incident
values are then summed across the barangay's incidents. A report not linked to any incident
counts as an incident of its own. Every count keeps the incidents and reports it came from, and
how much of it rests on a single reporter or on reports with high/medium trust signals.

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
COUNTING_RULE = (
    "Counting rule: reports in one incident describe the same houses and people, so they are "
    "never added together. Per incident, the latest correction replaces earlier figures; "
    "otherwise, and for figures reported after that correction, the highest reported value is "
    "used. Incident values are then summed across the barangay's incidents. A report not "
    "linked to an incident counts as its own incident. Counts are reported figures for the "
    "RDANA team to verify, not verified damage."
)


@dataclass(frozen=True)
class IncidentEvidence:
    """One incident's counted reports (or one ungrouped report), oldest first."""

    incident_id: int | None
    barangay: str
    status: str
    members: tuple[Observation, ...]

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
    field: str
    contributions: tuple[Contribution, ...]

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
    contributions = tuple(
        c for incident in incidents if (c := incident_value(incident, name)) is not None
    )
    return Count(name, contributions) if contributions else None


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

    def count(self, name: str) -> Count | None:
        return count(self.incidents, name)

    def fingerprint(self) -> str:
        """Changes whenever the counted evidence changes, so stale drafts can be flagged."""
        data = [
            {
                "incident": incident.incident_id,
                "status": incident.status,
                "reporters": incident.reporter_count,
                "reports": [
                    [m.id, m.at.isoformat(), m.point, m.flagged, incident.facts(m)]
                    for m in incident.members
                ],
            }
            for incident in self.incidents
        ]
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class CityEvidence:
    barangays: dict[str, BarangayEvidence]
    unplaced: list[IncidentEvidence]  # no barangay: the officer must place them first


def collect(session: Session) -> CityEvidence:
    """Group every assessed report by incident and barangay in one pass over the database."""
    assessed = set(session.exec(select(Report.id).where(Report.status == STATUS_ASSESSED)).all())
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
        if kind == "incident" and ident in incidents:
            row = incidents[ident]
            evidence = IncidentEvidence(ident, row.barangay or "", row.status, tuple(rows))
        else:
            evidence = IncidentEvidence(None, rows[0].barangay or "", "ungrouped", tuple(rows))
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

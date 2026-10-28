"""Group assessed reports with deterministic candidates, a pair judge and a stored audit.

    uv run python -m app.incidents --all
    uv run python -m app.incidents --report 12

No transaction spans a model call. In-process grouping is serialized; a decision revision
check under SQLite BEGIN IMMEDIATE retries a stale plan from a second grouping process.
"""

import argparse
import asyncio
import hashlib
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.meta.factory import LLM_BACKENDS, make_pair_judge
from app.meta.llm import AgentLLM
from app.models import (
    STATUS_ASSESSED,
    AgentDecision,
    Assessment,
    Incident,
    PhotoAssessment,
    Report,
)
from app.pair_judge import judge_pair, redacted_place, report_context

GPS_DISTANCE_CAP_M = 500.0
RECENT_REPORT_LIMIT = 3
DEPTH_ORDER = ("none", "ankle", "knee", "waist", "chest", "above_head")
TOTAL_FIELDS = (
    "houses_totally_damaged",
    "houses_partially_damaged",
    "houses_flooded",
    "families_affected",
    "families_evacuated",
)
ROLLUP_CAVEAT = (
    "Reported totals may double-count households across reports, including corrections. "
    "These are evidence, not verified damage or insurance loss figures."
)


@dataclass
class Coordination:
    reports: defaultdict[int, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )
    grouping: asyncio.Lock = field(default_factory=asyncio.Lock)


_COORDINATION: WeakKeyDictionary = WeakKeyDictionary()


def coordination(engine: Engine) -> Coordination:
    if engine not in _COORDINATION:
        _COORDINATION[engine] = Coordination()
    return _COORDINATION[engine]


@dataclass(frozen=True)
class Observation:
    id: int
    incident_id: int | None
    at: datetime
    barangay: str | None
    point: tuple[float, float] | None
    facts: dict[str, Any]
    context: dict[str, Any]


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(min(1.0, h)))


def candidate_incidents(
    new: Observation, groups: dict[int, list[Observation]], *, window_days: int
) -> list[int]:
    """Same barangay within the activity window; close GPS can add missing-barangay pairs.

    Two known but different barangays never match. Where both sides have a point, distance
    vetoes the candidate. Order is nearest first, then newest activity, then stable id.
    """
    if not new.barangay and new.point is None:
        return []
    candidates = []
    for incident_id, members in groups.items():
        members = [m for m in members if m.id != new.id]
        if not members:
            continue
        latest = max(m.at for m in members)
        if abs(new.at - latest) > timedelta(days=window_days):
            continue
        barangay = next((m.barangay for m in reversed(members) if m.barangay), None)
        if new.barangay and barangay and new.barangay != barangay:
            continue
        distances = (
            [distance_m(new.point, m.point) for m in members if m.point is not None]
            if new.point is not None
            else []
        )
        nearest = min(distances) if distances else None
        if nearest is not None and nearest > GPS_DISTANCE_CAP_M:
            continue
        same_barangay = new.barangay is not None and new.barangay == barangay
        if not same_barangay and nearest is None:
            continue
        candidates.append(
            (nearest if nearest is not None else math.inf, -latest.timestamp(), incident_id)
        )
    return [item[2] for item in sorted(candidates)]


def rollup(members: list[Observation]) -> dict[str, Any]:
    """Depth maxima are ordinal, latest depth may be unknown; totals are explicitly unverified."""
    ordered = sorted(members, key=lambda m: (m.at, m.id))
    depths = [m.facts.get("flood_depth", "unknown") for m in ordered]
    known = [d for d in depths if d in DEPTH_ORDER]
    totals = {}
    for key in TOTAL_FIELDS:
        values = [m.facts.get(key) for m in ordered if m.facts.get(key) is not None]
        totals[key] = sum(values) if values else None
    return {
        "report_count": len(ordered),
        "latest_depth": depths[-1] if depths else "unknown",
        "max_depth": max(known, key=DEPTH_ORDER.index) if known else "unknown",
        "totals": totals,
        "caveat": ROLLUP_CAVEAT,
    }


def incident_title(member: Observation) -> str:
    hazards = member.facts.get("hazards") or ["damage"]
    place = redacted_place(member.facts.get("place_detail"))
    return " · ".join(
        part
        for part in (member.barangay or "Location unknown", hazards[0].replace("_", " "), place)
        if part
    )


def observations(session: Session) -> dict[int, Observation]:
    assessments = {a.report_id: a for a in session.exec(select(Assessment)).all()}
    photos: defaultdict[int, list[PhotoAssessment]] = defaultdict(list)
    for photo in session.exec(select(PhotoAssessment).order_by(PhotoAssessment.id)).all():
        photos[photo.report_id].append(photo)
    result = {}
    for report in session.exec(select(Report).order_by(Report.received_at, Report.id)).all():
        if report.id not in assessments:
            continue
        facts = assessments[report.id].facts or {}
        point = None
        if report.lat is not None and report.lon is not None:
            point = (report.lat, report.lon)
        else:
            point = next(
                (
                    (p.gps_lat, p.gps_lon)
                    for p in photos[report.id]
                    if p.gps_lat is not None and p.gps_lon is not None
                ),
                None,
            )
        context = report_context(
            barangay=report.barangay,
            facts=facts,
            judgments=[p.judgment for p in photos[report.id] if p.judgment],
        )
        context["observed_at"] = report.received_at.isoformat()
        result[report.id] = Observation(
            report.id,
            report.incident_id,
            report.received_at,
            report.barangay,
            point,
            facts,
            context,
        )
    return result


def revision(session: Session) -> int:
    return session.exec(select(func.max(AgentDecision.id))).one() or 0


class IncidentService:
    def __init__(self, settings: Settings, engine: Engine, judge: AgentLLM) -> None:
        self.settings, self.engine, self.judge = settings, engine, judge
        self.locks = coordination(engine)

    def fingerprint(self, observation: Observation) -> str:
        data = {
            "version": 1,
            "context": observation.context,
            "point": observation.point,
            "judge": self.judge.model,
            "window_days": self.settings.incident_window_days,
            "threshold": self.settings.pair_merge_threshold,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    async def group(self, report_id: int, *, retry_failed: bool = False) -> Report | None:
        async with self.locks.reports[report_id]:
            return await self.group_locked(report_id, retry_failed=retry_failed)

    async def group_locked(self, report_id: int, *, retry_failed: bool = False) -> Report | None:
        """The assessor already owns the same per-report lock when calling this method."""
        async with self.locks.grouping:
            for _attempt in range(5):
                with Session(self.engine, expire_on_commit=False) as session:
                    report = session.get(Report, report_id)
                    if report is None or report.status != STATUS_ASSESSED:
                        return report
                    all_members = observations(session)
                    new = all_members.get(report_id)
                    if new is None:
                        return report
                    fingerprint = self.fingerprint(new)
                    if report.grouping_fingerprint == fingerprint and not (
                        retry_failed and report.grouping_error
                    ):
                        return report
                    epoch = revision(session)
                    groups: defaultdict[int, list[Observation]] = defaultdict(list)
                    for member in all_members.values():
                        if member.incident_id is not None:
                            groups[member.incident_id].append(member)
                    candidates = candidate_incidents(
                        new, groups, window_days=self.settings.incident_window_days
                    )
                # Every session is closed before asking the judge.
                comparisons = []
                chosen = None
                needs_officer = not new.barangay and new.point is None
                error = None
                for incident_id in candidates:
                    recent = [m for m in groups[incident_id] if m.id != report_id][
                        -RECENT_REPORT_LIMIT:
                    ]
                    try:
                        judgment = await judge_pair(
                            self.judge, [m.context for m in recent], new.context
                        )
                    except Exception as exc:
                        # Preserve the actual provider's availability error, without a fallback.
                        error = f"{type(exc).__name__}: {exc}"
                        judgment = {
                            "decision": "unsure",
                            "confidence": None,
                            "reason": f"Pair judge unavailable: {error}",
                        }
                    comparisons.append((incident_id, recent[-1].id, judgment))
                    if judgment["decision"] == "same" and (
                        judgment["confidence"] >= self.settings.pair_merge_threshold
                    ):
                        chosen = incident_id
                        break
                    if judgment["decision"] == "unsure" or (
                        judgment["confidence"] < self.settings.pair_merge_threshold
                    ):
                        needs_officer = True
                        break
                out = self._apply(
                    new, fingerprint, epoch, chosen, needs_officer, comparisons, error
                )
                if out is not None:
                    return out
            raise RuntimeError("Incident inputs changed repeatedly; rerun grouping.")

    def _apply(self, new, fingerprint, epoch, chosen, needs_officer, comparisons, error):
        with Session(self.engine, expire_on_commit=False) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            report = session.get(Report, new.id)
            current = observations(session)
            if (
                revision(session) != epoch
                or report.status != STATUS_ASSESSED
                or self.fingerprint(current[new.id]) != fingerprint
            ):
                session.rollback()
                return None
            old_id = report.incident_id
            old = session.get(Incident, old_id) if old_id is not None else None
            old_barangay = old.barangay if old else None
            others = [m for m in current.values() if m.incident_id == old_id and m.id != new.id]
            target = session.get(Incident, chosen) if chosen is not None else None
            if target is None:
                # A singleton changes place in-place: no empty duplicate after a late pin.
                target = (
                    old
                    if old is not None and not others
                    else Incident(
                        title=incident_title(new),
                        barangay=new.barangay,
                        first_seen=new.at,
                        last_seen=new.at,
                    )
                )
                target.status = "needs_officer" if needs_officer else "open"
                session.add(target)
                session.flush()
            report.incident_id = target.id
            report.grouping_fingerprint = fingerprint
            report.grouping_error = error
            report.grouping_needs_officer = needs_officer
            session.add(report)
            session.flush()
            for compared_id, compared_report, judgment in comparisons:
                merged = chosen == compared_id
                uncertain = judgment["decision"] == "unsure" or (
                    judgment["confidence"] < self.settings.pair_merge_threshold
                )
                session.add(
                    AgentDecision(
                        kind="merged"
                        if merged
                        else "needs_officer"
                        if uncertain
                        else "kept_separate",
                        report_id=new.id,
                        incident_id=target.id,
                        compared_incident_id=compared_id,
                        compared_report_id=compared_report,
                        judge_model=self.judge.model,
                        **judgment,
                    )
                )
            if chosen is None:
                session.add(
                    AgentDecision(
                        kind="needs_officer" if needs_officer else "new_incident",
                        report_id=new.id,
                        incident_id=target.id,
                        decision="separate",
                        reason=(
                            "No barangay or GPS point; officer must establish the place."
                            if not new.barangay and new.point is None
                            else "Pair judgment requires officer review; no merge was made."
                            if needs_officer
                            else "No candidate was accepted within the place and activity filters."
                        ),
                    )
                )
            if old is not None and (old_id != target.id or old_barangay != new.barangay):
                session.add(
                    AgentDecision(
                        kind="re-linked",
                        report_id=new.id,
                        incident_id=target.id,
                        compared_incident_id=old_id,
                        decision="re-linked",
                        reason=f"Reassessment changed the grouping from incident {old_id} "
                        f"({old_barangay or 'unknown place'}) to {target.id} "
                        f"({new.barangay or 'unknown place'}).",
                    )
                )
            session.flush()
            refreshed = observations(session)
            for incident_id in {old_id, target.id} - {None}:
                incident = session.get(Incident, incident_id)
                members = [m for m in refreshed.values() if m.incident_id == incident_id]
                incident.active = bool(members)
                incident.latest_facts = rollup(members)
                if members:
                    incident.barangay = next(
                        (m.barangay for m in reversed(members) if m.barangay), None
                    )
                    incident.title = incident_title(
                        replace(members[-1], barangay=incident.barangay)
                    )
                    review = session.exec(
                        select(Report.id).where(
                            Report.incident_id == incident_id,
                            Report.grouping_needs_officer == True,  # noqa: E712
                        )
                    ).first()
                    incident.status = "needs_officer" if review is not None else "open"
                    incident.first_seen = min(m.at for m in members)
                    incident.last_seen = max(m.at for m in members)
                session.add(incident)
            session.commit()
            return report

    def report_ids(self) -> list[int]:
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(Report.id)
                    .where(Report.status == STATUS_ASSESSED)
                    .order_by(Report.received_at, Report.id)
                ).all()
            )


async def _cli(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.judge:
        settings = settings.model_copy(update={"pair_judge_llm": args.judge})
    engine = make_engine(settings)
    init_db(engine)
    judge = make_pair_judge(settings, make_recorder(engine))
    service = IncidentService(settings, engine, judge)
    failed = False
    try:
        ids = [args.report] if args.report is not None else service.report_ids()
        print(f"grouping {len(ids)} report(s), judge {judge.model}; cached calls are reused")
        for report_id in ids:
            report = await service.group(report_id, retry_failed=args.retry_failed)
            if report is None:
                print(f"report {report_id}: not found")
                failed = True
            elif report.status != STATUS_ASSESSED:
                print(f"report {report_id}: skipped ({report.status})")
            else:
                print(f"report {report_id}: incident {report.incident_id}")
                if report.grouping_error:
                    failed = True
                    print(f"  needs officer: {report.grouping_error}")
    finally:
        await judge.aclose()
        engine.dispose()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Group assessed reports into incidents.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true")
    which.add_argument("--report", type=int)
    parser.add_argument("--judge", choices=list(LLM_BACKENDS))
    parser.add_argument("--retry-failed", action="store_true", help="retry a stored judge failure")
    return asyncio.run(_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

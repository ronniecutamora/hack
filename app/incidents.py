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

from sqlalchemy import Engine, func, or_
from sqlmodel import Session, col, select

from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.facts import FACT_COUNT_FIELDS, LIVELIHOOD_DAMAGE
from app.meta.factory import LLM_BACKENDS, make_pair_judge
from app.meta.llm import AgentLLM
from app.models import (
    OFFICER_FALLEN_TREE,
    OFFICER_SIGNAL_REVIEWED,
    STATUS_ASSESSED,
    AgentDecision,
    Assessment,
    Incident,
    OfficerAction,
    PhotoAssessment,
    Report,
)
from app.pair_judge import judge_pair, redacted_place, report_context
from app.trace import Step, traced
from app.trust import flagging, signal_key

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
    reporter_id: int | None = None
    flagged: bool = False
    # (photo assessment id, media id, overlay media id, updated_at) per photo, for staleness of
    # outputs that show the photos (the Notice of Loss annex); grouping and counting ignore it.
    photos: tuple[tuple[int | None, int, int | None, str], ...] = ()
    # An officer's verdict on a fallen tree (the models cannot see them reliably): True
    # confirmed, False not there, None not reviewed. Counting applies it to the hazards.
    fallen_tree: bool | None = None
    corrects: int | None = None  # the report this one corrects (a reply to its summary)


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


def review_report_ids(session: Session) -> set[int]:
    """Reports whose grouping waits for the officer (an uncertain or failed pair judgment)."""
    return set(
        session.exec(
            select(Report.id).where(
                or_(
                    Report.grouping_needs_officer == True,  # noqa: E712
                    col(Report.grouping_error).is_not(None),
                )
            )
        ).all()
    )


@dataclass(frozen=True)
class CandidateFit:
    """What makes one candidate a better home than another when the judge accepts both."""

    incident_id: int
    pending: bool  # another member's grouping waits for the officer
    corrected: tuple[str, ...] = ()  # count fields a correction restates that members state


def candidate_fit(
    new: Observation, incident_id: int, members: list[Observation], review: set[int]
) -> CandidateFit:
    others = [m for m in members if m.id != new.id]
    corrected: tuple[str, ...] = ()
    if new.facts.get("is_correction"):
        stated = {n for m in others for n in FACT_COUNT_FIELDS if m.facts.get(n) is not None}
        corrected = tuple(
            n for n in FACT_COUNT_FIELDS if new.facts.get(n) is not None and n in stated
        )
    return CandidateFit(incident_id, any(m.id in review for m in others), corrected)


@dataclass(frozen=True)
class OwnCorrection:
    """A correction routed by rule to the incident holding its reporter's own earlier figure."""

    incident_id: int
    report_id: int  # the reporter's earlier report the correction restates
    fields: tuple[str, ...]  # count fields both reports state
    replied: bool = False  # the reporter replied to the bot's summary of that report

    def reason(self) -> str:
        if self.replied:
            return (
                f"Correction by the same reporter, sent as a reply to the bot's summary of "
                f"report {self.report_id}; routed by rule, no model call."
            )
        names = ", ".join(n.replace("_", " ") for n in self.fields)
        plural = "s" if len(self.fields) > 1 else ""
        return (
            f"Correction by the same reporter of the {names} figure{plural} in report "
            f"{self.report_id}; routed by rule, no model call."
        )


def own_correction(
    new: Observation, groups: dict[int, list[Observation]], candidates: list[int]
) -> OwnCorrection | None:
    """Rule B: a correction joins the candidate incident where the same pseudonymous reporter
    stated a figure it corrects, before any judge call.

    The live test (2026-10-29) showed why: "mali po yung sinabi ko kanina: dalawang bahay lang"
    was judged only "unsure" against the reporter's own voice note (no purok detail in the
    correction) and "same" against an unrelated old-photo incident, and merged there. Only
    candidates (same place, inside the activity window) qualify, only earlier reports count, and
    when several do, the most recent wins.
    """
    if new.reporter_id is None:
        return None
    if new.corrects is not None:
        # A reply to the bot's summary of one report names it, whatever the wording, and often
        # without a place ("dalawa lang po"): it joins that report's incident unless it names
        # a different barangay, which the place filters then handle like any report.
        for incident_id, members in groups.items():
            target = next(
                (m for m in members if m.id == new.corrects and m.reporter_id == new.reporter_id),
                None,
            )
            if target is None:
                continue
            place = next((m.barangay for m in reversed(members) if m.barangay), None)
            if new.barangay is None or place is None or new.barangay == place:
                return OwnCorrection(incident_id, target.id, (), replied=True)
    if not new.facts.get("is_correction"):
        return None
    restated = [n for n in FACT_COUNT_FIELDS if new.facts.get(n) is not None]
    best: tuple[tuple[datetime, int], OwnCorrection] | None = None
    for incident_id in candidates:
        for member in groups[incident_id]:
            if (
                member.id == new.id
                or member.reporter_id != new.reporter_id
                or (member.at, member.id) > (new.at, new.id)
            ):
                continue
            fields = tuple(n for n in restated if member.facts.get(n) is not None)
            key = (member.at, member.id)
            if fields and (best is None or key > best[0]):
                best = (key, OwnCorrection(incident_id, member.id, fields))
    return best[1] if best else None


def choose(
    accepted: list[tuple[CandidateFit, dict[str, Any]]], *, current: int | None
) -> tuple[int, str | None]:
    """Pick the best accepted match and say why it beat the runner-up (None if unopposed).

    Preference: an incident not waiting for review; for a correction, the incident whose
    members state the corrected figures; higher confidence; the report's current incident
    (no churn on re-grouping); then the candidate order (nearest, then newest).
    """
    ranked = sorted(
        enumerate(accepted),
        key=lambda item: (
            item[1][0].pending,
            -len(item[1][0].corrected),
            -item[1][1]["confidence"],
            item[1][0].incident_id != current,
            item[0],
        ),
    )
    best, best_judgment = ranked[0][1]
    if len(ranked) == 1:
        return best.incident_id, None
    runner, runner_judgment = ranked[1][1]
    reasons = []
    if len(best.corrected) > len(runner.corrected):
        fields = ", ".join(n.replace("_", " ") for n in best.corrected)
        plural = "s" if len(best.corrected) > 1 else ""
        reasons.append(
            f"it corrects the {fields} figure{plural} stated in incident {best.incident_id}"
        )
    if runner.pending and not best.pending:
        reasons.append(f"incident {runner.incident_id} waits for officer review")
    if not reasons:
        if best_judgment["confidence"] > runner_judgment["confidence"]:
            reasons.append(
                f"higher confidence ({best_judgment['confidence']:.2f} "
                f"vs {runner_judgment['confidence']:.2f})"
            )
        elif best.incident_id == current:
            reasons.append("the report was already grouped there")
        else:
            reasons.append("nearest place, then most recent activity")
    others = ", ".join(str(fit.incident_id) for _, (fit, _) in ranked[1:])
    plural = "s" if len(ranked) > 2 else ""
    return best.incident_id, (
        f"Chosen over incident{plural} {others}, also judged the same: {'; '.join(reasons)}."
    )


def rollup(members: list[Observation]) -> dict[str, Any]:
    """Depth maxima are ordinal, latest depth may be unknown; totals are explicitly unverified."""
    ordered = sorted(members, key=lambda m: (m.at, m.id))
    depths = [m.facts.get("flood_depth", "unknown") for m in ordered]
    known = [d for d in depths if d in DEPTH_ORDER]
    totals = {}
    for key in TOTAL_FIELDS:
        values = [m.facts.get(key) for m in ordered if m.facts.get(key) is not None]
        totals[key] = sum(values) if values else None
    # Kinds of livelihood damage any report states, in first-reported order (no counts).
    livelihood = [
        value
        for m in ordered
        for value in (m.facts.get("livelihood_damage") or [])
        if value in LIVELIHOOD_DAMAGE
    ]
    return {
        "report_count": len(ordered),
        "reporter_count": len({m.reporter_id for m in ordered if m.reporter_id is not None}),
        "flagged_report_count": sum(m.flagged for m in ordered),
        "latest_depth": depths[-1] if depths else "unknown",
        "max_depth": max(known, key=DEPTH_ORDER.index) if known else "unknown",
        "totals": totals,
        "livelihood_damage": list(dict.fromkeys(livelihood)),
        "caveat": ROLLUP_CAVEAT,
    }


def counted_facts(member: Observation) -> dict[str, Any]:
    """Schema-validated facts only (the redacted context), never the raw stored dict, with an
    officer's fallen-tree verdict applied to the hazards (the models cannot see trees)."""
    facts = member.context["facts"]
    if member.fallen_tree is None:
        return facts
    hazards = [h for h in facts.get("hazards") or [] if h != "fallen_tree"]
    if member.fallen_tree:
        hazards.append("fallen_tree")
    return {**facts, "hazards": hazards}


def incident_title(member: Observation) -> str:
    """Barangay, first hazard and place, from the facts as the officer adjusted them."""
    hazards = counted_facts(member).get("hazards") or ["damage"]
    place = redacted_place(member.facts.get("place_detail"))
    return " · ".join(
        part
        for part in (member.barangay or "Location unknown", hazards[0].replace("_", " "), place)
        if part
    )


def officer_reviews(session: Session) -> tuple[dict[tuple[int, str], str], dict[int, bool]]:
    """The latest officer verdict per (report, trust signal key), and per report on whether a
    fallen tree is there. The OfficerAction log is the only store of these reviews."""
    signals: dict[tuple[int, str], str] = {}
    trees: dict[int, bool] = {}
    for action in session.exec(
        select(OfficerAction)
        .where(col(OfficerAction.kind).in_((OFFICER_SIGNAL_REVIEWED, OFFICER_FALLEN_TREE)))
        .order_by(OfficerAction.id)
    ).all():
        detail = action.detail or {}
        if action.kind == OFFICER_SIGNAL_REVIEWED:
            signals[(action.report_id, detail.get("key"))] = detail.get("verdict")
        else:
            trees[action.report_id] = detail.get("verdict") == "confirmed"
    return signals, trees


def is_flagged(report_id: int, signals: list[dict] | None, reviews: dict) -> bool:
    """A high or medium trust signal the officer has not reviewed as genuine."""
    return any(
        flagging(s) and reviews.get((report_id, signal_key(s))) != "genuine" for s in signals or []
    )


def observations(session: Session) -> dict[int, Observation]:
    signal_reviews, tree_reviews = officer_reviews(session)
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
            report.reporter_id,
            is_flagged(report.id, assessments[report.id].signals, signal_reviews),
            tuple(
                (p.id, p.media_id, p.overlay_media_id, p.updated_at.isoformat())
                for p in photos[report.id]
            ),
            tree_reviews.get(report.id),
            report.corrects_report_id,
        )
    return result


def revision(session: Session) -> tuple[int, int]:
    """Changes with every grouping decision and every officer action, so a grouping plan made
    before either is re-planned instead of applied."""
    return (
        session.exec(select(func.max(AgentDecision.id))).one() or 0,
        session.exec(select(func.max(OfficerAction.id))).one() or 0,
    )


def refresh_incidents(session: Session, incident_ids: set[int | None]) -> None:
    """Recompute rollup, place, title, status and activity of incidents whose members changed."""
    refreshed = observations(session)
    for incident_id in incident_ids - {None}:
        incident = session.get(Incident, incident_id)
        members = [m for m in refreshed.values() if m.incident_id == incident_id]
        incident.active = bool(members)
        incident.latest_facts = rollup(members)
        if members:
            incident.barangay = next((m.barangay for m in reversed(members) if m.barangay), None)
            incident.title = incident_title(replace(members[-1], barangay=incident.barangay))
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


class IncidentService:
    def __init__(self, settings: Settings, engine: Engine, judge: AgentLLM) -> None:
        self.settings, self.engine, self.judge = settings, engine, judge
        self.locks = coordination(engine)

    def fingerprint(self, observation: Observation) -> str:
        data = {
            # 2: every candidate up to the cap is judged and the best accepted match wins.
            # 3: a correction joins its reporter's own earlier figure by rule (own_correction).
            "version": 3,
            "context": observation.context,
            "point": observation.point,
            "judge": self.judge.model,
            "window_days": self.settings.incident_window_days,
            "threshold": self.settings.pair_merge_threshold,
            "max_judged": self.settings.max_judged_candidates,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    async def group(self, report_id: int, *, retry_failed: bool = False) -> Report | None:
        async with self.locks.reports[report_id]:
            return await self.group_locked(report_id, retry_failed=retry_failed)

    async def group_locked(self, report_id: int, *, retry_failed: bool = False) -> Report | None:
        """The assessor already owns the same per-report lock when calling this method."""
        # Pair judgments are logged against this report, from any caller (app.trace).
        with traced(report_id=report_id):
            return await self._group_locked(report_id, retry_failed=retry_failed)

    async def _group_locked(self, report_id: int, *, retry_failed: bool) -> Report | None:
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
                    # An officer's merge or keep-separate sticks: no judge, no move.
                    pinned = report.officer_incident_id
                    if pinned is not None and session.get(Incident, pinned) is None:
                        pinned = None
                    groups: defaultdict[int, list[Observation]] = defaultdict(list)
                    for member in all_members.values():
                        if member.incident_id is not None:
                            groups[member.incident_id].append(member)
                    review = review_report_ids(session)
                    candidates = candidate_incidents(
                        new, groups, window_days=self.settings.incident_window_days
                    )
                if pinned is not None:
                    out = self._apply(new, fingerprint, epoch, pinned, False, [], None)
                    if out is not None:
                        return out
                    continue
                rule = own_correction(new, groups, candidates)
                if rule is not None:
                    # Deterministic and certain: no judge call is needed or paid for.
                    decision = {"decision": "same", "confidence": None, "reason": rule.reason()}
                    comparisons = [(rule.incident_id, rule.report_id, decision, None)]
                    out = self._apply(
                        new, fingerprint, epoch, rule.incident_id, False, comparisons, None
                    )
                    if out is not None:
                        return out
                    continue
                fits = [candidate_fit(new, i, groups[i], review) for i in candidates]
                # Stable sort: within equal fit the nearest, newest order stays.
                fits.sort(key=lambda f: (f.pending, -len(f.corrected)))
                fits = fits[: self.settings.max_judged_candidates]
                # Every session is closed before asking the judge. Every candidate up to the cap
                # is judged; first-match-wins once merged a correction into the wrong incident.
                comparisons = []
                accepted = []
                error = None
                for fit in fits:
                    recent = [m for m in groups[fit.incident_id] if m.id != report_id][
                        -RECENT_REPORT_LIMIT:
                    ]
                    try:
                        with traced(step=Step.PAIR_JUDGE, subject=f"incident:{fit.incident_id}"):
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
                    comparisons.append((fit.incident_id, recent[-1].id, judgment, self.judge.model))
                    if error:
                        # The provider is down, and a missing comparison may hide the best
                        # match: nothing merges; the officer sees it and can retry.
                        accepted = []
                        break
                    if self._accepted(judgment):
                        accepted.append((fit, judgment))
                chosen = None
                if accepted:
                    chosen, why = choose(accepted, current=new.incident_id)
                    if why:
                        comparisons = [
                            (i, r, {**j, "reason": f"{j['reason']} {why}"} if i == chosen else j, m)
                            for i, r, j, m in comparisons
                        ]
                needs_officer = (not new.barangay and new.point is None) or (
                    chosen is None and any(self._uncertain(j) for _, _, j, _ in comparisons)
                )
                out = self._apply(
                    new, fingerprint, epoch, chosen, needs_officer, comparisons, error
                )
                if out is not None:
                    return out
            raise RuntimeError("Incident inputs changed repeatedly; rerun grouping.")

    def _accepted(self, judgment: dict[str, Any]) -> bool:
        return (
            judgment["decision"] == "same"
            and judgment["confidence"] >= self.settings.pair_merge_threshold
        )

    def _uncertain(self, judgment: dict[str, Any]) -> bool:
        return judgment["decision"] == "unsure" or (
            judgment["decision"] == "same" and not self._accepted(judgment)
        )

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
            for compared_id, compared_report, judgment, model in comparisons:
                # An uncertain comparison needs the officer only when nothing else matched.
                session.add(
                    AgentDecision(
                        kind="merged"
                        if chosen == compared_id
                        else "needs_officer"
                        if chosen is None and self._uncertain(judgment)
                        else "kept_separate",
                        report_id=new.id,
                        incident_id=target.id,
                        compared_incident_id=compared_id,
                        compared_report_id=compared_report,
                        judge_model=model,  # None: a deterministic rule decided
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
            refresh_incidents(session, {old_id, target.id})
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

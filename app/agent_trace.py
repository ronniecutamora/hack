"""Read models over the call log: each report's agent trace and the model calls behind a draft.

A report's trace lists what the agent did for it, in pipeline order: every Meta model call
(model, latency, cache hit, cost) next to the decision it produced, the deterministic steps
labelled as rules, and then every officer action on the report, labelled as a human decision.
Outputs are the derived facts the dashboard already shows; never a prompt, a raw reply or a
reporter's words. Totals count every logged call, including repeats from a re-assessment.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import case
from sqlmodel import Session, col, func, select

from app.facts import fact_lines
from app.geo import GEO_SOURCES
from app.meta.sam import MODEL as SAM_MODEL
from app.models import (
    AgentDecision,
    Assessment,
    ModelCall,
    OfficerAction,
    PhotoAssessment,
    Report,
)
from app.review import ACTION_LABELS, SIGNAL_VERDICTS, TREE_VERDICTS
from app.trace import STEP_ORDER, STEPS, ModelFamily, Step, model_family

TRACE_SINCE = "2026-10-30"  # the day calls started to be linked to reports
FACT_LINES_SHOWN = 3


@dataclass(frozen=True)
class CallStats:
    """The logged calls of one step (several when a report was assessed again, or a long voice
    note was sent in parts); latency and cache state are the latest call's."""

    model: str
    family: ModelFamily | None
    latency_ms: int
    cached: bool
    cost: float
    calls: int
    cached_calls: int

    @classmethod
    def of(cls, calls: list[ModelCall]) -> "CallStats":
        latest = calls[-1]
        return cls(
            model=latest.model,
            family=model_family(latest.model),
            latency_ms=latest.latency_ms,
            cached=latest.cached,
            cost=sum(c.cost_estimate for c in calls),
            calls=len(calls),
            cached_calls=sum(1 for c in calls if c.cached),
        )


@dataclass(frozen=True)
class TraceStep:
    label: str
    actor: Literal["model", "rule", "officer"]
    output: str
    stats: CallStats | None = None
    model: str | None = None  # the model the stored result names, for a call that was not logged
    detail: str | None = None  # the longer explanation, shown on hover
    officer: str | None = None
    at: datetime | None = None

    @property
    def family(self) -> ModelFamily | None:
        return self.stats.family if self.stats else model_family(self.model)


@dataclass
class Trace:
    steps: list[TraceStep] = field(default_factory=list)
    calls: int = 0
    cached_calls: int = 0
    cost: float = 0.0
    model_ms: int = 0  # summed call latencies; calls inside a step can overlap
    recorded: bool = True  # False: the agent ran before calls were linked to reports

    @property
    def agent_steps(self) -> int:
        return sum(1 for s in self.steps if s.actor != "officer")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _subject_id(subject: str | None, kind: str) -> int | None:
    prefix = f"{kind}:"
    if subject and subject.startswith(prefix) and subject[len(prefix) :].isdigit():
        return int(subject[len(prefix) :])
    return None


def _judgment_output(judgment: dict[str, Any] | None, error: str | None) -> str:
    if judgment is None:
        return "failed: see the report's error" if error else "no judgment saved"
    flags = [name.replace("_", " ") for name in ("floodwater", "debris", "tarpaulin")
             if judgment.get(name)]  # fmt: skip
    if judgment.get("fallen_tree"):
        flags.append("fallen tree (needs confirmation)")
    return " · ".join([f"roof damage: {judgment.get('roof_damage', 'unknown')}", *flags])


def _roof_output(measurement: dict[str, Any] | None) -> str:
    if measurement is None:
        return "no measurement saved"
    return f"{_plural(measurement['roof_count'], 'roof')} found"


def _flood_output(measurement: dict[str, Any] | None) -> str:
    if measurement is None:
        return "no measurement saved"
    if not measurement["floodwater_present"]:
        return "no floodwater"
    return f"floodwater over {measurement['floodwater_fraction']:.0%} of the photo"


def _facts_output(facts: dict[str, Any] | None) -> str:
    if facts is None:
        return "no facts saved"
    lines = fact_lines(facts)
    if not lines:
        return "no damage figures stated"
    more = len(lines) - FACT_LINES_SHOWN
    return ", ".join(lines[:FACT_LINES_SHOWN]) + (f" (+{more} more)" if more > 0 else "")


def _geo_output(report: Report, assessment: Assessment) -> str:
    how = GEO_SOURCES.get(assessment.geo_source, assessment.geo_source)
    if report.barangay is None:
        return f"no barangay ({how})"
    return f"barangay {report.barangay} ({how})"


def _trust_output(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "no trust signals"
    codes = ", ".join(dict.fromkeys(s["code"].replace("_", " ") for s in signals))
    return f"{_plural(len(signals), 'signal')}: {codes}"


def _decision_output(d: AgentDecision) -> str:
    target = f"incident #{d.incident_id}"
    action = {
        "new_incident": f"started {target}",
        "merged": f"joined {target}",
        "kept_separate": f"kept apart, in {target}",
        "needs_officer": f"asked the officer ({target})",
        "re-linked": f"moved to {target}",
    }.get(d.kind, f"{d.kind.replace('_', ' ')}: {target}")
    if d.judge_model is None or d.compared_incident_id is None:
        return action
    confidence = f", {d.confidence:.0%}" if d.confidence is not None else ""
    relation = {"same": "same as", "different": "different from"}.get(d.decision, d.decision)
    return f"{relation} incident #{d.compared_incident_id}{confidence} → {action}"


def _officer_output(action: OfficerAction) -> str:
    detail = action.detail or {}
    label = ACTION_LABELS.get(action.kind, action.kind.replace("_", " "))
    if action.kind == "officer_set_place" and detail.get("barangay"):
        return f"{label}: {detail['barangay']}"
    if action.kind == "officer_signal_reviewed" and detail.get("verdict"):
        code = str(detail.get("code", "signal")).replace("_", " ")
        return f"{label}: {code}, {SIGNAL_VERDICTS.get(detail['verdict'], detail['verdict'])}"
    if action.kind == "officer_fallen_tree" and detail.get("verdict"):
        return f"{label}: {TREE_VERDICTS.get(detail['verdict'], detail['verdict'])}"
    if action.kind in ("officer_merged", "officer_kept_separate") and action.incident_id:
        return f"{label} → incident #{action.incident_id}"
    return label


class _Calls:
    """One report's logged calls, grouped by (step, subject) and handed out once each."""

    def __init__(self, calls: list[ModelCall]) -> None:
        self.groups: dict[tuple[str | None, str | None], list[ModelCall]] = defaultdict(list)
        for call in calls:
            self.groups[(call.step, call.subject)].append(call)

    def take(self, step: Step, subject: str | None = None) -> CallStats | None:
        calls = self.groups.pop((step, subject), None)
        return CallStats.of(calls) if calls else None

    def take_before(self, step: Step, subject: str, at: datetime) -> CallStats | None:
        """The calls of one comparison up to a decision (a later re-grouping logs its own)."""
        calls = self.groups.get((step, subject), [])
        before = [c for c in calls if c.at <= at]
        if not before:
            return None
        rest = calls[len(before) :]
        if rest:
            self.groups[(step, subject)] = rest
        else:
            self.groups.pop((step, subject), None)
        return CallStats.of(before)


def _step(step: Step, output: str, stats: CallStats | None = None, **extra) -> TraceStep:
    info = STEPS[step]
    return TraceStep(info.label, info.actor, output, stats, **extra)


def build_trace(
    report: Report,
    assessment: Assessment | None,
    photos: list[PhotoAssessment],
    calls: list[ModelCall],
    decisions: list[AgentDecision],
    actions: list[OfficerAction],
) -> Trace:
    trace = Trace(
        calls=len(calls),
        cached_calls=sum(1 for c in calls if c.cached),
        cost=sum(c.cost_estimate for c in calls),
        model_ms=sum(c.latency_ms for c in calls),
    )
    pending = _Calls(calls)
    steps = trace.steps
    words = len(report.transcript.split()) if report.transcript else 0
    transcribed = sorted(
        (subject for step, subject in pending.groups if step == Step.TRANSCRIBE),
        key=lambda subject: _subject_id(subject, "part") or 0,
    )
    for subject in transcribed:
        part = _subject_id(subject, "part")
        label = STEPS[Step.TRANSCRIBE].label + (f" (part {part})" if part else "")
        output = f"{_plural(words, 'word')} transcribed" if words else "no speech transcribed"
        steps.append(TraceStep(label, "model", output, pending.take(Step.TRANSCRIBE, subject)))
    if report.transcript and assessment and assessment.transcript_clean is not None:
        cleaned = assessment.transcript_clean != report.transcript
        steps.append(
            _step(Step.CLEANUP, "repeated words removed" if cleaned else "nothing to clean")
        )
    facts_calls = pending.take(Step.FACTS)
    if facts_calls or (assessment and assessment.facts is not None):
        steps.append(
            _step(
                Step.FACTS,
                _facts_output(assessment and assessment.facts),
                facts_calls,
                model=assessment.llm_model if assessment else None,
            )
        )
    for number, photo in enumerate(sorted(photos, key=lambda p: p.media_id), 1):
        subject = f"media:{photo.media_id}"
        suffix = f" · photo {number}" if len(photos) > 1 else ""
        for step, output, model in (
            (Step.PHOTO_JUDGE, _judgment_output(photo.judgment, photo.error), photo.llm_model),
            (Step.SAM_ROOF, _roof_output(photo.measurement), SAM_MODEL),
            (Step.SAM_FLOODWATER, _flood_output(photo.measurement), SAM_MODEL),
        ):
            stats = pending.take(step, subject)
            if stats is None and photo.judgment is None:
                continue  # a failed photo: the error is on the report
            info = STEPS[step]
            steps.append(TraceStep(info.label + suffix, info.actor, output, stats, model=model))
    if assessment is not None:
        steps.append(_step(Step.GEOLOCATE, _geo_output(report, assessment)))
        if assessment.signals is not None:
            steps.append(_step(Step.TRUST, _trust_output(assessment.signals)))
    for d in decisions:
        if d.judge_model is None:
            steps.append(
                _step(Step.GROUPING_RULE, _decision_output(d), detail=d.reason, at=d.created_at)
            )
            continue
        stats = (
            pending.take_before(Step.PAIR_JUDGE, f"incident:{d.compared_incident_id}", d.created_at)
            if d.compared_incident_id is not None
            else None
        )
        steps.append(
            _step(
                Step.PAIR_JUDGE,
                _decision_output(d),
                stats,
                model=d.judge_model,
                detail=d.reason,
                at=d.created_at,
            )
        )
    # Calls with no saved output to sit beside (a failed photo, an interrupted grouping) are
    # still shown, so the trace accounts for every call it totals.
    for (step, _subject), group in sorted(
        pending.groups.items(), key=lambda item: _order(item[0][0])
    ):
        known = _as_step(step)
        label = STEPS[known].label if known else (step or "unlabelled call").replace("_", " ")
        steps.append(TraceStep(label, "model", "no saved output", CallStats.of(group)))
    for action in actions:
        steps.append(
            TraceStep(
                "Officer decision",
                "officer",
                _officer_output(action),
                officer=action.officer,
                at=action.created_at,
            )
        )
    model_outputs = bool(
        report.transcript
        or (assessment and assessment.facts is not None)
        or any(p.judgment for p in photos)
        or any(d.judge_model for d in decisions)
    )
    trace.recorded = bool(calls) or not model_outputs
    return trace


def _as_step(value: str | None) -> Step | None:
    return Step(value) if value in Step._value2member_map_ else None


def _order(value: str | None) -> int:
    step = _as_step(value)
    return STEP_ORDER[step] if step else len(STEP_ORDER)


def report_traces(session: Session, reports: list[Report]) -> dict[int, Trace]:
    """The traces of many reports, in a fixed number of queries."""
    ids = [r.id for r in reports]
    if not ids:
        return {}

    def rows(model, order) -> defaultdict[int, list]:
        grouped: defaultdict[int, list] = defaultdict(list)
        query = select(model).where(col(model.report_id).in_(ids)).order_by(*order)
        for row in session.exec(query).all():
            grouped[row.report_id].append(row)
        return grouped

    assessments = {
        a.report_id: a
        for a in session.exec(select(Assessment).where(col(Assessment.report_id).in_(ids))).all()
    }
    photos = rows(PhotoAssessment, (col(PhotoAssessment.media_id),))
    calls = rows(ModelCall, (col(ModelCall.id),))
    decisions = rows(AgentDecision, (col(AgentDecision.created_at), col(AgentDecision.id)))
    actions = rows(OfficerAction, (col(OfficerAction.created_at), col(OfficerAction.id)))
    return {
        r.id: build_trace(
            r, assessments.get(r.id), photos[r.id], calls[r.id], decisions[r.id], actions[r.id]
        )
        for r in reports
    }


@dataclass(frozen=True)
class DraftCalls:
    model: str
    family: ModelFamily | None
    calls: int
    cached_calls: int
    cost: float
    model_ms: int


def draft_calls(session: Session, draft_id: int) -> list[DraftCalls]:
    """The model calls that wrote a draft (its RDANA summary attempts), per model."""
    rows = session.exec(
        select(
            ModelCall.model,
            func.count(),
            func.coalesce(func.sum(case((col(ModelCall.cached), 1), else_=0)), 0),
            func.total(ModelCall.cost_estimate),
            func.coalesce(func.sum(ModelCall.latency_ms), 0),
        )
        .where(ModelCall.draft_id == draft_id)
        .group_by(ModelCall.model)
        .order_by(ModelCall.model)
    ).all()
    return [DraftCalls(m, model_family(m), n, c, cost, ms) for m, n, c, cost, ms in rows]

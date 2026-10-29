import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlmodel import Session, select

from app.assessment import AssessmentService
from app.counting import COUNT_FIELDS, collect
from app.db import init_db, make_engine, make_recorder
from app.facts import FACT_COUNT_FIELDS
from app.incidents import (
    GPS_DISTANCE_CAP_M,
    IncidentService,
    Observation,
    candidate_incidents,
    distance_m,
    rollup,
)
from app.intake import IntakeService
from app.meta.base import CallRecord, ModelApiError
from app.models import (
    AgentDecision,
    Assessment,
    Incident,
    ModelCall,
    PhotoAssessment,
    Report,
    Reporter,
)
from app.pair_judge import PAIR_SCHEMA, redacted_place, report_context
from tests.test_assessment import FakeLLM, FakeSam, facts
from tests.test_geo import CENTRES

AT = datetime(2026, 10, 28, 4)
SAME = {
    "decision": "same",
    "confidence": 0.97,
    "reason": "Same flood at the same synthetic street.",
}
DIFFERENT = {"decision": "different", "confidence": 0.96, "reason": "Different synthetic streets."}


class FakeJudge:
    model = "fake-pair-judge"

    def __init__(self, *replies, engine=None):
        self.replies = list(replies)
        self.calls = []
        self.engine = engine
        self.closed = False

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.engine:
            make_recorder(self.engine)(CallRecord(self.model, 1, False, 0))
        await asyncio.sleep(0)  # allow competing group tasks to run
        reply = self.replies.pop(0) if self.replies else SAME
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def aclose(self):
        self.closed = True


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def rows(engine, model):
    with Session(engine) as session:
        return session.exec(select(model).order_by(model.id)).all()


def add_report(engine, *, barangay="Malaban", at=AT, point=None, **fields):
    with Session(engine, expire_on_commit=False) as session:
        reporter = session.exec(select(Reporter)).first()
        if reporter is None:
            reporter = Reporter(pseudonym="synthetic")
            session.add(reporter)
            session.flush()
        report = Report(
            reporter_id=reporter.id,
            channel="demo",
            barangay=barangay,
            received_at=at,
            text="Raw text must never reach the judge.",
            status="assessed",
            lat=point[0] if point else None,
            lon=point[1] if point else None,
        )
        session.add(report)
        session.flush()
        session.add(
            Assessment(
                report_id=report.id,
                llm_model="fake-facts",
                facts=facts(hazards=["flood"], place_detail="Demo Street A", **fields),
            )
        )
        session.commit()
        return report.id


async def test_three_reports_become_two_incidents_and_repeat_changes_nothing(settings, engine):
    judge = FakeJudge(engine=engine)
    service = IncidentService(settings, engine, judge)
    ids = [
        add_report(engine),
        add_report(engine, at=AT + timedelta(days=1), flood_depth="knee"),
        add_report(engine, barangay="Casile", at=AT + timedelta(days=1)),
    ]
    for report_id in ids:
        await service.group(report_id)
    incidents = rows(engine, Incident)
    assert len(incidents) == 2
    assert incidents[0].latest_facts["report_count"] == 2
    decisions = rows(engine, AgentDecision)
    merged = [d for d in decisions if d.kind == "merged"]
    assert len(merged) == 1
    assert (merged[0].judge_model, merged[0].reason) == (judge.model, SAME["reason"])
    assert len(judge.calls) == 1
    assert len(rows(engine, ModelCall)) == 1  # write succeeded during the judge call
    before = [
        r.model_dump() for model in (Incident, AgentDecision, Report) for r in rows(engine, model)
    ]
    for report_id in ids:
        await service.group(report_id)
    after = [
        r.model_dump() for model in (Incident, AgentDecision, Report) for r in rows(engine, model)
    ]
    assert before == after
    assert len(judge.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"decision": "unsure", "confidence": 0.8, "reason": "Location is unclear."},
        {"decision": "same", "confidence": 0.6, "reason": "Possibly the same street."},
    ],
)
async def test_uncertainty_never_merges(settings, engine, reply):
    service = IncidentService(settings, engine, FakeJudge(reply))
    first = await service.group(add_report(engine))
    second = await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    assert first.incident_id != second.incident_id
    assert rows(engine, Incident)[1].status == "needs_officer"
    decision = next(d for d in rows(engine, AgentDecision) if d.judge_model)
    assert decision.kind == "needs_officer"
    assert decision.compared_incident_id == first.incident_id
    assert decision.compared_report_id == first.id


async def test_no_place_has_own_review_incident_without_a_call(settings, engine):
    judge = FakeJudge()
    service = IncidentService(settings, engine, judge)
    await service.group(add_report(engine, barangay=None))
    await service.group(add_report(engine, barangay=None))
    assert len(rows(engine, Incident)) == 2
    assert all(i.status == "needs_officer" for i in rows(engine, Incident))
    assert not judge.calls


async def test_late_pin_reuses_singleton_and_writes_relink(settings, engine):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_text(7, "Flood in Brgy. Casile")
    assessor = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    first = await assessor.assess(report.id)
    await intake.ingest_location(7, *CENTRES["Malaban"])
    second = await assessor.assess(report.id)
    assert first.incident_id == second.incident_id
    assert second.barangay == "Malaban"
    assert len(rows(engine, Incident)) == 1
    assert rows(engine, Incident)[0].barangay == "Malaban"
    assert len([d for d in rows(engine, AgentDecision) if d.kind == "re-linked"]) == 1
    before = len(rows(engine, AgentDecision))
    await assessor.assess(report.id)
    assert len(rows(engine, AgentDecision)) == before


async def test_relink_to_existing_retires_empty_group_and_preserves_audit(settings, engine):
    service = IncidentService(settings, engine, FakeJudge())
    first_id = add_report(engine, barangay="Casile")
    second_id = add_report(engine)
    old = await service.group(first_id)
    target = await service.group(second_id)
    with Session(engine) as session:
        report = session.get(Report, first_id)
        report.barangay = "Malaban"
        session.add(report)
        session.commit()
    moved = await service.group(first_id)
    assert moved.incident_id == target.incident_id
    incidents = rows(engine, Incident)
    assert sum(i.active for i in incidents) == 1
    assert next(i for i in incidents if i.id == old.incident_id).latest_facts["report_count"] == 0
    assert next(i for i in incidents if i.active).latest_facts["report_count"] == 2
    assert len([d for d in rows(engine, AgentDecision) if d.kind == "re-linked"]) == 1


async def test_different_tries_next_candidate(settings, engine):
    judge = FakeJudge(DIFFERENT, DIFFERENT, SAME)
    service = IncidentService(settings, engine, judge)
    first = await service.group(add_report(engine))
    await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    third = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert third.incident_id == first.incident_id
    assert len(judge.calls) == 3
    assert len([d for d in rows(engine, AgentDecision) if d.kind == "kept_separate"]) == 2


@pytest.mark.parametrize("confidence", [0.0, 0.8, 1.0])
async def test_different_at_any_confidence_tries_older_candidate(settings, engine, confidence):
    judge = FakeJudge(
        DIFFERENT, {**DIFFERENT, "confidence": confidence}, {**SAME, "confidence": 0.95}
    )
    service = IncidentService(settings, engine, judge)
    older = await service.group(add_report(engine))
    newer = await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    third = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert third.incident_id == older.incident_id != newer.incident_id
    assert third.grouping_needs_officer is False
    assert len(judge.calls) == 3
    compared = [d for d in rows(engine, AgentDecision) if d.report_id == third.id]
    assert [(d.kind, d.compared_incident_id) for d in compared] == [
        ("kept_separate", newer.incident_id),
        ("merged", older.incident_id),
    ]
    assert all(i.status == "open" for i in rows(engine, Incident))


@pytest.mark.parametrize(
    "place,names",
    [
        ("tapat ng bahay ni Pedro Santos, Purok 3", ["Pedro", "Santos"]),
        ("nina Jose at Maria", ["Jose", "Maria"]),
        ("kay Kapitan Reyes", ["Reyes"]),
        ("kina Aling Nena", ["Nena"]),
        ("Juan dela Cruz's store", ["Juan", "dela", "Cruz"]),
        ("beside Juan dela Cruz’s sari-sari store", ["Juan", "dela", "Cruz"]),
        *[
            (f"{title} Reyes, Purok 3", ["Reyes"])
            for title in ("Kap.", "Kapitan", "Kagawad", "Kgd.", "Aling", "Mang", "Mr", "Mrs", "Ms")
        ],
    ],
)
def test_place_personal_references_are_redacted(place, names):
    result = redacted_place(place)
    context = json.dumps(
        report_context(barangay="Malaban", facts={"place_detail": place}, judgments=[])
    )
    for name in names:
        assert name not in result
        assert name not in context
    if "Purok 3" in place:
        assert "Purok 3" in result
    if "store" in place:
        assert "store" in result


@pytest.mark.parametrize("place", ["Purok 3", "tapat ng chapel", "malapit sa covered court"])
def test_place_landmarks_survive_redaction(place):
    assert redacted_place(place) == place


async def test_model_failure_is_visible_idempotent_and_explicitly_retryable(settings, engine):
    judge = FakeJudge(ModelApiError("Fireworks", "muse-glimmer not deployed", status=404))
    service = IncidentService(settings, engine, judge)
    await service.group(add_report(engine))
    report_id = add_report(engine, at=AT + timedelta(hours=1))
    failed = await service.group(report_id)
    assert "not deployed" in failed.grouping_error
    assert rows(engine, Incident)[1].status == "needs_officer"
    decisions = len(rows(engine, AgentDecision))
    await service.group(report_id)
    assert len(rows(engine, AgentDecision)) == decisions
    assert len(judge.calls) == 1
    retried = await service.group(report_id, retry_failed=True)
    assert retried.grouping_error is None
    assert sum(i.active for i in rows(engine, Incident)) == 1


async def test_concurrent_reports_and_services_do_not_duplicate_groups(settings, engine):
    judge = FakeJudge()
    a, b = IncidentService(settings, engine, judge), IncidentService(settings, engine, judge)
    ids = [add_report(engine, at=AT + timedelta(hours=n)) for n in range(4)]
    await asyncio.gather(*(a.group(i) for i in ids), b.group(ids[0]))
    assert len(rows(engine, Incident)) == 1
    assert len(rows(engine, AgentDecision)) == 4
    assert len(judge.calls) == 3


def observation(**changes):
    base = Observation(1, None, AT, "Malaban", None, facts(), {})
    return replace(base, **changes)


def test_candidates_filter_time_barangay_and_gps_and_add_point_only():
    new = observation(point=(14.3, 121.1))
    close = observation(id=2, point=(14.3001, 121.1))
    groups = {
        1: [close],
        2: [replace(close, barangay="Casile")],
        3: [replace(close, at=AT - timedelta(days=15))],
        4: [replace(close, point=(14.4, 121.1))],
        5: [replace(close, barangay=None)],
        6: [replace(close, at=AT + timedelta(days=15))],
    }
    assert candidate_incidents(new, groups, window_days=14) == [1, 5]
    assert candidate_incidents(replace(new, barangay=None), groups, window_days=14) == [1, 2, 5]
    assert (
        candidate_incidents(replace(new, barangay=None, point=None), groups, window_days=14) == []
    )
    assert candidate_incidents(
        new, {1: [replace(close, at=AT - timedelta(days=14))]}, window_days=14
    ) == [1]
    assert distance_m(new.point, groups[4][0].point) > GPS_DISTANCE_CAP_M
    assert distance_m(new.point, close.point) < GPS_DISTANCE_CAP_M


def test_rollup_uses_time_order_max_depth_nulls_and_caveated_totals():
    early = observation(facts=facts(flood_depth="chest", houses_flooded=5, families_affected=2))
    late = observation(
        id=2,
        at=AT + timedelta(days=1),
        facts=facts(
            flood_depth="ankle",
            houses_flooded=3,
            families_affected=1,
            is_correction=True,
        ),
    )
    result = rollup([late, early])
    assert (result["max_depth"], result["latest_depth"]) == ("chest", "ankle")
    assert result["totals"]["houses_flooded"] == 8
    assert result["totals"]["families_affected"] == 3
    assert result["totals"]["houses_totally_damaged"] is None
    assert "double-count" in result["caveat"] and "corrections" in result["caveat"]
    unknown = rollup([observation()])
    assert unknown["max_depth"] == unknown["latest_depth"] == "unknown"
    assert rollup([])["report_count"] == 0


async def test_judge_receives_only_redacted_derived_context(settings, engine):
    judge = FakeJudge()
    service = IncidentService(settings, engine, judge)
    await service.group(add_report(engine, summary_en="private person name"))
    report_id = add_report(engine, at=AT + timedelta(hours=1), summary_en="another private name")
    with Session(engine) as session:
        session.add(
            PhotoAssessment(
                report_id=report_id,
                media_id=99,
                llm_model="fake",
                judgment={"roof_damage": "partial", "notes": "private photo name"},
            )
        )
        session.commit()
    await service.group(report_id)
    call = judge.calls[0]
    assert call["schema"] == PAIR_SCHEMA and "image" not in call
    assert not any(
        s in call["user_text"]
        for s in (
            "private",
            "Raw text",
            "synthetic",
            "reporter",
            "transcript",
            "measurement",
        )
    )
    assert "roof damage: partial" in call["user_text"]
    context = report_context(
        barangay="Malaban",
        facts={
            "place_detail": "Purok 3, my name is Demo Person, near Street A",
            "hazards": ["name slipped into enum"],
            "houses_flooded": "private name",
        },
        judgments=[],
    )
    assert "Demo Person" not in json.dumps(context)
    assert "hazards" not in context["facts"]
    assert "houses_flooded" not in context["facts"]


@pytest.mark.parametrize("confidence", [-1, 1.1, float("nan"), float("inf")])
async def test_invalid_confidence_never_merges(settings, engine, confidence):
    service = IncidentService(settings, engine, FakeJudge({**SAME, "confidence": confidence}))
    await service.group(add_report(engine))
    out = await service.group(add_report(engine))
    assert out.grouping_error.startswith("ModelOutputError")
    assert len(rows(engine, Incident)) == 2


def test_schema_upgrade_preserves_existing_reports_and_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE report (
            id INTEGER PRIMARY KEY, reporter_id INTEGER, channel VARCHAR, received_at DATETIME,
            text VARCHAR, transcript VARCHAR, lat FLOAT, lon FLOAT, barangay VARCHAR, status VARCHAR
        )""")
        connection.exec_driver_sql("INSERT INTO report (id, text) VALUES (42, 'synthetic')")
    init_db(engine)
    init_db(engine)
    assert {"incident_id", "grouping_fingerprint", "grouping_error"} <= {
        c["name"] for c in inspect(engine).get_columns("report")
    }
    with Session(engine) as session:
        assert session.get(Report, 42).text == "synthetic"
    engine.dispose()


async def test_gps_only_member_keeps_known_incident_barangay(settings, engine):
    service = IncidentService(settings, engine, FakeJudge())
    first = await service.group(add_report(engine, point=CENTRES["Malaban"]))
    second = await service.group(
        add_report(
            engine,
            barangay=None,
            point=CENTRES["Malaban"],
            at=AT + timedelta(hours=1),
        )
    )
    assert second.incident_id == first.incident_id
    incident = rows(engine, Incident)[0]
    assert incident.barangay == "Malaban" and incident.title.startswith("Malaban")
    # A subsequent named report without GPS still finds this incident.
    third = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert third.incident_id == first.incident_id


async def test_moving_uncertain_member_clears_old_review_status(settings, engine):
    judge = FakeJudge(
        {"decision": "unsure", "confidence": 0.5, "reason": "Place unclear."}, DIFFERENT, SAME
    )
    service = IncidentService(settings, engine, judge)
    await service.group(add_report(engine))
    uncertain = await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    # A later confident match may join a group that still contains unresolved evidence (the
    # open incident is judged first and is different).
    joined = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert joined.incident_id == uncertain.incident_id
    assert rows(engine, Incident)[1].status == "needs_officer"
    with Session(engine) as session:
        report = session.get(Report, uncertain.id)
        report.barangay = "Casile"
        session.add(report)
        session.commit()
    await service.group(uncertain.id)
    incidents = rows(engine, Incident)
    assert all(i.status == "open" for i in incidents)
    assert (
        next(i for i in incidents if i.id == joined.incident_id).latest_facts["report_count"] == 1
    )


async def test_separate_connections_retry_stale_grouping_plan(settings, engine):
    other_engine = make_engine(settings)
    judge = FakeJudge()
    a = IncidentService(settings, engine, judge)
    b = IncidentService(settings, other_engine, judge)
    try:
        await a.group(add_report(engine))
        second = add_report(engine, at=AT + timedelta(hours=1))
        third = add_report(engine, at=AT + timedelta(hours=2))
        await asyncio.gather(a.group(second), b.group(third))
        assert len(rows(engine, Incident)) == 1
        assert rows(engine, Incident)[0].latest_facts["report_count"] == 3
        assert len(rows(engine, AgentDecision)) == 3
    finally:
        other_engine.dispose()


async def test_reassessment_splits_changed_situation_and_refreshes_both_rollups(settings, engine):
    judge = FakeJudge(SAME, DIFFERENT)
    service = IncidentService(settings, engine, judge)
    first_id = add_report(engine, houses_flooded=4)
    second_id = add_report(engine, at=AT + timedelta(hours=1), houses_flooded=7)
    first = await service.group(first_id)
    await service.group(second_id)
    with Session(engine) as session:
        assessment = session.exec(select(Assessment).where(Assessment.report_id == second_id)).one()
        assessment.facts = {**assessment.facts, "place_detail": "Demo Street Z"}
        session.add(assessment)
        session.commit()
    moved = await service.group(second_id)
    assert moved.incident_id != first.incident_id
    incidents = rows(engine, Incident)
    assert [i.latest_facts["totals"]["houses_flooded"] for i in incidents] == [4, 7]
    assert all(i.latest_facts["report_count"] == 1 for i in incidents)


async def test_grouping_cli_all_report_and_missing_id(settings, engine, monkeypatch, capsys):
    from app import incidents

    first = add_report(engine)
    second = add_report(engine, at=AT + timedelta(hours=1))
    judge = FakeJudge()
    monkeypatch.setattr(incidents, "get_settings", lambda: settings)
    monkeypatch.setattr(incidents, "make_pair_judge", lambda settings, recorder: judge)
    import argparse

    args = argparse.Namespace(all=True, report=None, judge="muse-glimmer", retry_failed=False)
    assert await incidents._cli(args) == 0
    output = capsys.readouterr().out
    assert "grouping 2 report(s)" in output
    assert f"report {first}: incident" in output and f"report {second}: incident" in output
    assert judge.closed and len(judge.calls) == 1
    count = len(rows(engine, AgentDecision))
    assert await incidents._cli(argparse.Namespace(**{**vars(args), "report": first})) == 0
    assert len(rows(engine, AgentDecision)) == count
    assert await incidents._cli(argparse.Namespace(**{**vars(args), "report": 999})) == 1
    assert "not found" in capsys.readouterr().out


async def test_unassessed_report_is_skipped_and_has_no_incident(settings, engine):
    service = IncidentService(settings, engine, FakeJudge())
    report_id = add_report(engine)
    with Session(engine) as session:
        report = session.get(Report, report_id)
        report.status = "assessment_failed"
        session.add(report)
        session.commit()
    assert (await service.group(report_id)).incident_id is None
    assert service.report_ids() == []
    assert await service.group(999) is None
    assert not rows(engine, AgentDecision)


UNSURE = {"decision": "unsure", "confidence": 0.7, "reason": "Place cannot be established."}


def same(confidence):
    return {"decision": "same", "confidence": confidence, "reason": "Same synthetic situation."}


async def test_correction_goes_to_the_incident_stating_the_corrected_figure(settings, engine):
    """The 2026-10-29 live case: Casile 3 houses, an unconfirmed old photo, then "2, not 3"."""
    judge = FakeJudge(UNSURE, same(0.85), same(0.85))
    service = IncidentService(settings, engine, judge)
    voice = await service.group(add_report(engine, barangay="Casile", houses_partially_damaged=3))
    photo = await service.group(add_report(engine, barangay="Casile", at=AT + timedelta(hours=1)))
    assert photo.incident_id != voice.incident_id and photo.grouping_needs_officer
    correction = await service.group(
        add_report(
            engine,
            barangay="Casile",
            at=AT + timedelta(hours=2),
            is_correction=True,
            houses_partially_damaged=2,
        )
    )
    assert correction.incident_id == voice.incident_id
    assert correction.grouping_needs_officer is False
    decisions = [d for d in rows(engine, AgentDecision) if d.report_id == correction.id]
    assert [(d.kind, d.compared_incident_id) for d in decisions] == [
        ("merged", voice.incident_id),
        ("kept_separate", photo.incident_id),
    ]
    assert (
        f"corrects the houses partially damaged figure stated in incident {voice.incident_id}"
        in decisions[0].reason
    )
    assert f"incident {photo.incident_id} waits for officer review" in decisions[0].reason
    with Session(engine) as session:
        houses = collect(session).barangays["Casile"].count("houses_partially_damaged")
    assert houses.value == 2
    assert houses.contributions[0].rule == "latest correction"
    assert houses.pending_value is None


async def test_wrongly_merged_correction_moves_when_regrouped(settings, engine):
    """A correction merged by the old first-match rule moves once its grouping is re-run."""
    service = IncidentService(settings, engine, FakeJudge(UNSURE))
    voice = await service.group(add_report(engine, barangay="Casile", houses_partially_damaged=3))
    photo = await service.group(add_report(engine, barangay="Casile", at=AT + timedelta(hours=1)))
    correction_id = add_report(
        engine,
        barangay="Casile",
        at=AT + timedelta(hours=2),
        is_correction=True,
        houses_partially_damaged=2,
    )
    with Session(engine) as session:  # the live state: merged into the photo's incident
        report = session.get(Report, correction_id)
        report.incident_id, report.grouping_fingerprint = photo.incident_id, "old-rule"
        session.add(report)
        session.commit()
    service.judge.replies = [same(0.9), same(0.9)]
    moved = await service.group(correction_id)
    assert moved.incident_id == voice.incident_id
    relinks = [d for d in rows(engine, AgentDecision) if d.kind == "re-linked"]
    assert [(d.compared_incident_id, d.incident_id) for d in relinks] == [
        (photo.incident_id, voice.incident_id)
    ]
    statuses = {i.id: i.status for i in rows(engine, Incident)}
    assert statuses == {voice.incident_id: "open", photo.incident_id: "needs_officer"}


async def test_unsure_on_one_candidate_does_not_stop_a_match_on_another(settings, engine):
    judge = FakeJudge(DIFFERENT, UNSURE, SAME)
    service = IncidentService(settings, engine, judge)
    older = await service.group(add_report(engine))
    newer = await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    third = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert third.incident_id == older.incident_id != newer.incident_id
    assert third.grouping_needs_officer is False
    assert all(i.status == "open" for i in rows(engine, Incident))
    compared = [d for d in rows(engine, AgentDecision) if d.report_id == third.id]
    assert [(d.kind, d.compared_incident_id) for d in compared] == [
        ("kept_separate", newer.incident_id),
        ("merged", older.incident_id),
    ]


async def test_best_accepted_match_wins_by_confidence_then_candidate_order(settings, engine):
    judge = FakeJudge(DIFFERENT, same(0.9), same(0.97))
    service = IncidentService(settings, engine, judge)
    older = await service.group(add_report(engine))
    await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    third = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert third.incident_id == older.incident_id
    merged = next(d for d in rows(engine, AgentDecision) if d.kind == "merged")
    assert "higher confidence (0.97 vs 0.90)" in merged.reason


async def test_judge_calls_are_capped_per_grouping(settings, engine):
    settings.max_judged_candidates = 2
    judge = FakeJudge(*[DIFFERENT] * 20)
    service = IncidentService(settings, engine, judge)
    for hour in range(5):
        await service.group(add_report(engine, at=AT + timedelta(hours=hour)))
    assert len(rows(engine, Incident)) == 5
    # 0 + 1 + 2 + 2 + 2 calls: the fourth and fifth reports had 3 and 4 candidates.
    assert len(judge.calls) == 7


async def test_judge_failure_stops_judging_and_never_merges(settings, engine):
    judge = FakeJudge(DIFFERENT)
    service = IncidentService(settings, engine, judge)
    await service.group(add_report(engine))
    await service.group(add_report(engine, at=AT + timedelta(hours=1)))
    judge.replies = [ModelApiError("Meta", "unavailable", status=503), SAME]
    failed = await service.group(add_report(engine, at=AT + timedelta(hours=2)))
    assert "unavailable" in failed.grouping_error
    assert failed.grouping_needs_officer is True
    assert len(rows(engine, Incident)) == 3
    assert len([d for d in rows(engine, AgentDecision) if d.report_id == failed.id]) == 2


def test_correction_fields_reuse_the_facts_schema():
    assert set(COUNT_FIELDS) <= set(FACT_COUNT_FIELDS)
    assert "houses_partially_damaged" in FACT_COUNT_FIELDS
    assert "is_correction" not in FACT_COUNT_FIELDS

"""Reports a restart interrupted (new, or cut off while assessing) resume once at startup.

No model call is real: the assessor runs on the fakes from test_assessment.
"""

import logging
from datetime import timedelta

import pytest
from sqlmodel import Session

from app import trust
from app.assessment import AssessmentService, resumable_report_ids
from app.bot import resume_interrupted
from app.db import init_db, make_engine
from app.intake import IntakeService
from app.models import Report, Reporter, utcnow
from tests.test_assessment import FakeLLM, FakeSam


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def set_status(engine, report_id, status, **fields):
    with Session(engine) as session:
        report = session.get(Report, report_id)
        report.status = status
        for key, value in fields.items():
            setattr(report, key, value)
        session.add(report)
        session.commit()


def status(engine, report_id):
    with Session(engine) as session:
        return session.get(Report, report_id).status


async def test_new_and_cut_off_reports_resume_but_held_failed_and_blocked_do_not(settings, engine):
    intake = IntakeService(settings, engine)
    queued = await intake.ingest_text(1, "baha sa Casile, tuhod")  # stored, never assessed
    cut_off = await intake.ingest_text(2, "baha sa Malaban")
    started = utcnow() - timedelta(minutes=1)
    set_status(engine, cut_off.id, "assessing", assessing_started_at=started)
    held = await intake.ingest_text(3, "baha sa Poblacion")
    set_status(engine, held.id, "held", hold_reason="spend cap")
    failed = await intake.ingest_text(4, "baha sa Dela Paz")
    set_status(engine, failed.id, "assessment_failed")
    blocked = await intake.ingest_text(5, "spam")
    with Session(engine) as session:
        reporter = session.get(Reporter, session.get(Report, blocked.id).reporter_id)
        trust.set_blocked(session, reporter.pseudonym[:8], True)

    llm = FakeLLM()
    assessor = AssessmentService(settings, engine, llm, FakeSam())
    assert await resume_interrupted(assessor, engine, pause_s=0) == [queued.id, cut_off.id]

    assert status(engine, queued.id) == status(engine, cut_off.id) == "assessed"
    assert status(engine, held.id) == "held"
    assert status(engine, failed.id) == "assessment_failed"
    assert status(engine, blocked.id) == "new"
    assert await resume_interrupted(assessor, engine, pause_s=0) == []  # nothing left


async def test_a_report_assessed_meanwhile_is_not_assessed_again(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(1, "baha sa Casile")
    llm = FakeLLM()
    assessor = AssessmentService(settings, engine, llm, FakeSam())
    (pending,), _ = resumable_report_ids(engine, 10)
    await assessor.assess(report.id)  # a live message got there first
    calls = len(llm.calls)
    await assessor.assess(pending, only_if=("new", "assessing"))
    assert len(llm.calls) == calls


async def test_resume_is_bounded_to_the_newest_and_says_how_many_wait(settings, engine, caplog):
    intake = IntakeService(settings, engine)
    ids = [(await intake.ingest_text(10 + i, f"baha {i}")).id for i in range(4)]
    with Session(engine) as session:  # oldest first by time received
        for minutes, report_id in enumerate(ids):
            row = session.get(Report, report_id)
            row.received_at = utcnow() - timedelta(minutes=10 - minutes)
            session.add(row)
        session.commit()
    assert resumable_report_ids(engine, 2) == (ids[2:], 4)

    caplog.set_level(logging.INFO)
    assessor = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    assert await resume_interrupted(assessor, engine, limit=2, pause_s=0) == ids[2:]
    assert "2 older interrupted report(s) not resumed" in caplog.text
    assert [status(engine, i) for i in ids] == ["new", "new", "assessed", "assessed"]

"""The agent trace plumbing: every model call is logged with the report, step and subject it
served (app.trace), concurrent assessments never mix them, and the migration is idempotent."""

import asyncio
import sqlite3

import httpx
import pytest
import respx
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.assessment import AssessmentService
from app.db import init_db, make_engine, make_recorder
from app.drafts import DraftService
from app.incidents import IncidentService
from app.intake import IntakeService
from app.meta.base import CallRecord
from app.meta.sam import parse_stream
from app.meta.transcribe import TranscribeClient
from app.models import Media, ModelCall, Report
from app.trace import STEP_ORDER, STEPS, Step, current, traced
from tests.test_assessment import FakeLLM, facts, jpeg
from tests.test_incidents import DIFFERENT, FakeJudge
from tests.test_incidents import add_report as add_assessed
from tests.test_intake import FakeTranscriber, fake_convert
from tests.test_photo import JUDGMENT
from tests.test_rdana import SummaryLLM, add_report, cite_all
from tests.test_sam import STREAM


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def calls(engine) -> list[ModelCall]:
    with Session(engine) as session:
        return list(session.exec(select(ModelCall).order_by(ModelCall.id)).all())


class RecordingSam:
    """SAM fake that logs each request through the real recorder, as SamClient does."""

    model = "fake-sam"

    def __init__(self, recorder):
        self.recorder = recorder

    async def segment(self, image, concept, *, image_mime="image/jpeg", image_url=None):
        await asyncio.sleep(0)  # let the concurrent photo calls interleave
        self.recorder(CallRecord(self.model, 7, False, 0.0025))
        return parse_stream(STREAM, concept)


def test_every_step_is_listed_once_with_a_label_and_an_actor():
    assert set(STEPS) == set(Step) and list(STEP_ORDER) == list(Step)
    assert {info.actor for info in STEPS.values()} == {"model", "rule"}
    rules = {step for step, info in STEPS.items() if info.actor == "rule"}
    assert rules == {Step.CLEANUP, Step.GEOLOCATE, Step.TRUST, Step.GROUPING_RULE}


def test_contexts_nest_and_reset():
    assert current() is None
    with traced(report_id=4):
        with traced(step=Step.FACTS):
            assert (current().report_id, current().step) == (4, Step.FACTS)
        assert (current().report_id, current().step) == (4, None)
    assert current() is None


@respx.mock
async def test_a_real_client_call_is_logged_with_its_context_and_unlinked_without_one(
    settings, cache, engine
):
    respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(200, json={"transcript": "hi", "audioDurationMs": 10})
    )
    client = TranscribeClient(api_key="k", cache=cache, recorder=make_recorder(engine))
    with traced(report_id=None, step=Step.TRANSCRIBE, subject="part:1"):
        await client.transcribe(b"audio")
    await client.transcribe(b"audio")  # a cache hit outside any step
    first, second = calls(engine)
    assert (first.step, first.subject, first.cached) == ("transcribe", "part:1", False)
    assert (second.step, second.subject, second.report_id, second.cached) == (
        None,
        None,
        None,
        True,
    )


async def test_assessing_a_photo_and_text_report_logs_each_step_against_the_report(
    settings, engine
):
    recorder = make_recorder(engine)
    report = await IntakeService(settings, engine).ingest_photo(
        111, jpeg(), caption="Baha po sa Brgy. Malaban"
    )
    llm = FakeLLM(facts(flood_depth="knee"), recorder=recorder)
    await AssessmentService(settings, engine, llm, RecordingSam(recorder)).assess(report.id)

    logged = calls(engine)
    assert {c.report_id for c in logged} == {report.id}
    assert sorted(c.step for c in logged) == sorted(
        ["facts", "photo_judge", "sam_roof", "sam_floodwater"]
    )
    photo_steps = [c for c in logged if c.step != "facts"]
    assert {c.subject for c in photo_steps} == {f"media:{photo_id(engine)}"}
    assert next(c for c in logged if c.step == "facts").subject is None
    assert current() is None  # nothing leaks out of the assessment


def photo_id(engine) -> int:
    with Session(engine) as session:
        return session.exec(select(Media.id).where(Media.kind == "photo")).one()


class MarkingLLM:
    """Logs each call with a model name naming the report it saw, after yielding to the other
    report's task, so a context leak between concurrent assessments shows as a mismatch."""

    model = "marking-llm"

    def __init__(self, recorder, owners):
        self.recorder, self.owners = recorder, owners

    async def chat_json(self, **kwargs):
        for _ in range(3):
            await asyncio.sleep(0)
        image = kwargs.get("image")
        owner = (
            self.owners[image]
            if image is not None
            else next(
                rid
                for marker, rid in self.owners.items()
                if isinstance(marker, str) and marker in kwargs["user_text"]
            )
        )
        self.recorder(CallRecord(f"seen-report-{owner}", 1, False, 0.0))
        return JUDGMENT if kwargs.get("image") is not None else facts()


class MarkingSam(RecordingSam):
    def __init__(self, recorder, owners):
        super().__init__(recorder)
        self.owners = owners

    async def segment(self, image, concept, *, image_mime="image/jpeg", image_url=None):
        for _ in range(2):
            await asyncio.sleep(0)
        owner = self.owners[image]
        self.recorder(CallRecord(f"seen-report-{owner}", 1, False, 0.0))
        return parse_stream(STREAM, concept)


async def test_two_concurrent_assessments_never_mix_their_report_ids(settings, engine):
    recorder = make_recorder(engine)
    intake = IntakeService(settings, engine)
    photo_a, photo_b = jpeg((800, 400)), jpeg((640, 480))
    a = await intake.ingest_photo(111, photo_a, caption="alphamarker baha sa Malaban")
    b = await intake.ingest_photo(222, photo_b, caption="bravomarker baha sa Casile")
    owners = {"alphamarker": a.id, "bravomarker": b.id}
    images = {photo_a: a.id, photo_b: b.id}
    llm = MarkingLLM(recorder, {**owners, **images})
    grouping = IncidentService(settings, engine, FakeJudge(DIFFERENT, DIFFERENT))
    service = AssessmentService(settings, engine, llm, MarkingSam(recorder, images), grouping)

    await asyncio.gather(service.assess(a.id), service.assess(b.id))

    logged = calls(engine)
    assert len(logged) == 8  # facts, photo judge and two SAM concepts, per report
    assert all(c.model == f"seen-report-{c.report_id}" for c in logged)
    assert {c.report_id for c in logged} == {a.id, b.id}


async def test_pair_judgments_are_logged_against_the_report_and_the_compared_incident(
    settings, engine
):
    judge = FakeJudge(DIFFERENT, engine=engine)
    service = IncidentService(settings, engine, judge)
    first = add_assessed(engine)
    await service.group(first)
    second = add_assessed(engine, place="Demo Street B")
    await service.group(second)

    (call,) = calls(engine)
    with Session(engine) as session:
        first_incident = session.get(Report, first).incident_id
    assert (call.report_id, call.step, call.subject) == (
        second,
        "pair_judge",
        f"incident:{first_incident}",
    )


async def test_voice_parts_are_logged_as_transcription_of_the_report(settings, engine):
    recorder = make_recorder(engine)

    class Recording(FakeTranscriber):
        async def transcribe(self, wav, **kwargs):
            recorder(CallRecord("muse-voice-transcribe-1.0", 3, False, 0.0001))
            return await super().transcribe(wav, **kwargs)

    intake = IntakeService(
        settings, engine, Recording(texts=["una", "ikalawa"]), convert=fake_convert
    )
    report = await intake.ingest_voice(111, b"OggS")

    logged = calls(engine)
    assert [(c.report_id, c.step, c.subject) for c in logged] == [
        (report.id, "transcribe", "part:1"),
        (report.id, "transcribe", "part:2"),
    ]


async def test_the_rdana_summary_calls_are_linked_to_the_draft_they_wrote(settings, engine):
    add_report(engine, hazards=["flood"])
    recorder = make_recorder(engine)

    class Recording(SummaryLLM):
        async def chat_json(self, **kwargs):
            recorder(CallRecord(self.model, 9, False, 0.002))
            return await super().chat_json(**kwargs)

    draft = await DraftService(settings, engine, Recording(cite_all)).generate_rdana("Malaban")

    (call,) = calls(engine)
    assert (call.draft_id, call.step, call.report_id) == (draft.id, "rdana_summary", None)


def test_the_migration_adds_the_trace_columns_to_an_old_database_and_is_idempotent(settings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.db_path) as old:
        old.execute(
            "CREATE TABLE modelcall (id INTEGER PRIMARY KEY, at DATETIME NOT NULL, "
            "model VARCHAR NOT NULL, latency_ms INTEGER NOT NULL, cached BOOLEAN NOT NULL, "
            "cost_estimate FLOAT NOT NULL)"
        )
        old.execute(
            "INSERT INTO modelcall (at, model, latency_ms, cached, cost_estimate) "
            "VALUES ('2026-10-28 01:00:00', 'muse-spark-1.3', 900, 0, 0.001)"
        )
    engine = make_engine(settings)
    init_db(engine)
    init_db(engine)  # a second start changes nothing

    columns = {c["name"] for c in inspect(engine).get_columns("modelcall")}
    assert {"report_id", "draft_id", "step", "subject"} <= columns
    indexes = {i["name"] for i in inspect(engine).get_indexes("modelcall")}
    assert {"ix_modelcall_report_id", "ix_modelcall_draft_id"} <= indexes
    (old_call,) = calls(engine)
    assert (old_call.report_id, old_call.draft_id, old_call.step, old_call.subject) == (
        None,
        None,
        None,
        None,
    )
    engine.dispose()

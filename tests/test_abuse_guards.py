"""Admission, cost/blocked holds, resume CLI and additive database upgrades."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlmodel import Session, select

from app import assessment, bot, trust
from app.assessment import AssessmentService
from app.config import Settings
from app.db import init_db, make_engine
from app.incidents import IncidentService
from app.intake import Admission, IntakeService
from app.models import Assessment, Media, ModelCall, PhotoAssessment, Report, Reporter, utcnow
from tests.test_assessment import FakeLLM, FakeSam, jpeg
from tests.test_bot import FakeTranscriber, make_update, one_chunk
from tests.test_incidents import FakeJudge


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def rows(engine, model):
    with Session(engine) as session:
        return session.exec(select(model).order_by(model.id)).all()


def add_cost(engine, amount, when=None):
    with Session(engine) as session:
        call = ModelCall(model="fake", latency_ms=1, cached=False, cost_estimate=amount)
        if when:
            call.at = when
        session.add(call)
        session.commit()


def block(engine, reporter_id, value=True):
    with Session(engine) as session:
        reporter = session.get(Reporter, reporter_id)
        trust.set_blocked(session, reporter.pseudonym[:8], value)


def context(settings, engine):
    llm, sam, judge = FakeLLM(), FakeSam(), FakeJudge()
    assessor = AssessmentService(
        settings, engine, llm, sam, IncidentService(settings, engine, judge)
    )
    intake = IntakeService(settings, engine, FakeTranscriber(), convert=one_chunk)
    tasks = []

    def create_task(coro, name=None):
        tasks.append(asyncio.create_task(coro, name=name))

    ctx = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"settings": settings, "intake": intake, "assessor": assessor},
            create_task=create_task,
        )
    )
    return ctx, tasks, llm


@pytest.mark.parametrize("kind", ["text", "photo", "document", "voice", "location"])
async def test_100_messages_only_hour_limit_stored_and_assessed_before_download(
    settings, engine, monkeypatch, kind
):
    ctx, tasks, llm = context(settings, engine)
    downloads = AsyncMock(return_value=jpeg())
    monkeypatch.setattr(bot, "_download", downloads)
    handler = {
        "text": bot.on_text,
        "photo": bot.on_photo,
        "document": bot.on_image_document,
        "voice": bot.on_voice,
        "location": bot.on_location,
    }[kind]
    messages = []
    for _ in range(100):
        fields = {
            "text": {"text": "baha sa Malaban"},
            "photo": {"photo": [object()], "caption": "baha sa Malaban"},
            "document": {
                "document": SimpleNamespace(mime_type="image/jpeg", file_size=10),
                "caption": "baha sa Malaban",
            },
            "voice": {"voice": object()},
            "location": {"location": SimpleNamespace(latitude=14.33, longitude=121.08)},
        }[kind]
        update = make_update(**fields)
        await handler(update, ctx)
        messages.append(update.effective_message)
    await asyncio.gather(*tasks)
    stored = rows(engine, Report)
    assert len(stored) == settings.reports_per_hour
    assert all(r.status == "assessed" for r in stored)
    assert len(tasks) == settings.reports_per_hour
    assert downloads.await_count == (
        settings.reports_per_hour if kind in ("photo", "document", "voice") else 0
    )
    for message in messages[settings.reports_per_hour :]:
        message.reply_text.assert_awaited_once_with(bot.RATE_LIMITED)
    assert "911" in bot.RATE_LIMITED
    assert llm.calls or kind == "location"


async def test_admission_covers_rolling_day_and_hour_and_direct_ingest_is_unlimited(
    settings, engine, monkeypatch
):
    now = utcnow()
    monkeypatch.setattr("app.intake.utcnow", lambda: now)
    settings.reports_per_hour, settings.reports_per_day = 2, 3
    intake = IntakeService(settings, engine)
    assert intake.admit("a") == Admission.OK
    first = await intake.ingest_text("a", "one")
    with Session(engine) as session:
        r = session.get(Report, first.id)
        r.received_at = now - timedelta(hours=2)
        session.add(r)
        session.commit()
    await intake.ingest_text("a", "two")
    assert intake.admit("a") == Admission.OK
    await intake.ingest_text("a", "three")
    assert intake.admit("a") == Admission.RATE_LIMITED
    monkeypatch.setattr("app.intake.utcnow", lambda: now + timedelta(hours=2))
    assert intake.admit("a") == Admission.RATE_LIMITED  # daily cap
    monkeypatch.setattr("app.intake.utcnow", lambda: now + timedelta(days=1, seconds=1))
    assert intake.admit("a") == Admission.OK
    for _ in range(5):
        await intake.ingest_text("a", "direct demo/import bypass")
    assert len(rows(engine, Report)) == 8


async def test_attached_pin_at_limit_is_free_but_standalone_pin_is_limited(settings, engine):
    settings.reports_per_hour = 1
    ctx, tasks, _ = context(settings, engine)
    await bot.on_text(make_update(text="baha sa Malaban"), ctx)
    for expected in (bot.LOCATION_RECEIVED, bot.RATE_LIMITED):
        update = make_update(location=SimpleNamespace(latitude=14.33, longitude=121.08))
        await bot.on_location(update, ctx)
        update.effective_message.reply_text.assert_awaited_once_with(expected)
    await asyncio.gather(*tasks)
    assert len(rows(engine, Report)) == 1
    assert len(tasks) == 2


async def test_concurrent_album_downloads_do_not_overadmit(settings, engine, monkeypatch):
    settings.reports_per_hour = 2
    ctx, tasks, _ = context(settings, engine)

    async def download(_):
        await asyncio.sleep(0)
        return jpeg()

    downloads = AsyncMock(side_effect=download)
    monkeypatch.setattr(bot, "_download", downloads)
    await asyncio.gather(
        *(bot.on_photo(make_update(photo=[object()], caption=None), ctx) for _ in range(12))
    )
    await asyncio.gather(*tasks)
    assert downloads.await_count == 2
    assert len(rows(engine, Report)) == 2


async def test_blocked_photo_is_received_held_and_pending_resumes_after_unblock(
    settings, engine, monkeypatch
):
    ctx, tasks, llm = context(settings, engine)
    first = await ctx.application.bot_data["intake"].ingest_text(42, "before block")
    block(engine, first.reporter_id)
    monkeypatch.setattr(bot, "_download", AsyncMock(return_value=jpeg()))
    update = make_update(photo=[object()], caption="baha sa Malaban")
    await bot.on_photo(update, ctx)
    report = rows(engine, Report)[-1]
    assert report.status == "held" and report.hold_reason == "reporter blocked"
    assert rows(engine, Media)[0].report_id == report.id
    update.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)
    assert not tasks and not llm.calls
    service = ctx.application.bot_data["assessor"]
    assert service.report_ids(pending_only=True) == []
    assert (await service.assess(report.id)).status == "held"  # explicit --all cannot evade
    block(engine, first.reporter_id, False)
    assert report.id in service.report_ids(pending_only=True)
    await run_pending_cli(settings, engine, service, monkeypatch)
    assert rows(engine, Report)[-1].status == "assessed"
    assert rows(engine, Report)[-1].hold_reason is None


async def run_pending_cli(settings, engine, service, monkeypatch):
    monkeypatch.setattr(assessment, "get_settings", lambda: settings)
    monkeypatch.setattr(assessment, "make_engine", lambda _: engine)
    monkeypatch.setattr(assessment, "make_assessor", lambda *a, **kw: service)
    monkeypatch.setattr(
        assessment, "make_transcriber", lambda *a: SimpleNamespace(aclose=AsyncMock())
    )
    service.llm.aclose = AsyncMock()
    service.sam.aclose = AsyncMock()
    assert await assessment._cli(SimpleNamespace(llm=None, report=None, pending=True)) == 0


@pytest.mark.parametrize("resume", ["raise_cap", "expire_window"])
async def test_spend_hold_makes_no_calls_then_pending_cli_resumes(
    settings, engine, monkeypatch, resume
):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_photo("a", jpeg(), caption="baha sa Malaban")
    add_cost(engine, 11)
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    out = await service.assess(report.id)
    assert (out.status, out.hold_reason) == ("held", "spend cap")
    assert not service.llm.calls and not rows(engine, Assessment)
    assert report.id in service.report_ids(pending_only=True)
    if resume == "raise_cap":
        settings.daily_spend_cap_usd = 12
    else:
        with Session(engine) as session:
            call = session.exec(select(ModelCall)).one()
            call.at -= timedelta(days=1, seconds=1)
            session.add(call)
            session.commit()
    await run_pending_cli(settings, engine, service, monkeypatch)
    assert rows(engine, Report)[0].status == "assessed"
    assert rows(engine, Report)[0].hold_reason is None
    assert service.llm.calls


@pytest.mark.parametrize("blocked", [True, False])
async def test_voice_hold_is_before_transcription_and_resumes(settings, engine, blocked):
    transcriber = SimpleNamespace(transcribe=AsyncMock(wraps=FakeTranscriber().transcribe))
    intake = IntakeService(settings, engine, transcriber, convert=one_chunk)
    first = await intake.ingest_text("a", "first")
    if blocked:
        block(engine, first.reporter_id)
    else:
        add_cost(engine, 11)
    report = await intake.ingest_voice("a", b"OggS")
    assert report.status == "held" and report.transcript is None
    transcriber.transcribe.assert_not_awaited()
    block(engine, first.reporter_id, False)
    settings.daily_spend_cap_usd = 12
    service = AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), transcriber=transcriber, convert=one_chunk
    )
    out = await service.assess(report.id)
    assert out.status == "assessed" and out.transcript == "may baha po"
    transcriber.transcribe.assert_awaited_once()


async def test_expired_held_voice_fails_visibly_and_preserves_report(settings, engine):
    add_cost(engine, 11)
    report = await IntakeService(settings, engine).ingest_voice("a", b"OggS")
    # Simulate a file already deleted by the old retention policy while the report was held.
    media = rows(engine, Media)[0]
    (settings.data_dir / media.path).unlink()
    settings.daily_spend_cap_usd = 12
    service = AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), transcriber=FakeTranscriber()
    )
    out = await service.assess(report.id)
    assert out.status == "assessment_failed"
    assert "raw media expired while held" in rows(engine, Assessment)[0].error
    assert len(rows(engine, Report)) == 1


def test_model_spend_exact_window_cache_and_unknown_prices(settings, engine):
    now = datetime(2026, 10, 28, 12)
    add_cost(engine, 100, now - timedelta(days=1))  # expired boundary
    add_cost(engine, 1.5, now - timedelta(hours=23))
    add_cost(engine, 2.5, now)
    add_cost(engine, 300, now + timedelta(seconds=1))  # clock skew isn't current spend
    add_cost(engine, 0, now)
    with Session(engine) as session:
        assert trust.model_spend(session, now) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("reports_per_hour", 0),
        ("reports_per_day", -1),
        ("daily_spend_cap_usd", 0),
        ("daily_spend_cap_usd", float("inf")),
        ("photo_max_age_days", -1),
        ("photo_hash_max_distance", -1),
        ("photo_hash_max_distance", 65),
    ],
)
def test_limit_settings_are_bounded(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_new_limit_environment_names(monkeypatch):
    values = {
        "REPORTS_PER_HOUR": 25,
        "REPORTS_PER_DAY": 70,
        "DAILY_SPEND_CAP_USD": 15.5,
        "PHOTO_MAX_AGE_DAYS": 4,
        "PHOTO_HASH_MAX_DISTANCE": 9,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    settings = Settings(_env_file=None)
    assert all(getattr(settings, name.lower()) == value for name, value in values.items())


def test_block_cli_validates_prefix_and_changes_unique_reporter(
    settings, engine, monkeypatch, capsys
):
    with Session(engine) as session:
        session.add_all(
            [Reporter(pseudonym="abcdef12" + "a" * 56), Reporter(pseudonym="abcdef12" + "b" * 56)]
        )
        session.commit()
    monkeypatch.setattr(trust, "get_settings", lambda: settings)
    for prefix in ("a", "not-a-pseudonym", "12345678", "abcdef12"):
        assert trust.main(["block", prefix]) == 2
    assert not any(r.blocked for r in rows(engine, Reporter))
    assert trust.main(["block", "ABCDEF12A"]) == 0
    assert rows(engine, Reporter)[0].blocked
    assert trust.main(["unblock", "abcdef12a"]) == 0
    assert not rows(engine, Reporter)[0].blocked
    assert "blocked: no -> yes" in capsys.readouterr().out


def test_init_db_upgrades_slice3_schema_with_existing_data(settings):
    engine = make_engine(settings)
    # Exact pre-guard columns on the five affected tables; other tables are create_all's job.
    definitions = {
        "reporter": """id INTEGER PRIMARY KEY, pseudonym VARCHAR NOT NULL,
            created_at DATETIME NOT NULL""",
        "report": """id INTEGER PRIMARY KEY, reporter_id INTEGER NOT NULL, incident_id INTEGER,
            grouping_fingerprint VARCHAR, grouping_error VARCHAR, grouping_needs_officer BOOLEAN,
            channel VARCHAR NOT NULL, received_at DATETIME NOT NULL,
            text VARCHAR, transcript VARCHAR,
            lat FLOAT, lon FLOAT, barangay VARCHAR, status VARCHAR NOT NULL""",
        "media": """id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, kind VARCHAR NOT NULL,
            path VARCHAR NOT NULL, sha256 VARCHAR NOT NULL, created_at DATETIME NOT NULL,
            purged_at DATETIME""",
        "assessment": """id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL,
            llm_model VARCHAR NOT NULL,
            transcript_clean VARCHAR, facts JSON, geo_source VARCHAR NOT NULL, geo_detail VARCHAR,
            error VARCHAR, updated_at DATETIME NOT NULL""",
        "photoassessment": """id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, media_id INTEGER,
            overlay_media_id INTEGER, llm_model VARCHAR NOT NULL, judgment JSON, measurement JSON,
            gps_lat FLOAT, gps_lon FLOAT, error VARCHAR, updated_at DATETIME NOT NULL""",
    }
    with engine.begin() as conn:
        for table, columns in definitions.items():
            conn.exec_driver_sql(f"CREATE TABLE {table} ({columns})")
        conn.exec_driver_sql(
            "INSERT INTO reporter VALUES (1, 'old-pseudonym', '2026-10-28 04:00:00')"
        )
        conn.exec_driver_sql("""INSERT INTO report
            (id, reporter_id, channel, received_at, text, status)
            VALUES (1, 1, 'telegram', '2026-10-28 04:00:00', 'retained emergency', 'assessed')""")
        conn.exec_driver_sql("""INSERT INTO media (id, report_id, kind, path, sha256, created_at)
            VALUES (1, 1, 'photo', 'media/missing.jpg', 'old', '2026-10-28 04:00:00')""")
        conn.exec_driver_sql("""INSERT INTO assessment
            (id, report_id, llm_model, geo_source, updated_at)
            VALUES (1, 1, 'old', 'none', '2026-10-28 04:00:00')""")
        conn.exec_driver_sql("""INSERT INTO photoassessment
            (id, report_id, media_id, llm_model, updated_at)
            VALUES (1, 1, 1, 'old', '2026-10-28 04:00:00')""")
    init_db(engine)
    init_db(engine)
    assert rows(engine, Reporter)[0].blocked is False
    assert rows(engine, Report)[0].text == "retained emergency"
    assert rows(engine, Report)[0].hold_reason is None
    assert rows(engine, Report)[0].assessing_started_at is None
    assert rows(engine, Media)[0].phash is None
    assert rows(engine, Assessment)[0].signals is None
    assert rows(engine, PhotoAssessment)[0].taken_at is None
    assert "ix_media_sha256" in {i["name"] for i in inspect(engine).get_indexes("media")}


async def test_empty_transcription_after_hold_cannot_be_assessed_as_complete(settings, engine):
    from app.meta.transcribe import Transcript

    add_cost(engine, 11)
    report = await IntakeService(settings, engine).ingest_voice("a", b"OggS")
    settings.daily_spend_cap_usd = 12
    transcriber = SimpleNamespace(
        transcribe=AsyncMock(return_value=Transcript(text="", audio_duration_ms=100))
    )
    service = AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), transcriber=transcriber, convert=one_chunk
    )
    out = await service.assess(report.id)
    assert out.status == "assessment_failed"
    assert "voice note not transcribed" in rows(engine, Assessment)[0].error

"""Regression coverage for the slice 3c review. All inputs and model clients are synthetic."""

import logging
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import Session, select

from app import assessment, bot, trust
from app import intake as intake_module
from app.assessment import AssessmentService, GeoResult
from app.config import Settings
from app.db import init_db, make_engine
from app.intake import Admission, IntakeService
from app.models import Assessment, Media, PhotoAssessment, Report, utcnow
from app.retention import purge_expired
from tests.test_abuse_guards import add_cost, block, context, rows
from tests.test_assessment import FakeLLM, FakeSam, jpeg
from tests.test_bot import FakeTranscriber, make_update, one_chunk
from tests.test_geo import CENTRES


@pytest.fixture(autouse=True)
def no_http(respx_mock):
    """An unmatched HTTP call fails instead of reaching a provider."""


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize(
    "reason,age_hours,purged",
    [
        ("reporter blocked", 23, 0),
        ("reporter blocked", 25, 1),
        ("spend cap", 25, 0),
        ("spend cap", 71, 0),
        ("spend cap", 73, 1),
    ],
)
async def test_retention_bounds_held_media(settings, engine, reason, age_hours, purged):
    report = await IntakeService(settings, engine).ingest_photo("a", jpeg(), caption="baha")
    media = rows(engine, Media)[0]
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status, current.hold_reason = "held", reason
        session.add(current)
        session.commit()
    if reason == "reporter blocked":
        block(engine, report.reporter_id)
    later = media.created_at + timedelta(hours=age_hours)
    assert purge_expired(settings, engine, now=later) == purged
    stored = rows(engine, Media)[0]
    assert (stored.purged_at is not None) == bool(purged)
    assert (settings.data_dir / stored.path).exists() == (not purged)
    assert stored.phash == media.phash
    current = rows(engine, Report)[0]
    assert (current.status, current.hold_reason, current.text) == ("held", reason, "baha")


async def test_spend_hold_retention_uses_configured_max_and_resume_records_expiry(settings, engine):
    settings = Settings(_env_file=None, **(settings.model_dump() | {"held_media_max_hours": 48}))
    assert settings.held_media_max_hours == 48
    add_cost(engine, 11)
    report = await IntakeService(settings, engine).ingest_photo(
        "a", jpeg(), caption="baha sa Malaban"
    )
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    assert (await service.assess(report.id)).status == "held"
    media = rows(engine, Media)[0]
    assert purge_expired(settings, engine, now=media.created_at + timedelta(hours=47)) == 0
    assert purge_expired(settings, engine, now=media.created_at + timedelta(hours=49)) == 1
    settings.daily_spend_cap_usd = 12
    resumed = await service.assess(report.id)
    assert resumed.status == "assessment_failed" and resumed.barangay == "Malaban"
    saved = rows(engine, Assessment)[0]
    assert "raw media expired while held" in saved.error
    assert saved.facts is not None


def test_held_media_max_default_and_environment(monkeypatch):
    monkeypatch.delenv("HELD_MEDIA_MAX_HOURS", raising=False)
    assert Settings(_env_file=None).held_media_max_hours == 72
    monkeypatch.setenv("HELD_MEDIA_MAX_HOURS", "96")
    assert Settings(_env_file=None).held_media_max_hours == 96


@pytest.mark.parametrize("value", [0, -1])
def test_held_media_max_rejects_nonpositive_values(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, held_media_max_hours=value)


@pytest.mark.parametrize("held_hours", [23, 24, 25])
def test_held_media_max_cannot_be_shorter_than_normal_ttl(held_hours):
    values = {"raw_media_ttl_hours": 24, "held_media_max_hours": held_hours}
    if held_hours < 24:
        with pytest.raises(
            ValidationError, match="held_media_max_hours must be >= raw_media_ttl_hours"
        ):
            Settings(_env_file=None, **values)
    else:
        assert Settings(_env_file=None, **values).held_media_max_hours == held_hours


async def test_start_discloses_configured_retention_extension(settings, engine):
    settings = Settings(
        _env_file=None, data_dir=settings.data_dir, raw_media_ttl_hours=12, held_media_max_hours=60
    )
    ctx, _, _ = context(settings, engine)
    update = make_update()
    await bot.on_start(update, ctx)
    text = update.effective_message.reply_text.call_args.args[0]
    assert "12 oras" in text and "12 hours" in text
    assert "60 oras" in text and "up to 60 hours if processing is paused" in text


async def test_retention_purges_released_spend_hold_on_normal_ttl(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo("a", jpeg())
    later = utcnow() + timedelta(hours=25)
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status, current.hold_reason = "held", "spend cap"
        session.add(current)
        session.commit()
    assert purge_expired(settings, engine, now=later) == 0
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status, current.hold_reason = "assessed", None
        session.add(current)
        session.commit()
    assert purge_expired(settings, engine, now=later) == 1


@pytest.mark.parametrize("kind", ["photo", "voice"])
@pytest.mark.parametrize("purged", [False, True])
async def test_resumed_missing_raw_media_fails_visibly(settings, engine, kind, purged):
    intake = IntakeService(settings, engine)
    report = await getattr(intake, f"ingest_{kind}")("a", b"synthetic")
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status, current.hold_reason = "held", "spend cap"
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        (settings.data_dir / media.path).unlink()
        if purged:
            media.purged_at = utcnow()
            session.add(media)
        session.add(current)
        session.commit()
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    assert (await service.assess(report.id)).status == "assessment_failed"
    assert "raw media expired while held" in rows(engine, Assessment)[0].error
    # A later --pending retry must not turn the failed hold into an empty assessment.
    assert (await service.assess(report.id)).status == "assessment_failed"
    assert "raw media expired while held" in rows(engine, Assessment)[0].error
    assert not service.llm.calls


@pytest.mark.parametrize("window", ["hour", "day"])
async def test_blocked_reporters_are_limited_before_download(settings, engine, monkeypatch, window):
    settings.reports_per_hour, settings.reports_per_day = (2, 10) if window == "hour" else (10, 2)
    ctx, tasks, _ = context(settings, engine)
    intake = ctx.application.bot_data["intake"]
    report = await intake.ingest_text(42, "before block")
    block(engine, report.reporter_id)
    assert intake.admit(42) == Admission.BLOCKED
    download = AsyncMock(return_value=jpeg())
    monkeypatch.setattr(bot, "_download", download)
    for _ in range(3):
        update = make_update(photo=[object()], caption=None)
        await bot.on_photo(update, ctx)
    assert download.await_count == 1
    assert len(rows(engine, Report)) == 2
    assert len(rows(engine, Media)) == 1
    assert not tasks
    update.effective_message.reply_text.assert_awaited_once_with(bot.RATE_LIMITED)


@pytest.mark.parametrize("status", ["new", "assessed", "assessment_failed"])
async def test_blocked_pin_never_attaches_to_preexisting_report(settings, engine, status):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_text("a", "before block")
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status = status
        session.add(current)
        session.commit()
    block(engine, report.reporter_id)
    snapshot = rows(engine, Report)[0].model_dump()
    attached = await intake.ingest_location("a", *CENTRES["Malaban"])
    assert attached.id != report.id
    assert (attached.status, attached.hold_reason) == ("held", "reporter blocked")
    assert rows(engine, Report)[0].model_dump() == snapshot
    assert not intake.can_attach_location("a")
    standalone = await intake.ingest_location("a", *CENTRES["Malaban"])
    assert standalone.id != report.id
    assert (standalone.status, standalone.hold_reason) == ("held", "reporter blocked")


async def test_blocked_pin_cannot_bypass_report_rate_limit(settings, engine):
    settings.reports_per_hour = 1
    ctx, tasks, _ = context(settings, engine)
    report = await ctx.application.bot_data["intake"].ingest_text(42, "before block")
    block(engine, report.reporter_id)
    update = make_update(location=SimpleNamespace(latitude=14.33, longitude=121.08))
    await bot.on_location(update, ctx)
    update.effective_message.reply_text.assert_awaited_once_with(bot.RATE_LIMITED)
    assert len(rows(engine, Report)) == 1 and not tasks
    assert rows(engine, Report)[0].lat is None


async def test_pin_on_assessed_report_is_pending_across_spend_hold(settings, engine):
    intake = IntakeService(settings, engine)
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    report = await intake.ingest_text("a", "baha sa Casile")
    assert (await service.assess(report.id)).barangay == "Casile"
    add_cost(engine, 11)
    service.llm.calls.clear()
    attached = await intake.ingest_location("a", *CENTRES["Malaban"])
    assert attached.id == report.id and attached.status == "new"
    assert service.report_ids(pending_only=True) == [report.id]
    held = await service.assess(report.id)
    assert (held.status, held.hold_reason) == ("held", "spend cap")
    assert not service.llm.calls
    settings.daily_spend_cap_usd = 12
    for report_id in service.report_ids(pending_only=True):
        resumed = await service.assess(report_id)
    assert resumed.status == "assessed" and resumed.barangay == "Malaban"
    assert rows(engine, Assessment)[0].geo_source == "pin"
    assert service.report_ids(pending_only=True) == []


@pytest.mark.parametrize("source", ["text", "caption", "transcript", "remaining_photo"])
@pytest.mark.parametrize("purged", [False, True])
async def test_missing_held_media_keeps_available_evidence(settings, engine, source, purged):
    intake = IntakeService(settings, engine)
    if source == "text":
        report = await intake.ingest_text("a", "baha sa Malaban")
    else:
        report = await intake.ingest_photo(
            "a", jpeg(), caption="baha sa Malaban" if source == "caption" else None
        )
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status, current.hold_reason = "held", "spend cap"
        if source == "transcript":
            current.transcript = "baha sa Malaban"
        # Missing audio alongside text; otherwise lose the original photo.
        if source == "text":
            intake._store_media(session, current, "voice", b"OggS", "ogg")
        session.add(current)
        session.commit()
        missing = session.exec(select(Media).where(Media.report_id == report.id)).one()
        (settings.data_dir / missing.path).unlink()
        if purged:
            missing.purged_at = utcnow()
            session.add(missing)
        if source == "remaining_photo":
            intake._store_media(session, current, "photo", jpeg((800, 400)), "jpg")
        session.commit()
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    for _ in range(2):  # pending retries retain both the error and the surviving evidence
        out = await service.assess(report.id)
        assert out.status == "assessment_failed"
        saved = rows(engine, Assessment)[0]
        assert "raw media expired while held" in saved.error
        if source == "remaining_photo":
            assert rows(engine, PhotoAssessment)[0].judgment is not None
        else:
            assert saved.facts is not None and out.barangay == "Malaban"
            if source == "transcript":
                assert saved.transcript_clean == "baha sa Malaban"
    assert service.llm.calls


async def test_retention_skips_resumed_voice_while_assessing(settings, engine):
    add_cost(engine, 11)
    report = await IntakeService(settings, engine).ingest_voice("a", b"OggS")
    assert report.status == "held"
    media = rows(engine, Media)[0]
    with Session(engine) as session:
        current = session.get(Media, media.id)
        current.created_at = utcnow() - timedelta(hours=100)
        session.add(current)
        session.commit()
    settings.daily_spend_cap_usd = 12
    checked = []

    async def convert(source, out_dir):
        current = rows(engine, Report)[0]
        assert current.status == "assessing"
        assert current.assessing_started_at is not None
        assert utcnow() - current.assessing_started_at < timedelta(minutes=1)
        checked.append(purge_expired(settings, engine))
        assert source.read_bytes() == b"OggS"
        return await one_chunk(source, out_dir)

    service = AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), transcriber=FakeTranscriber(), convert=convert
    )
    out = await service.assess(report.id)
    assert checked == [0]
    assert out.status == "assessed" and out.transcript == "may baha po"
    assert purge_expired(settings, engine) == 1


@pytest.mark.parametrize("selection", ["all", "report"])
async def test_spend_cap_skips_assessed_reports(settings, engine, monkeypatch, caplog, selection):
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    report = await IntakeService(settings, engine).ingest_text("a", "baha sa Malaban")
    before = await service.assess(report.id)
    snapshot = before.model_dump()
    saved = rows(engine, Assessment)[0].model_dump()
    service.llm.calls.clear()
    add_cost(engine, 11)
    monkeypatch.setattr(assessment, "get_settings", lambda: settings)
    monkeypatch.setattr(assessment, "make_engine", lambda _: engine)
    monkeypatch.setattr(assessment, "make_assessor", lambda *a, **kw: service)
    monkeypatch.setattr(
        assessment, "make_transcriber", lambda *a: SimpleNamespace(aclose=AsyncMock())
    )
    for client in (service.llm, service.sam, service.incidents.judge):
        client.aclose = AsyncMock()
    with caplog.at_level(logging.WARNING, logger="app.assessment"):
        assert (
            await assessment._cli(
                SimpleNamespace(
                    llm=None, report=report.id if selection == "report" else None, pending=False
                )
            )
            == 0
        )
    assert rows(engine, Report)[0].model_dump() == snapshot
    assert rows(engine, Assessment)[0].model_dump() == saved
    assert not service.llm.calls
    assert "skip" in caplog.text.lower() and "spend cap" in caplog.text.lower()


@pytest.mark.parametrize(
    "raw,clean,mismatch",
    [("dito sa Malaban", "dito sa Casile", True), ("dito sa Casile", "dito sa Malaban", False)],
)
async def test_mismatch_uses_the_same_clean_transcript_as_geolocation(
    settings, engine, monkeypatch, raw, clean, mismatch
):
    report = await IntakeService(settings, engine).ingest_location("a", *CENTRES["Malaban"])
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.transcript = raw
        session.add(current)
        session.commit()
    monkeypatch.setattr(assessment, "clean_transcript", lambda _: clean)
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    await service.assess(report.id)
    saved = rows(engine, Assessment)[0]
    assert saved.transcript_clean == clean and saved.geo_source == "pin"
    assert any(s["code"] == "pin_text_mismatch" for s in saved.signals) == mismatch


def probe_write(engine):
    """Another SQLite writer must be able to run during hashing/file reads."""
    with sqlite3.connect(engine.url.database, timeout=0) as connection:
        connection.execute(
            "INSERT INTO modelcall (at, model, latency_ms, cached, cost_estimate) "
            "VALUES ('2026-10-28', 'lock probe', 0, 1, 0)"
        )
        connection.rollback()


async def test_intake_hash_runs_off_loop_before_sqlite_write(settings, engine, monkeypatch):
    original = intake_module.photo_hash
    observed = []

    def checked(data):
        observed.append(threading.current_thread() is not threading.main_thread())
        probe_write(engine)
        return original(data)

    monkeypatch.setattr(intake_module, "photo_hash", checked)
    await IntakeService(settings, engine).ingest_photo("a", jpeg())
    assert observed == [True]


async def test_lazy_hash_reads_and_decodes_before_save_write(settings, engine, monkeypatch):
    report = await IntakeService(settings, engine).ingest_photo("a", jpeg())
    with Session(engine) as session:
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        media.phash = None
        session.add(media)
        session.commit()
        path = settings.data_dir / media.path
    original_read, original_hash = Path.read_bytes, trust.photo_hash
    reads, hashes = [], []

    def checked_read(self):
        if self == path:
            probe_write(engine)
            reads.append(self)
        return original_read(self)

    def checked_hash(data):
        probe_write(engine)
        hashes.append(data)
        return original_hash(data)

    monkeypatch.setattr(Path, "read_bytes", checked_read)
    monkeypatch.setattr(trust, "photo_hash", checked_hash)
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    service._save(report.id, None, None, GeoResult(None, "none"), [], [])
    assert len(reads) == len(hashes) == 1
    assert rows(engine, Media)[0].phash is not None


@pytest.mark.parametrize("missing", [False, True])
async def test_unhashable_legacy_photo_is_not_retried(settings, engine, monkeypatch, missing):
    report = await IntakeService(settings, engine).ingest_photo("a", b"invalid photo")
    with Session(engine) as session:
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        media.phash = None
        session.add(media)
        session.commit()
        path = settings.data_dir / media.path
    if missing:
        path.unlink()
    reads = []
    original = Path.read_bytes

    def counted(self):
        if self == path:
            reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counted)
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    service._save(report.id, None, None, GeoResult(None, "none"), [], [])
    assert rows(engine, Media)[0].phash is not None
    reads.clear()
    service._save(report.id, None, None, GeoResult(None, "none"), [], [])
    assert not reads


async def test_comparison_uses_indexed_sha_and_only_projected_nonnull_phashes(settings, engine):
    intake = IntakeService(settings, engine)
    await intake.ingest_photo("a", jpeg())
    other = await intake.ingest_photo("b", jpeg())
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session:
            report = session.get(Report, other.id)
            signals = trust.compute_signals(session, report, settings)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert signals[0]["code"] == "photo_reused"
    comparisons = [q for q in queries if "from media" in q and "media.report_id =" not in q]
    assert any("media.sha256 =" in q for q in comparisons)
    phashes = [q for q in comparisons if "media.phash is not null" in q]
    assert phashes
    assert all(
        q.startswith("select media.id, media.report_id, media.phash from media") for q in phashes
    )
    assert not any("media.path" in q for q in comparisons)


@pytest.mark.parametrize(
    "actions,expected",
    [
        (["block"], "no -> yes"),
        (["block", "unblock"], "yes -> no"),
        (["block", "block"], "yes -> yes"),
    ],
)
async def test_block_cli_prints_previous_and_new_state(
    settings, engine, monkeypatch, capsys, actions, expected
):
    report = await IntakeService(settings, engine).ingest_text("a", "synthetic")
    from app.models import Reporter

    with Session(engine) as session:
        prefix = session.get(Reporter, report.reporter_id).pseudonym[:8]
    monkeypatch.setattr(trust, "get_settings", lambda: settings)
    for action in actions:
        capsys.readouterr()
        assert trust.main([action, prefix]) == 0
    assert f"blocked: {expected}" in capsys.readouterr().out

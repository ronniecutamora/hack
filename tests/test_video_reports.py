"""Video and audio file intake shares the ordinary report pipeline."""

from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app import bot
from app.db import init_db, make_engine
from app.models import Media, Report
from tests.test_bot import FakeTranscriber, make_context, make_update


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


async def test_video_size_refusal_stores_nothing(settings, engine):
    video = SimpleNamespace(file_size=bot.MAX_DOWNLOAD_BYTES + 1, duration=3)
    update = make_update(video=video, caption=None)
    await bot.on_video(update, make_context(settings, engine, FakeTranscriber()))
    assert "short" in update.effective_message.reply_text.call_args.args[0].lower()
    with Session(engine) as session:
        assert not session.exec(select(Report)).all()
        assert not session.exec(select(Media)).all()


def test_all_video_and_audio_handlers_are_registered(settings):
    from pydantic import SecretStr

    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123456:ABC-test")})
    app = bot.build_application(configured)
    callbacks = {handler.callback for handler in app.handlers[0]}
    assert bot.on_video in callbacks
    assert bot.on_audio in callbacks


@pytest.fixture
def clip(tmp_path):
    """A tiny synthetic clip with an audio stream; no network or personal data."""
    import subprocess

    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture
def silent_clip(tmp_path):
    import subprocess

    path = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=8",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def rows(engine, model):
    with Session(engine) as session:
        return list(session.exec(select(model).order_by(model.id)).all())


@pytest.mark.parametrize(
    ("kind", "mime", "expected"),
    [
        ("video", None, "on_video"),
        ("video_note", None, "on_video"),
        ("document", "video/mp4", "on_video"),
        ("audio", None, "on_audio"),
        ("document", "audio/mpeg", "on_audio"),
    ],
)
def test_telegram_filters_route_each_message_type(settings, kind, mime, expected):
    from pydantic import SecretStr
    from telegram import Update

    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123456:ABC-test")})
    application = bot.build_application(configured)
    media = {"file_id": "file", "file_unique_id": "unique", "duration": 3}
    if kind == "video":
        media.update(width=160, height=90)
    if kind == "video_note":
        media["length"] = 90
    if mime:
        media.update(mime_type=mime, file_name="clip")
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1_790_000_000,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Reporter"},
            kind: media,
        },
    }
    update = Update.de_json(payload, application.bot)
    matched = [h.callback for h in application.handlers[0] if h.check_update(update)]
    assert matched == [getattr(bot, expected)]


async def test_video_runs_normal_agent_and_deletes_raw_audio(settings, engine, clip):
    from app.agent_trace import report_traces
    from app.assessment import AssessmentService
    from app.db import make_recorder
    from app.gazetteer import transcription_keywords
    from app.intake import LANGUAGE_BIAS, IntakeService
    from app.meta.base import CallRecord
    from app.meta.transcribe import Transcript
    from app.models import PhotoAssessment
    from tests.test_assessment import FakeLLM
    from tests.test_trace import RecordingSam

    recorder = make_recorder(engine)

    class RecordingTranscriber:
        calls = []

        async def transcribe(self, wav, *, language_bias=None, keywords=None):
            self.calls.append((wav, language_bias, keywords))
            recorder(CallRecord("muse-voice-transcribe-1.0", 5, False, 0.001))
            return Transcript(text="baha sa Malaban", audio_duration_ms=3000)

    transcriber = RecordingTranscriber()
    intake = IntakeService(settings, engine, transcriber)
    report = await intake.ingest_av(42, clip, video=True, caption="Baha po", album="album")
    media = rows(engine, Media)
    assert len([m for m in media if m.kind == "photo"]) == 3
    assert len([m for m in media if m.kind == "audio"]) == 1
    assert all(m.frame_label and "video" in m.frame_label for m in media if m.kind == "photo")
    assert not list(settings.media_dir.glob("tmp-*"))
    assert not list(settings.media_dir.rglob("*.mp4"))

    llm = FakeLLM(recorder=recorder)
    out = await AssessmentService(
        settings, engine, llm, RecordingSam(recorder), transcriber=transcriber
    ).assess(report.id)
    assert out.status == "assessed"
    assert out.transcript == "baha sa Malaban"
    assert transcriber.calls[0][1:] == (LANGUAGE_BIAS, transcription_keywords())
    assert len(rows(engine, PhotoAssessment)) == 3
    assert len([call for call in llm.calls if call.get("image")]) == 3
    (audio,) = [m for m in rows(engine, Media) if m.kind == "audio"]
    assert audio.audio_done and audio.purged_at is not None
    assert not (settings.data_dir / audio.path).exists()
    with Session(engine) as session:
        trace = report_traces(session, [session.get(Report, report.id)])[report.id]
    labels = [step.label for step in trace.steps]
    for label in ("Extract video audio", "Transcribe the audio", "Extract video keyframes"):
        assert label in labels
    assert any("Judge photo damage" in label for label in labels)
    assert any("Find floodwater (SAM)" in label for label in labels)


async def test_silent_video_skips_transcription(settings, engine, silent_clip):
    from app.assessment import AssessmentService
    from app.intake import IntakeService
    from tests.test_assessment import FakeLLM, FakeSam
    from tests.test_intake import FakeTranscriber

    transcriber = FakeTranscriber()
    report = await IntakeService(settings, engine, transcriber).ingest_av(
        42, silent_clip, video=True, caption="Baha sa Malaban"
    )
    assert not [m for m in rows(engine, Media) if m.kind == "audio"]
    out = await AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), transcriber=transcriber
    ).assess(report.id)
    assert out.status == "assessed"
    assert not transcriber.calls


async def test_duration_probe_refuses_forged_metadata_before_storage(
    settings, engine, clip, monkeypatch
):
    from unittest.mock import AsyncMock

    settings.video_max_seconds = 1
    monkeypatch.setattr(bot, "_download", AsyncMock(return_value=clip))
    update = make_update(video=SimpleNamespace(file_size=100, duration=0), caption=None)
    await bot.on_video(update, make_context(settings, engine, FakeTranscriber()))
    assert "shorter" in update.effective_message.reply_text.call_args.args[0]
    assert not rows(engine, Report)
    assert not rows(engine, Media)
    assert not list(settings.media_dir.glob("tmp-*"))


async def test_album_and_late_caption_join_the_same_video_report(settings, engine, silent_clip):
    from app.intake import IntakeService
    from tests.test_assessment import jpeg

    intake = IntakeService(settings, engine)
    photo = await intake.ingest_photo(42, jpeg(), album="album")
    video = await intake.ingest_av(42, silent_clip, video=True, album="album")
    assert photo.id == video.id
    assert len([m for m in rows(engine, Media) if m.kind == "photo"]) == 4
    assert (await intake.attach_caption(42, "Baha sa Malaban")).id == video.id
    assert (await intake.ingest_location(42, 14.33, 121.08)).id == video.id


async def test_video_rate_limit_and_spend_hold_prevent_model_calls(
    settings, engine, clip, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.assessment import AssessmentService
    from app.intake import IntakeService
    from app.models import ModelCall
    from tests.test_assessment import FakeLLM, FakeSam
    from tests.test_intake import FakeTranscriber

    settings.reports_per_hour = 1
    intake = IntakeService(settings, engine)
    await intake.ingest_text(42, "Baha sa Malaban")
    monkeypatch.setattr(bot, "_download", AsyncMock(return_value=clip))
    update = make_update(video=SimpleNamespace(file_size=100, duration=3), caption=None)
    await bot.on_video(update, make_context(settings, engine, FakeTranscriber()))
    update.effective_message.reply_text.assert_awaited_once_with(bot.RATE_LIMITED)
    assert bot._download.await_count == 0

    with Session(engine) as session:
        session.add(ModelCall(model="fake", latency_ms=1, cached=False, cost_estimate=11))
        session.commit()
    report = await intake.ingest_av(43, clip, video=True)
    assert report.status == "held" and report.hold_reason == "spend cap"
    transcriber, llm = FakeTranscriber(), FakeLLM()
    out = await AssessmentService(settings, engine, llm, FakeSam(), transcriber=transcriber).assess(
        report.id
    )
    assert out.status == "held" and not transcriber.calls and not llm.calls
    (audio,) = [m for m in rows(engine, Media) if m.kind == "audio"]
    assert (settings.data_dir / audio.path).is_file()  # retained only for the existing hold window


@pytest.mark.parametrize(
    "kind", ["video", "video_note", "video_document", "audio", "audio_document"]
)
async def test_each_handler_stores_and_acknowledges(settings, engine, clip, monkeypatch, kind):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(bot, "_download", AsyncMock(return_value=clip))
    media = SimpleNamespace(file_size=len(clip), duration=3)
    field = {"video_document": "document", "audio_document": "document"}.get(kind, kind)
    update = make_update(**{field: media}, caption="Baha sa Malaban")
    handler = bot.on_video if kind.startswith("video") else bot.on_audio
    await handler(update, make_context(settings, engine, FakeTranscriber()))
    update.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)
    (report,) = rows(engine, Report)
    assert report.text == "Baha sa Malaban"
    assert any(
        m.kind == ("photo" if kind.startswith("video") else "audio") for m in rows(engine, Media)
    )


async def test_blocked_reporter_video_is_held_without_transcription(settings, engine, clip):
    from app.assessment import AssessmentService
    from app.intake import IntakeService
    from app.models import Reporter
    from tests.test_assessment import FakeLLM, FakeSam
    from tests.test_intake import FakeTranscriber

    intake = IntakeService(settings, engine)
    first = await intake.ingest_text(42, "Baha sa Malaban")
    with Session(engine) as session:
        reporter = session.get(Reporter, first.reporter_id)
        reporter.blocked = True
        session.add(reporter)
        session.commit()
    report = await intake.ingest_av(42, clip, video=True)
    assert report.status == "held" and report.hold_reason == "reporter blocked"
    transcriber, llm = FakeTranscriber(), FakeLLM()
    await AssessmentService(settings, engine, llm, FakeSam(), transcriber=transcriber).assess(
        report.id
    )
    assert not transcriber.calls and not llm.calls


async def test_audio_file_transcribes_and_raw_audio_is_deleted(settings, engine, clip):
    from app.assessment import AssessmentService
    from app.intake import IntakeService
    from tests.test_assessment import FakeLLM, FakeSam
    from tests.test_intake import FakeTranscriber

    transcriber = FakeTranscriber()
    report = await IntakeService(settings, engine, transcriber).ingest_av(42, clip, video=False)
    out = await AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), transcriber=transcriber
    ).assess(report.id)
    assert out.status == "assessed" and out.transcript == "Bumaha na po dito sa amin"
    (audio,) = [m for m in rows(engine, Media) if m.kind == "audio"]
    assert audio.audio_done and audio.purged_at and not (settings.data_dir / audio.path).exists()


async def test_video_frames_use_photo_reuse_signal(settings, engine, silent_clip):
    from app.assessment import AssessmentService
    from app.intake import IntakeService
    from app.models import Assessment
    from tests.test_assessment import FakeLLM, FakeSam

    intake = IntakeService(settings, engine)
    await intake.ingest_av(1, silent_clip, video=True, caption="Baha sa Malaban")
    second = await intake.ingest_av(2, silent_clip, video=True, caption="Baha sa Malaban")
    await AssessmentService(settings, engine, FakeLLM(), FakeSam()).assess(second.id)
    (assessment,) = rows(engine, Assessment)
    assert any(signal["code"] == "photo_reused" for signal in assessment.signals)


async def test_video_with_silent_audio_track_skips_transcription(settings, engine, tmp_path):
    import subprocess

    from app.intake import IntakeService

    path = tmp_path / "silent-track.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=8",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
    )
    await IntakeService(settings, engine).ingest_av(42, path.read_bytes(), video=True)
    assert len([m for m in rows(engine, Media) if m.kind == "photo"]) == 3
    assert not [m for m in rows(engine, Media) if m.kind == "audio"]

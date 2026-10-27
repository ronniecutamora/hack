from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select
from telegram import Update
from telegram.error import BadRequest, TimedOut

from app import bot
from app.config import Settings
from app.db import init_db, make_engine
from app.intake import IntakeService
from app.meta.transcribe import Transcript
from app.models import Media, Report


class Downloadable:
    def __init__(self, data: bytes):
        self.data = data

    async def get_file(self):
        return SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(self.data)))


class FakeTranscriber:
    async def transcribe(self, wav, *, language_bias=None, keywords=None):
        return Transcript(text="may baha po", audio_duration_ms=500)


async def one_chunk(source, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunk_000.wav").write_bytes(b"RIFF")
    return [out_dir / "chunk_000.wav"]


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(bot, "TELEGRAM_RETRY_DELAYS_S", (0, 0))


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    return engine


def make_update(**message_fields):
    message = SimpleNamespace(reply_text=AsyncMock(), **message_fields)
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))


def make_context(settings, engine, transcriber=None):
    intake = IntakeService(settings, engine, transcriber, convert=one_chunk)
    data = {"intake": intake, "settings": settings}
    return SimpleNamespace(application=SimpleNamespace(bot_data=data))


def reports(engine):
    with Session(engine) as session:
        return session.exec(select(Report)).all()


async def test_start_explains_privacy_in_taglish_and_english(settings, engine):
    update = make_update()
    await bot.on_start(update, make_context(settings, engine))
    text = update.effective_message.reply_text.call_args.args[0]
    assert "Kumusta" in text and "Hello" in text
    assert "24 oras" in text and "24 hours" in text
    assert "pseudonymous" in text


async def test_text_message_becomes_a_report(settings, engine):
    update = make_update(text="Baha na sa amin")
    await bot.on_text(update, make_context(settings, engine))
    assert [r.text for r in reports(engine)] == ["Baha na sa amin"]
    update.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)


async def test_photo_uses_largest_size_and_caption(settings, engine):
    small, large = Downloadable(b"small"), Downloadable(b"large")
    update = make_update(photo=[small, large], caption="Bubong")
    await bot.on_photo(update, make_context(settings, engine))
    (report,) = reports(engine)
    assert report.text == "Bubong"
    media = next((settings.data_dir / "media").rglob("*.jpg"))
    assert media.read_bytes() == b"large"


async def test_voice_note_is_transcribed(settings, engine):
    update = make_update(voice=Downloadable(b"OggS"))
    await bot.on_voice(update, make_context(settings, engine, FakeTranscriber()))
    (report,) = reports(engine)
    assert report.transcript == "may baha po"
    update.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)


async def test_voice_without_transcriber_tells_the_user(settings, engine):
    update = make_update(voice=Downloadable(b"OggS"))
    await bot.on_voice(update, make_context(settings, engine, None))
    update.effective_message.reply_text.assert_awaited_once_with(bot.VOICE_NOT_TRANSCRIBED)


async def test_location_message(settings, engine):
    update = make_update(location=SimpleNamespace(latitude=14.33, longitude=121.08))
    await bot.on_location(update, make_context(settings, engine))
    (report,) = reports(engine)
    assert (report.lat, report.lon) == (14.33, 121.08)


def test_main_explains_missing_token(monkeypatch, capsys):
    monkeypatch.setattr(bot, "get_settings", lambda: Settings(_env_file=None))
    assert bot.main() == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err


def test_application_registers_all_handlers(settings):
    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123456:ABC-test")})
    application = bot.build_application(configured)
    assert len(application.handlers[0]) == 6
    assert application.bot_data["assessor"].llm.model == "muse-spark-1.3"


async def test_voice_download_retries_a_flaky_connection(settings, engine):
    # Regression (2026-10-27): a slow TLS handshake to Telegram failed the voice download.
    voice = Downloadable(b"OggS")
    get_file = voice.get_file
    voice.get_file = AsyncMock(side_effect=[TimedOut(), TimedOut(), await get_file()])
    update = make_update(voice=voice)

    await bot.on_voice(update, make_context(settings, engine, FakeTranscriber()))

    assert voice.get_file.await_count == 3
    (report,) = reports(engine)
    assert report.transcript == "may baha po"


async def test_bad_request_is_not_retried():
    call = AsyncMock(side_effect=BadRequest("message to reply not found"))
    with pytest.raises(BadRequest):
        await bot._with_retries(call)
    assert call.await_count == 1


async def test_error_reply_failure_is_logged_not_raised(settings, engine, caplog):
    update = Update(update_id=1)
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=TimedOut()))
    context = SimpleNamespace(error=RuntimeError("boom"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Update, "effective_message", property(lambda self: message))
        await bot.on_error(update, context)

    assert message.reply_text.await_count == 1 + len(bot.TELEGRAM_RETRY_DELAYS_S)
    message.reply_text.assert_awaited_with(bot.ERROR_REPLY)
    assert "could not send the error reply" in caplog.text


class FakeAssessor:
    def __init__(self):
        self.assessed = []

    async def assess(self, report_id):
        self.assessed.append(report_id)


def context_with_assessor(settings, engine):
    context = make_context(settings, engine, FakeTranscriber())
    assessor = FakeAssessor()
    tasks = []

    def create_task(coroutine, name=None):
        tasks.append(name)
        coroutine.close()  # the real app runs it in the background; here we only record it
        assessor.assessed.append(name)

    context.application.bot_data["assessor"] = assessor
    context.application.create_task = create_task
    return context, assessor


def image_document(mime, size=1000, data=b"\xff\xd8jpeg"):
    document = Downloadable(data)
    document.mime_type = mime
    document.file_size = size
    return document


async def test_every_report_is_queued_for_assessment_after_the_reply(settings, engine):
    context, assessor = context_with_assessor(settings, engine)
    await bot.on_text(make_update(text="baha sa Casile"), context)
    await bot.on_photo(make_update(photo=[Downloadable(b"jpg")], caption=None), context)
    await bot.on_voice(make_update(voice=Downloadable(b"OggS")), context)
    location = SimpleNamespace(latitude=14.3383, longitude=121.0843)
    await bot.on_location(make_update(location=location), context)
    ids = [r.id for r in reports(engine)]
    assert assessor.assessed == [f"assess-{i}" for i in (ids[0], ids[1], ids[2], ids[2])]


async def test_image_sent_as_a_file_is_stored_with_its_type(settings, engine):
    context, assessor = context_with_assessor(settings, engine)
    update = make_update(document=image_document("image/png"), caption="Brgy. Loma")
    await bot.on_image_document(update, context)
    (report,) = reports(engine)
    assert report.text == "Brgy. Loma"
    update.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)
    assert assessor.assessed == [f"assess-{report.id}"]
    with Session(engine) as session:
        (media,) = session.exec(select(Media)).all()
    assert media.path.endswith(".png")


@pytest.mark.parametrize(
    ("document", "reply"),
    [
        (image_document("image/heic"), bot.UNSUPPORTED_IMAGE),
        (image_document("image/jpeg", size=25 * 1024 * 1024), bot.TOO_LARGE),
    ],
)
async def test_unreadable_or_huge_files_are_refused_politely(settings, engine, document, reply):
    context, assessor = context_with_assessor(settings, engine)
    update = make_update(document=document, caption=None)
    await bot.on_image_document(update, context)
    assert reports(engine) == []
    assert assessor.assessed == []
    update.effective_message.reply_text.assert_awaited_once_with(reply)

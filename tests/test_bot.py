from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select
from telegram import Update
from telegram.error import TimedOut

from app import bot
from app.config import Settings
from app.db import init_db, make_engine
from app.intake import IntakeService
from app.meta.transcribe import Transcript
from app.models import Report


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
    assert len(application.handlers[0]) == 5


async def test_error_reply_failure_is_logged_not_raised(settings, engine, caplog):
    update = Update(update_id=1)
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=TimedOut()))
    context = SimpleNamespace(error=RuntimeError("boom"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Update, "effective_message", property(lambda self: message))
        await bot.on_error(update, context)

    message.reply_text.assert_awaited_once_with(bot.ERROR_REPLY)
    assert "could not send the error reply" in caplog.text

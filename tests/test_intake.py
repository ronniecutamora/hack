import asyncio
import shutil
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.audio import AudioConversionError, to_wav_chunks
from app.db import init_db, make_engine, make_recorder
from app.gazetteer import transcription_keywords
from app.intake import LANGUAGE_BIAS, IntakeService
from app.meta.base import CallRecord, ModelApiError
from app.meta.transcribe import Transcript
from app.models import Media, ModelCall, Report, Reporter
from app.privacy import MissingSaltError, pseudonymize

TELEGRAM_USER_ID = 123456789


class FakeTranscriber:
    def __init__(self, texts=("Bumaha na po dito sa amin",), error=None):
        self.texts = list(texts)
        self.error = error
        self.calls = []

    async def transcribe(self, wav, *, language_bias=None, keywords=None):
        self.calls.append({"wav": wav, "language_bias": language_bias, "keywords": keywords})
        if self.error:
            raise self.error
        return Transcript(text=self.texts.pop(0), audio_duration_ms=1000)


async def fake_convert(source, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for i in range(2):
        chunk = out_dir / f"chunk_{i:03d}.wav"
        chunk.write_bytes(b"RIFF" + bytes([i]))
        chunks.append(chunk)
    return chunks


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    return engine


def rows(engine, model):
    with Session(engine) as session:
        return session.exec(select(model)).all()


def test_pseudonym_is_stable_salted_and_hides_the_id():
    a = pseudonymize("salt", TELEGRAM_USER_ID)
    assert a == pseudonymize("salt", str(TELEGRAM_USER_ID))
    assert a != pseudonymize("other-salt", TELEGRAM_USER_ID)
    assert a != pseudonymize("salt", TELEGRAM_USER_ID + 1)
    assert len(a) == 64
    assert str(TELEGRAM_USER_ID) not in a


def test_empty_salt_is_refused():
    with pytest.raises(MissingSaltError):
        pseudonymize("", TELEGRAM_USER_ID)


async def test_text_intake_stores_pseudonym_only(settings, engine):
    intake = IntakeService(settings, engine)

    first = await intake.ingest_text(TELEGRAM_USER_ID, "  Baha na sa Brgy. San Antonio  ")
    second = await intake.ingest_text(TELEGRAM_USER_ID, "Putol na ang puno")

    assert first.text == "Baha na sa Brgy. San Antonio"
    assert first.channel == "telegram"
    assert first.reporter_id == second.reporter_id
    (reporter,) = rows(engine, Reporter)
    assert reporter.pseudonym == pseudonymize("test-salt", TELEGRAM_USER_ID)
    # the raw id must not appear anywhere in the database file
    assert str(TELEGRAM_USER_ID).encode() not in settings.db_path.read_bytes()


async def test_empty_text_is_rejected(settings, engine):
    with pytest.raises(ValueError):
        await IntakeService(settings, engine).ingest_text(1, "   ")


async def test_photo_intake_writes_file_and_media_row(settings, engine):
    intake = IntakeService(settings, engine)

    report = await intake.ingest_photo(TELEGRAM_USER_ID, b"\xff\xd8fakejpeg", caption="Bubong")

    assert report.text == "Bubong"
    (media,) = rows(engine, Media)
    assert media.kind == "photo"
    assert media.report_id == report.id
    assert not media.path.startswith("/")  # stored relative to DATA_DIR
    assert (settings.data_dir / media.path).read_bytes() == b"\xff\xd8fakejpeg"
    assert len(media.sha256) == 64
    assert media.purged_at is None


async def test_voice_intake_converts_transcribes_and_joins_chunks(settings, engine):
    transcriber = FakeTranscriber(texts=["unang bahagi", "ikalawang bahagi"])
    intake = IntakeService(settings, engine, transcriber, convert=fake_convert)

    report = await intake.ingest_voice(TELEGRAM_USER_ID, b"OggS-fake")

    assert report.transcript == "unang bahagi ikalawang bahagi"
    assert report.status == "new"
    assert [c["language_bias"] for c in transcriber.calls] == [LANGUAGE_BIAS] * 2
    assert all(c["keywords"] == transcription_keywords() for c in transcriber.calls)
    assert "De La Paz" in transcriber.calls[0]["keywords"]
    assert [c["wav"] for c in transcriber.calls] == [b"RIFF\x00", b"RIFF\x01"]
    (media,) = rows(engine, Media)
    assert media.kind == "voice"
    assert (settings.data_dir / media.path).suffix == ".ogg"


class RecordingTranscriber(FakeTranscriber):
    """Logs each call through the real DB recorder, as the production clients do."""

    def __init__(self, engine, seen_reports):
        super().__init__(texts=["unang bahagi", "ikalawang bahagi"])
        self.record = make_recorder(engine)
        self.engine = engine
        self.seen_reports = seen_reports

    async def transcribe(self, wav, *, language_bias=None, keywords=None):
        self.seen_reports.append(len(rows(self.engine, Report)))
        self.record(CallRecord("muse-voice-transcribe-1.0", 10, False, 0.0))
        return await super().transcribe(wav, language_bias=language_bias, keywords=keywords)


async def test_voice_intake_holds_no_db_lock_while_transcribing(settings, engine):
    # Regression (2026-10-27): the open report transaction locked SQLite, so the model-call
    # log write failed with "database is locked" and every voice note was lost.
    seen_reports = []
    intake = IntakeService(
        settings, engine, RecordingTranscriber(engine, seen_reports), convert=fake_convert
    )

    report = await intake.ingest_voice(TELEGRAM_USER_ID, b"OggS-fake")

    assert report.transcript == "unang bahagi ikalawang bahagi"
    assert seen_reports == [1, 1]  # the report is committed (visible to the web app) first
    assert len(rows(engine, ModelCall)) == 2
    (stored,) = rows(engine, Report)
    assert stored.transcript == report.transcript


@pytest.mark.parametrize(
    "error", [ModelApiError("Voice Transcribe", "down", status=503), AudioConversionError("bad")]
)
async def test_voice_failure_still_saves_the_report(settings, engine, error):
    intake = IntakeService(settings, engine, FakeTranscriber(error=error), convert=fake_convert)

    report = await intake.ingest_voice(TELEGRAM_USER_ID, b"OggS-fake")

    assert report.status == "transcription_failed"
    assert report.transcript is None
    assert len(rows(engine, Media)) == 1


async def test_location_attaches_to_recent_report_else_creates_one(settings, engine):
    intake = IntakeService(settings, engine)
    text_report = await intake.ingest_text(TELEGRAM_USER_ID, "Baha dito")

    pinned = await intake.ingest_location(TELEGRAM_USER_ID, 14.3306, 121.0807)
    assert pinned.id == text_report.id
    assert (pinned.lat, pinned.lon) == (14.3306, 121.0807)

    standalone = await intake.ingest_location(TELEGRAM_USER_ID, 14.34, 121.08)
    assert standalone.id != text_report.id
    assert standalone.text is None
    assert len(rows(engine, Report)) == 2


async def test_old_report_does_not_absorb_a_new_location(settings, engine):
    intake = IntakeService(settings, engine)
    old = await intake.ingest_text(TELEGRAM_USER_ID, "Baha kahapon")
    with Session(engine) as session:
        report = session.get(Report, old.id)
        report.received_at -= timedelta(hours=2)
        session.add(report)
        session.commit()

    pinned = await intake.ingest_location(TELEGRAM_USER_ID, 14.33, 121.08)

    assert pinned.id != old.id


async def test_invalid_coordinates_rejected(settings, engine):
    with pytest.raises(ValueError):
        await IntakeService(settings, engine).ingest_location(1, 95.0, 0.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_converts_and_splits(tmp_path):
    source = tmp_path / "tone.ogg"
    proc = await asyncio.create_subprocess_exec(
        shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2.5", "-c:a", "libopus", str(source),
    )  # fmt: skip
    await proc.wait()
    if proc.returncode != 0 or not source.exists():
        pytest.skip("this ffmpeg build cannot encode opus")

    chunks = await to_wav_chunks(source, tmp_path / "out", chunk_seconds=1)

    assert len(chunks) == 3
    assert all(c.read_bytes()[:4] == b"RIFF" for c in chunks)


async def test_missing_source_raises_clear_error(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    with pytest.raises(AudioConversionError, match="could not convert"):
        await to_wav_chunks(tmp_path / "nope.ogg", tmp_path / "out")

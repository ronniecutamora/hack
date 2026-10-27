"""Report intake: one service used by the Telegram bot, tests and later channels.

Each ingest_* call stores a Report for a pseudonymous Reporter. Raw photos and voice notes are
written under DATA_DIR/media and deleted later by app.retention. The raw user id is used only
to derive the pseudonym and is never stored or logged.
"""

import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.audio import AudioConversionError, to_wav_chunks
from app.cache import sha256_hex
from app.config import Settings
from app.meta.base import MetaError
from app.meta.transcribe import Transcript
from app.models import STATUS_TRANSCRIPTION_FAILED, Media, Report, Reporter, utcnow
from app.privacy import pseudonymize

logger = logging.getLogger(__name__)

# Taglish voice reports: bias toward Tagalog and English (the model detects the rest itself).
LANGUAGE_BIAS = ["Tagalog", "English"]
# A location pin sent shortly after a report is attached to that report.
LOCATION_JOIN_WINDOW = timedelta(minutes=15)

ConvertFn = Callable[[Path, Path], Awaitable[list[Path]]]


class Transcriber(Protocol):
    async def transcribe(
        self, wav: bytes, *, language_bias: list[str] | None = ..., keywords: list[str] | None = ...
    ) -> Transcript: ...


class IntakeService:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        transcriber: Transcriber | None = None,
        convert: ConvertFn = to_wav_chunks,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.transcriber = transcriber
        self.convert = convert

    def _session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def _reporter(self, session: Session, user_ref: int | str) -> Reporter:
        pseudonym = pseudonymize(self.settings.pseudonym_salt.get_secret_value(), user_ref)
        reporter = session.exec(select(Reporter).where(Reporter.pseudonym == pseudonym)).first()
        if reporter is None:
            reporter = Reporter(pseudonym=pseudonym)
            session.add(reporter)
            session.flush()
        return reporter

    def _new_report(
        self, session: Session, user_ref: int | str, channel: str, **fields: object
    ) -> Report:
        reporter = self._reporter(session, user_ref)
        assert reporter.id is not None
        report = Report(reporter_id=reporter.id, channel=channel, **fields)
        session.add(report)
        session.flush()
        return report

    def _store_media(self, session: Session, report: Report, kind: str, data: bytes, ext: str):
        assert report.id is not None
        digest = sha256_hex(data)
        relative = Path("media") / str(report.id) / f"{digest[:16]}.{ext.lstrip('.').lower()}"
        target = self.settings.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        media = Media(report_id=report.id, kind=kind, path=str(relative), sha256=digest)
        session.add(media)
        return media

    async def ingest_text(
        self, user_ref: int | str, text: str, *, channel: str = "telegram"
    ) -> Report:
        text = text.strip()
        if not text:
            raise ValueError("text report is empty")
        with self._session() as session:
            report = self._new_report(session, user_ref, channel, text=text)
            session.commit()
            return report

    async def ingest_photo(
        self,
        user_ref: int | str,
        data: bytes,
        *,
        caption: str | None = None,
        ext: str = "jpg",
        channel: str = "telegram",
    ) -> Report:
        with self._session() as session:
            report = self._new_report(
                session, user_ref, channel, text=(caption or "").strip() or None
            )
            self._store_media(session, report, "photo", data, ext)
            session.commit()
            return report

    async def ingest_voice(
        self,
        user_ref: int | str,
        data: bytes,
        *,
        ext: str = "ogg",
        channel: str = "telegram",
    ) -> Report:
        """Store the voice note, convert it, transcribe it and save the transcript.

        If conversion or transcription fails the report is still saved (status
        "transcription_failed") with the raw audio kept until retention purges it.
        """
        with self._session() as session:
            report = self._new_report(session, user_ref, channel)
            media = self._store_media(session, report, "voice", data, ext)
            try:
                report.transcript = await self._transcribe(self.settings.data_dir / media.path)
            except (MetaError, AudioConversionError) as exc:
                logger.warning("voice transcription failed: %s", exc)
                report.status = STATUS_TRANSCRIPTION_FAILED
            session.add(report)
            session.commit()
            return report

    async def _transcribe(self, audio_path: Path) -> str:
        if self.transcriber is None:
            raise AudioConversionError("no transcriber configured")
        parts: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:  # converted audio is never kept
            for chunk in await self.convert(audio_path, Path(tmp)):
                result = await self.transcriber.transcribe(
                    chunk.read_bytes(), language_bias=LANGUAGE_BIAS
                )
                if result.text:
                    parts.append(result.text)
        return " ".join(parts)

    async def ingest_location(
        self, user_ref: int | str, lat: float, lon: float, *, channel: str = "telegram"
    ) -> Report:
        """Attach a location pin to the reporter's latest report without one, else start one."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("latitude or longitude out of range")
        with self._session() as session:
            reporter = self._reporter(session, user_ref)
            cutoff = utcnow() - LOCATION_JOIN_WINDOW
            report = session.exec(
                select(Report)
                .where(
                    Report.reporter_id == reporter.id,
                    col(Report.lat).is_(None),
                    Report.received_at >= cutoff,
                )
                .order_by(col(Report.received_at).desc())
            ).first()
            if report is None:
                report = self._new_report(session, user_ref, channel)
            report.lat, report.lon = lat, lon
            session.add(report)
            session.commit()
            return report

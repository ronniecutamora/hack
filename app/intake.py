"""Report intake: one service used by the Telegram bot, tests and later channels.

Each ingest_* call stores a Report for a pseudonymous Reporter. Raw photos and voice notes are
written under DATA_DIR/media and deleted later by app.retention. The raw user id is used only
to derive the pseudonym and is never stored or logged.
"""

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from weakref import WeakValueDictionary

from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.audio import AudioConversionError, to_wav_chunks
from app.cache import sha256_hex
from app.config import Settings
from app.gazetteer import transcription_keywords
from app.meta.base import MetaError
from app.meta.transcribe import Transcript
from app.models import (
    STATUS_ASSESSED,
    STATUS_HELD,
    STATUS_NEW,
    STATUS_TRANSCRIPTION_FAILED,
    Media,
    Report,
    Reporter,
    utcnow,
)
from app.privacy import pseudonymize
from app.trust import HASH_UNAVAILABLE, HOLD_BLOCKED, hold_reason, photo_hash

logger = logging.getLogger(__name__)

# Taglish voice reports: bias toward Tagalog and English (the model detects the rest itself).
LANGUAGE_BIAS = ["Tagalog", "English"]
# A location pin sent shortly after a report is attached to that report.
LOCATION_JOIN_WINDOW = timedelta(minutes=15)
# Reporters send a photo, then type its words as a separate message (live test 2026-10-29: 1 s
# and 14 s gaps). A text this soon after a caption-less photo becomes the photo's caption.
CAPTION_JOIN_WINDOW = timedelta(minutes=2)

ConvertFn = Callable[[Path, Path], Awaitable[list[Path]]]


class Admission(StrEnum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"


class Transcriber(Protocol):
    async def transcribe(
        self, wav: bytes, *, language_bias: list[str] | None = ..., keywords: list[str] | None = ...
    ) -> Transcript: ...


async def transcribe_file(
    transcriber: Transcriber,
    audio_path: Path,
    *,
    language_bias: list[str] | None = None,
    keywords: list[str] | None = None,
    convert: ConvertFn | None = None,
) -> str:
    """Convert any audio file to WAV chunks, transcribe each and join the text."""
    parts: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:  # converted audio is never kept
        for chunk in await (convert or to_wav_chunks)(audio_path, Path(tmp)):
            result = await transcriber.transcribe(
                chunk.read_bytes(), language_bias=language_bias, keywords=keywords
            )
            if result.text:
                parts.append(result.text)
    return " ".join(parts)


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
        # Serialize admission through storage, including slow downloads, for Telegram albums.
        self._admission_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def admission_lock(self, user_ref: int | str) -> asyncio.Lock:
        key = pseudonymize(self.settings.pseudonym_salt.get_secret_value(), user_ref)
        lock = self._admission_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._admission_locks[key] = lock
        return lock

    def admit(self, user_ref: int | str) -> Admission:
        """Bot-only volume guard. Direct ingest (demo, imports, tests) stays unrestricted."""
        pseudonym = pseudonymize(self.settings.pseudonym_salt.get_secret_value(), user_ref)
        with self._session() as session:
            reporter = session.exec(select(Reporter).where(Reporter.pseudonym == pseudonym)).first()
            if reporter is None:
                return Admission.OK
            now = utcnow()
            for window, limit in (
                (timedelta(hours=1), self.settings.reports_per_hour),
                (timedelta(days=1), self.settings.reports_per_day),
            ):
                count = session.exec(
                    select(func.count(Report.id)).where(
                        Report.reporter_id == reporter.id, Report.received_at > now - window
                    )
                ).one()
                if count >= limit:
                    return Admission.RATE_LIMITED
            if reporter.blocked:
                return Admission.BLOCKED
        return Admission.OK

    def _location_target(self, session: Session, reporter_id: int) -> Report | None:
        return session.exec(
            select(Report)
            .where(
                Report.reporter_id == reporter_id,
                col(Report.lat).is_(None),
                Report.received_at >= utcnow() - LOCATION_JOIN_WINDOW,
            )
            .order_by(col(Report.received_at).desc(), col(Report.id).desc())
        ).first()

    def _caption_target(self, session: Session, reporter_id: int) -> Report | None:
        """The reporter's latest report if it is a recent photo without words.

        Taking the latest report means no other report came in between. A voice note's
        transcript is not a caption, and a report that has text is never overwritten.
        """
        latest = session.exec(
            select(Report)
            .where(
                Report.reporter_id == reporter_id,
                Report.received_at >= utcnow() - CAPTION_JOIN_WINDOW,
            )
            .order_by(col(Report.received_at).desc(), col(Report.id).desc())
        ).first()
        if latest is None or latest.text or latest.transcript:
            return None
        photo = session.exec(
            select(Media.id).where(Media.report_id == latest.id, Media.kind == "photo")
        ).first()
        return latest if photo is not None else None

    def _can_attach(
        self, user_ref: int | str, find: Callable[[Session, int], Report | None]
    ) -> bool:
        pseudonym = pseudonymize(self.settings.pseudonym_salt.get_secret_value(), user_ref)
        with self._session() as session:
            reporter = session.exec(select(Reporter).where(Reporter.pseudonym == pseudonym)).first()
            return (
                reporter is not None
                and not reporter.blocked
                and find(session, reporter.id) is not None
            )

    def can_attach_location(self, user_ref: int | str) -> bool:
        """A pin would join an existing report (so it is not a new report for the rate limit)."""
        return self._can_attach(user_ref, self._location_target)

    def can_attach_caption(self, user_ref: int | str) -> bool:
        """A text would become a photo's caption (so it is not a new report for the rate limit)."""
        return self._can_attach(user_ref, self._caption_target)

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
        if reporter.blocked:
            report.status, report.hold_reason = STATUS_HELD, HOLD_BLOCKED
        session.add(report)
        session.flush()
        return report

    def _store_media(
        self,
        session: Session,
        report: Report,
        kind: str,
        data: bytes,
        ext: str,
        *,
        phash: str | None = None,
    ):
        assert report.id is not None
        digest = sha256_hex(data)
        relative = Path("media") / str(report.id) / f"{digest[:16]}.{ext.lstrip('.').lower()}"
        target = self.settings.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        media = Media(
            report_id=report.id,
            kind=kind,
            path=str(relative),
            sha256=digest,
            phash=phash,
        )
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

    async def attach_caption(self, user_ref: int | str, text: str) -> Report | None:
        """Make a text the caption of the reporter's caption-less photo sent just before it.

        Returns the photo report, or None when the text is a report of its own. Like a late
        pin, an assessed report goes back to "new" so it is re-assessed (facts and place from
        the caption) and re-grouped; one being assessed right now is re-run by the assessor.
        A blocked reporter's text never joins an earlier report.
        """
        text = text.strip()
        if not text:
            return None
        pseudonym = pseudonymize(self.settings.pseudonym_salt.get_secret_value(), user_ref)
        with self._session() as session:
            reporter = session.exec(select(Reporter).where(Reporter.pseudonym == pseudonym)).first()
            if reporter is None or reporter.blocked:
                return None
            report = self._caption_target(session, reporter.id)
            if report is None:
                return None
            report.text = text
            if report.status == STATUS_ASSESSED:
                report.status = STATUS_NEW
            session.add(report)
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
        phash = await asyncio.to_thread(photo_hash, data) or HASH_UNAVAILABLE
        with self._session() as session:
            report = self._new_report(
                session, user_ref, channel, text=(caption or "").strip() or None
            )
            self._store_media(session, report, "photo", data, ext, phash=phash)
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
        The report is committed before transcribing: holding a write transaction open across
        the model call would lock SQLite for the call log and for the web app.
        """
        with self._session() as session:
            report = self._new_report(session, user_ref, channel)
            media = self._store_media(session, report, "voice", data, ext)
            reason = hold_reason(session, report, self.settings)
            if reason:
                report.status, report.hold_reason = STATUS_HELD, reason
                session.add(report)
            session.commit()
        if reason:
            return report
        try:
            report.transcript = await self._transcribe(self.settings.data_dir / media.path)
        except (MetaError, AudioConversionError) as exc:
            logger.warning("voice transcription failed: %s", exc)
            report.status = STATUS_TRANSCRIPTION_FAILED
        with self._session() as session:
            session.add(report)
            session.commit()
        return report

    async def _transcribe(self, audio_path: Path) -> str:
        if self.transcriber is None:
            raise AudioConversionError("no transcriber configured")
        return await transcribe_file(
            self.transcriber,
            audio_path,
            language_bias=LANGUAGE_BIAS,
            keywords=transcription_keywords(),  # barangay names: 4/9 -> 9/9 place names in tests
            convert=self.convert,
        )

    async def ingest_location(
        self, user_ref: int | str, lat: float, lon: float, *, channel: str = "telegram"
    ) -> Report:
        """Attach a location pin to the reporter's latest report without one, else start one."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("latitude or longitude out of range")
        with self._session() as session:
            reporter = self._reporter(session, user_ref)
            report = None if reporter.blocked else self._location_target(session, reporter.id)
            if report is None:
                report = self._new_report(session, user_ref, channel)
            elif report.status == STATUS_ASSESSED:
                # A new pin needs remapping even when the spend gate delays processing.
                report.status = STATUS_NEW
            report.lat, report.lon = lat, lon
            session.add(report)
            session.commit()
            return report

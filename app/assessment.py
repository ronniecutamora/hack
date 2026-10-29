"""Assess a report: clean the transcript, extract facts, judge photos, find the barangay.

Run in the background by the bot after each message, and from the command line:
    uv run python -m app.assessment --report 12
    uv run python -m app.assessment --pending
    uv run python -m app.assessment --all --llm muse-spark-open

Model calls are cached, so re-assessing (after a location pin arrives, or to compare LLMs) costs
nothing for unchanged inputs. No database transaction is held across a model call: a write
lock held during a slow call once locked out the call log ("database is locked", 2026-10-27).
"""

import argparse
import asyncio
import logging
import mimetypes
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.audio import AudioConversionError, to_wav_chunks
from app.cache import sha256_hex
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.facts import extract_facts
from app.gazetteer import Barangay, find_in_text
from app.geo import barangay_at, exif_gps
from app.incidents import IncidentService, coordination, observations, rollup
from app.intake import ConvertFn, IntakeService, Transcriber
from app.meta.base import MetaError, Recorder
from app.meta.factory import (
    LLM_BACKENDS,
    make_agent_llm,
    make_pair_judge,
    make_sam,
    make_transcriber,
)
from app.meta.llm import AgentLLM
from app.meta.sam import SamClient
from app.models import (
    STATUS_ASSESSED,
    STATUS_ASSESSING,
    STATUS_ASSESSMENT_FAILED,
    STATUS_HELD,
    STATUS_NEW,
    STATUS_TRANSCRIPTION_FAILED,
    Assessment,
    Incident,
    Media,
    PhotoAssessment,
    Report,
    Reporter,
    utcnow,
)
from app.photo import assess_photo
from app.transcript_cleanup import clean_transcript
from app.trust import compute_signals, hold_reason, pending_photo_hashes, photo_taken_at

logger = logging.getLogger(__name__)
# "assessing" is included so a run cut short by a crash or restart is picked up again.
PENDING_STATUSES = (
    STATUS_NEW,
    STATUS_ASSESSING,
    STATUS_TRANSCRIPTION_FAILED,
    STATUS_ASSESSMENT_FAILED,
    STATUS_HELD,
)


@dataclass(frozen=True)
class GeoResult:
    barangay: Barangay | None
    source: str  # pin | exif | text | transcript | llm | ambiguous | none
    detail: str | None = None


def geolocate(
    *,
    pin: tuple[float, float] | None,
    exif: tuple[float, float] | None,
    text: str | None,
    transcript: str | None,
    llm_barangay: str | None,
) -> GeoResult:
    """Pick the barangay: a GPS point first, then a name in the text, transcript or LLM facts.

    A point outside Biñan is noted but does not stop a name match (a reporter may send the
    pin from elsewhere). Several names in one text are ambiguous: no barangay is guessed.
    The LLM name is only a fallback when neither text nor transcript names a barangay.
    """
    notes: list[str] = []
    for source, point in (("pin", pin), ("exif", exif)):
        if point is None:
            continue
        barangay = barangay_at(*point)
        if barangay is not None:
            return GeoResult(barangay, source)
        notes.append(f"{source} outside Biñan")
    named_places = [
        (source, find_in_text(value))
        for source, value in (("text", text), ("transcript", transcript))
    ]
    for _, match in named_places:
        if match.ambiguous:
            return GeoResult(None, "ambiguous", " or ".join(b.name for b in match.barangays))
    for source, match in [*named_places, ("llm", find_in_text(llm_barangay, require_cues=False))]:
        if match.barangay is not None:
            detail = ", ".join([*match.matched, *notes]) + (" (approximate)" if match.fuzzy else "")
            return GeoResult(match.barangay, source, detail)
        if match.ambiguous:
            return GeoResult(None, "ambiguous", " or ".join(b.name for b in match.barangays))
    return GeoResult(None, "none", ", ".join(notes) or None)


def officer_place(report: Report, geo: GeoResult) -> tuple[str, str | None]:
    """Geo source and detail to store: an officer-set barangay always wins over geolocation,
    and the agent's own finding is kept beside it for the audit."""
    if report.officer_barangay is None:
        return geo.source, geo.detail
    found = geo.barangay.name if geo.barangay else "no barangay"
    return "officer", f"set by an officer; the agent found {found} ({geo.source})"


@dataclass
class _Photo:
    media_id: int
    data: bytes
    mime: str


@dataclass
class _PhotoOutcome:
    media_id: int
    judgment: dict[str, Any] | None = None
    measurement: dict[str, Any] | None = None
    overlay: bytes | None = None
    gps: tuple[float, float] | None = None
    taken_at: datetime | None = None
    error: str | None = None


class AssessmentService:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        llm: AgentLLM,
        sam: SamClient,
        incidents: IncidentService | None = None,
        transcriber: Transcriber | None = None,
        convert: ConvertFn = to_wav_chunks,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.llm = llm
        self.sam = sam
        self._locks = coordination(engine).reports
        self.incidents = incidents or IncidentService(settings, engine, llm)
        self.voice_intake = IntakeService(settings, engine, transcriber, convert)

    def _session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    async def assess(self, report_id: int) -> Report | None:
        """Assess one report; errors are stored on the assessment, never raised.

        Returns None if the report does not exist.
        """
        async with self._locks[report_id]:  # a pin and a photo may arrive moments apart
            with self._session() as session:
                report = session.get(Report, report_id)
                if report is None:
                    return None
                reason = hold_reason(session, report, self.settings)
                if reason:
                    if report.status == STATUS_ASSESSED:
                        logger.warning("skipping already-assessed report %s: %s", report_id, reason)
                        return report
                    report.status, report.hold_reason = STATUS_HELD, reason
                    session.add(report)
                    session.commit()
                    return report
            try:
                report = await self._assess(report_id)
            except Exception as exc:  # keep the bot alive; the error is saved for the officer
                logger.exception("assessment of report %s failed", report_id)
                return self._mark_failed(report_id, exc)
            if report is not None and report.status == STATUS_ASSESSED:
                try:
                    report = await self.incidents.group_locked(report_id)
                except Exception as exc:
                    logger.exception("grouping of report %s failed", report_id)
                    with self._session() as session:
                        report = session.get(Report, report_id)
                        report.grouping_error = f"{type(exc).__name__}: {exc}"
                        session.add(report)
                        session.commit()
            return report

    async def _assess(self, report_id: int) -> Report | None:
        with self._session() as session:
            report = session.get(Report, report_id)
            if report is None:
                return None
            previous = session.exec(
                select(Assessment.error).where(Assessment.report_id == report_id)
            ).first()
            raw_expired = False
            if report.status == STATUS_HELD or "raw media expired while held" in (previous or ""):
                raw_media = session.exec(
                    select(Media).where(
                        Media.report_id == report_id, col(Media.kind).in_(("photo", "voice"))
                    )
                ).all()
                raw_expired = any(
                    m.purged_at is not None or not (self.settings.data_dir / m.path).is_file()
                    for m in raw_media
                )
            photos = [
                _Photo(
                    m.id,
                    (self.settings.data_dir / m.path).read_bytes(),
                    mimetypes.guess_type(m.path)[0] or "image/jpeg",
                )
                for m in session.exec(
                    select(Media).where(
                        Media.report_id == report_id,
                        Media.kind == "photo",
                        col(Media.purged_at).is_(None),
                    )
                ).all()
                if (self.settings.data_dir / m.path).is_file()
            ]
            photo_ids = self._photo_ids(session, report_id)
            untranscribed_voice = not report.transcript and (
                session.exec(
                    select(Media.id).where(Media.report_id == report_id, Media.kind == "voice")
                ).first()
                is not None
            )
            report.status = STATUS_ASSESSING
            report.assessing_started_at = utcnow()
            report.hold_reason = None
            session.add(report)
            session.commit()

        # A held voice note never incurred transcription cost during intake. Resume it here
        # under the same block/spend gate as facts, photos and incident pair judgments.
        if untranscribed_voice and self.voice_intake.transcriber is not None:
            with self._session() as session:
                voice = session.exec(
                    select(Media).where(
                        Media.report_id == report_id,
                        Media.kind == "voice",
                        col(Media.purged_at).is_(None),
                    )
                ).first()
            if voice and (self.settings.data_dir / voice.path).is_file():
                try:
                    transcript = await self.voice_intake._transcribe(
                        self.settings.data_dir / voice.path
                    )
                except (MetaError, AudioConversionError):
                    logger.warning("held voice transcription failed for report %s", report_id)
                else:
                    report.transcript = transcript
                    with self._session() as session:
                        current = session.get(Report, report_id)
                        current.transcript = transcript
                        session.add(current)
                        session.commit()
                    untranscribed_voice = not bool(transcript.strip())

        transcript = clean_transcript(report.transcript) if report.transcript else None
        text = "\n".join(t for t in (report.text, transcript) if t)
        (facts, facts_error), outcomes = await asyncio.gather(
            self._facts(text) if text else _none(),
            asyncio.gather(*(self._photo(p) for p in photos)),
        )

        gps = next((o.gps for o in outcomes if o.gps), None)
        if gps is None:
            with self._session() as session:
                stored = session.exec(
                    select(PhotoAssessment)
                    .where(
                        PhotoAssessment.report_id == report_id,
                        col(PhotoAssessment.gps_lat).is_not(None),
                        col(PhotoAssessment.gps_lon).is_not(None),
                    )
                    .order_by(PhotoAssessment.id)
                ).first()
                if stored is not None:
                    gps = (stored.gps_lat, stored.gps_lon)
        geo = geolocate(
            pin=(report.lat, report.lon) if report.lat is not None else None,
            exif=gps,
            text=report.text,
            transcript=transcript,
            llm_barangay=(facts or {}).get("barangay_mentioned"),
        )
        errors = [e for e in (facts_error, *(o.error for o in outcomes)) if e]
        if raw_expired:
            errors.append("raw media expired while held")
        if untranscribed_voice:  # part of the report is missing: not "assessed"
            errors.insert(0, "voice note not transcribed")
        inputs = (report.text, report.lat, report.lon, photo_ids)
        return self._save(report_id, transcript, facts, geo, outcomes, errors, inputs)

    async def _facts(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return await extract_facts(self.llm, text), None
        except MetaError as exc:
            logger.warning("fact extraction failed: %s", exc)
            return None, f"facts: {exc}"

    async def _photo(self, photo: _Photo) -> _PhotoOutcome:
        outcome = _PhotoOutcome(photo.media_id)
        try:
            outcome.gps = await asyncio.to_thread(exif_gps, photo.data)
            outcome.taken_at = await asyncio.to_thread(photo_taken_at, photo.data)
            result = await assess_photo(self.llm, self.sam, photo.data, photo.mime)
        except MetaError as exc:
            logger.warning("photo assessment failed: %s", exc)
            outcome.error = f"photo {photo.media_id}: {exc}"
            return outcome
        except Exception as exc:  # one bad photo must not lose the report's other results
            logger.exception("photo %s could not be assessed", photo.media_id)
            outcome.error = f"photo {photo.media_id}: {type(exc).__name__}: {exc}"
            return outcome
        outcome.judgment = result.judgment
        outcome.measurement = result.measurement.as_dict()
        outcome.overlay = result.overlay
        return outcome

    def _save(
        self,
        report_id: int,
        transcript: str | None,
        facts: dict[str, Any] | None,
        geo: GeoResult,
        outcomes: list[_PhotoOutcome],
        errors: list[str],
        inputs: tuple[str | None, float | None, float | None, tuple[int, ...]],
    ) -> Report:
        """Store the results. `inputs` are the report's text, pin and photos this pass read: if
        a late caption, pin or album photo changed them meanwhile, the report stays "new" (not
        grouped) and the re-assessment queued with that change finishes it."""
        # Legacy hashing reads files/decodes images, so finish it before any SQLite write.
        hashes = pending_photo_hashes(self.engine, self.settings)
        with self._session() as session:
            for media_id, phash in hashes.items():
                media = session.get(Media, media_id)
                if media is not None and media.phash is None:
                    media.phash = phash
                    session.add(media)
            report = session.get(Report, report_id)
            assessment = session.exec(
                select(Assessment).where(Assessment.report_id == report_id)
            ).first() or Assessment(report_id=report_id, llm_model=self.llm.model)
            assessment.llm_model = self.llm.model
            assessment.transcript_clean = transcript
            if facts is not None or assessment.facts is None:
                assessment.facts = facts
            assessment.geo_source, assessment.geo_detail = officer_place(report, geo)
            assessment.error = "; ".join(errors) or None
            assessment.updated_at = utcnow()
            session.add(assessment)
            for outcome in outcomes:
                self._save_photo(session, report_id, outcome)
            session.flush()
            assessment.signals = compute_signals(
                session, report, self.settings, transcript=transcript
            )
            session.add(assessment)
            if report.officer_barangay is None:
                report.barangay = geo.barangay.name if geo.barangay else None
            current = (report.text, report.lat, report.lon, self._photo_ids(session, report_id))
            if current != inputs:
                report.status = STATUS_NEW
            else:
                report.status = STATUS_ASSESSMENT_FAILED if errors else STATUS_ASSESSED
            session.add(report)
            session.flush()
            # Trust changes do not change grouping inputs or justify another pair-judge call.
            if report.incident_id is not None:
                incident = session.get(Incident, report.incident_id)
                if incident is not None:
                    incident.latest_facts = rollup(
                        [
                            o
                            for o in observations(session).values()
                            if o.incident_id == report.incident_id
                        ]
                    )
                    session.add(incident)
            session.commit()
            return report

    @staticmethod
    def _photo_ids(session: Session, report_id: int) -> tuple[int, ...]:
        return tuple(
            session.exec(
                select(Media.id)
                .where(Media.report_id == report_id, Media.kind == "photo")
                .order_by(Media.id)
            ).all()
        )

    def _save_photo(self, session: Session, report_id: int, outcome: _PhotoOutcome) -> None:
        row = session.exec(
            select(PhotoAssessment).where(PhotoAssessment.media_id == outcome.media_id)
        ).first() or PhotoAssessment(
            report_id=report_id, media_id=outcome.media_id, llm_model=self.llm.model
        )
        row.gps_lat, row.gps_lon = outcome.gps or (None, None)
        row.taken_at = outcome.taken_at
        row.error = outcome.error
        row.updated_at = utcnow()
        if outcome.error is None:
            row.llm_model = self.llm.model
            row.judgment = outcome.judgment
            row.measurement = outcome.measurement
            if outcome.overlay is not None:
                row.overlay_media_id = self._store_overlay(session, report_id, outcome.overlay)
        session.add(row)

    def _store_overlay(self, session: Session, report_id: int, data: bytes) -> int:
        """Store the overlay as raw media (it shows the photo), reusing an identical one."""
        digest = sha256_hex(data)
        existing = session.exec(
            select(Media).where(
                Media.report_id == report_id,
                Media.kind == "overlay",
                Media.sha256 == digest,
                col(Media.purged_at).is_(None),
            )
        ).first()
        if existing is not None:
            return existing.id
        relative = Path("media") / str(report_id) / f"overlay-{digest[:16]}.jpg"
        target = self.settings.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        media = Media(report_id=report_id, kind="overlay", path=str(relative), sha256=digest)
        session.add(media)
        session.flush()
        return media.id

    def _mark_failed(self, report_id: int, exc: Exception | str) -> Report | None:
        with self._session() as session:
            report = session.get(Report, report_id)
            if report is None:
                return None
            assessment = session.exec(
                select(Assessment).where(Assessment.report_id == report_id)
            ).first() or Assessment(report_id=report_id, llm_model=self.llm.model)
            assessment.error = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
            assessment.updated_at = utcnow()
            report.status = STATUS_ASSESSMENT_FAILED
            report.hold_reason = None
            session.add_all([assessment, report])
            session.commit()
            return report

    def report_ids(self, *, pending_only: bool) -> list[int]:
        query = select(Report.id).order_by(col(Report.id))
        if pending_only:
            query = query.where(col(Report.status).in_(PENDING_STATUSES))
            query = query.join(Reporter, Reporter.id == Report.reporter_id).where(
                Reporter.blocked == False  # noqa: E712
            )
        with self._session() as session:
            return list(session.exec(query).all())


async def _none() -> tuple[None, None]:
    return None, None


def make_assessor(
    settings: Settings,
    engine: Engine,
    recorder: Recorder | None = None,
    transcriber: Transcriber | None = None,
) -> AssessmentService:
    return AssessmentService(
        settings,
        engine,
        make_agent_llm(settings, recorder),
        make_sam(settings, recorder),
        IncidentService(settings, engine, make_pair_judge(settings, recorder)),
        transcriber=transcriber,
    )


async def _cli(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.llm:
        settings = settings.model_copy(update={"agent_llm": args.llm})
    engine = make_engine(settings)
    init_db(engine)
    recorder = make_recorder(engine)
    transcriber = make_transcriber(settings, recorder)
    assessor = make_assessor(settings, engine, recorder, transcriber=transcriber)
    try:
        ids = [args.report] if args.report else assessor.report_ids(pending_only=args.pending)
        print(f"assessing {len(ids)} report(s) with {assessor.llm.model}")
        for report_id in ids:
            report = await assessor.assess(report_id)
            if report is None:
                print(f"report {report_id}: not found")
                continue
            print(f"report {report_id}: {report.status}, barangay {report.barangay or '-'}")
    finally:
        await transcriber.aclose()
        await assessor.llm.aclose()
        await assessor.sam.aclose()
        await assessor.incidents.judge.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assess reports (facts, photos, barangay).")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--report", type=int, help="one report id")
    which.add_argument(
        "--pending", action="store_true", help="new, failed or eligible held reports"
    )
    which.add_argument("--all", action="store_true", help="every report")
    parser.add_argument("--llm", choices=list(LLM_BACKENDS), help="override AGENT_LLM")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    return asyncio.run(_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

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
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.cache import sha256_hex
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.facts import extract_facts
from app.gazetteer import Barangay, find_in_text
from app.geo import barangay_at, exif_gps
from app.meta.base import MetaError, Recorder
from app.meta.factory import make_agent_llm, make_sam
from app.meta.llm import AgentLLM
from app.meta.sam import SamClient
from app.models import (
    STATUS_ASSESSED,
    STATUS_ASSESSING,
    STATUS_ASSESSMENT_FAILED,
    STATUS_NEW,
    STATUS_TRANSCRIPTION_FAILED,
    Assessment,
    Media,
    PhotoAssessment,
    Report,
    utcnow,
)
from app.photo import assess_photo
from app.transcript_cleanup import clean_transcript

logger = logging.getLogger(__name__)
# "assessing" is included so a run cut short by a crash or restart is picked up again.
PENDING_STATUSES = (
    STATUS_NEW,
    STATUS_ASSESSING,
    STATUS_TRANSCRIPTION_FAILED,
    STATUS_ASSESSMENT_FAILED,
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
    """
    notes: list[str] = []
    for source, point in (("pin", pin), ("exif", exif)):
        if point is None:
            continue
        barangay = barangay_at(*point)
        if barangay is not None:
            return GeoResult(barangay, source)
        notes.append(f"{source} outside Biñan")
    ambiguous: list[str] = []
    for source, value in (("text", text), ("transcript", transcript), ("llm", llm_barangay)):
        match = find_in_text(value, require_cues=source != "llm")
        if match.barangay is not None:
            detail = ", ".join([*match.matched, *notes]) + (" (approximate)" if match.fuzzy else "")
            return GeoResult(match.barangay, source, detail)
        if match.ambiguous:
            ambiguous = [b.name for b in match.barangays]
    if ambiguous:
        return GeoResult(None, "ambiguous", " or ".join(ambiguous))
    return GeoResult(None, "none", ", ".join(notes) or None)


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
    error: str | None = None


class AssessmentService:
    def __init__(self, settings: Settings, engine: Engine, llm: AgentLLM, sam: SamClient) -> None:
        self.settings = settings
        self.engine = engine
        self.llm = llm
        self.sam = sam
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    async def assess(self, report_id: int) -> Report | None:
        """Assess one report; errors are stored on the assessment, never raised.

        Returns None if the report does not exist.
        """
        async with self._locks[report_id]:  # a pin and a photo may arrive moments apart
            try:
                return await self._assess(report_id)
            except Exception as exc:  # keep the bot alive; the error is saved for the officer
                logger.exception("assessment of report %s failed", report_id)
                return self._mark_failed(report_id, exc)

    async def _assess(self, report_id: int) -> Report | None:
        with self._session() as session:
            report = session.get(Report, report_id)
            if report is None:
                return None
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
            untranscribed_voice = report.transcript is None and (
                session.exec(
                    select(Media.id).where(Media.report_id == report_id, Media.kind == "voice")
                ).first()
                is not None
            )
            report.status = STATUS_ASSESSING
            session.add(report)
            session.commit()

        transcript = clean_transcript(report.transcript) if report.transcript else None
        text = "\n".join(t for t in (report.text, transcript) if t)
        (facts, facts_error), outcomes = await asyncio.gather(
            self._facts(text) if text else _none(),
            asyncio.gather(*(self._photo(p) for p in photos)),
        )

        gps = next((o.gps for o in outcomes if o.gps), None)
        geo = geolocate(
            pin=(report.lat, report.lon) if report.lat is not None else None,
            exif=gps,
            text=report.text,
            transcript=transcript,
            llm_barangay=(facts or {}).get("barangay_mentioned"),
        )
        errors = [e for e in (facts_error, *(o.error for o in outcomes)) if e]
        if untranscribed_voice:  # part of the report is missing: not "assessed"
            errors.insert(0, "voice note not transcribed")
        return self._save(report_id, transcript, facts, geo, outcomes, errors)

    async def _facts(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return await extract_facts(self.llm, text), None
        except MetaError as exc:
            logger.warning("fact extraction failed: %s", exc)
            return None, f"facts: {exc}"

    async def _photo(self, photo: _Photo) -> _PhotoOutcome:
        outcome = _PhotoOutcome(photo.media_id, gps=exif_gps(photo.data))
        try:
            result = await assess_photo(self.llm, self.sam, photo.data, photo.mime)
        except MetaError as exc:
            logger.warning("photo assessment failed: %s", exc)
            outcome.error = f"photo {photo.media_id}: {exc}"
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
    ) -> Report:
        with self._session() as session:
            report = session.get(Report, report_id)
            assessment = session.exec(
                select(Assessment).where(Assessment.report_id == report_id)
            ).first() or Assessment(report_id=report_id, llm_model=self.llm.model)
            assessment.llm_model = self.llm.model
            assessment.transcript_clean = transcript
            if facts is not None or assessment.facts is None:
                assessment.facts = facts
            assessment.geo_source = geo.source
            assessment.geo_detail = geo.detail
            assessment.error = "; ".join(errors) or None
            assessment.updated_at = utcnow()
            session.add(assessment)
            for outcome in outcomes:
                self._save_photo(session, report_id, outcome)
            report.barangay = geo.barangay.name if geo.barangay else None
            report.status = STATUS_ASSESSMENT_FAILED if errors else STATUS_ASSESSED
            session.add(report)
            session.commit()
            return report

    def _save_photo(self, session: Session, report_id: int, outcome: _PhotoOutcome) -> None:
        row = session.exec(
            select(PhotoAssessment).where(PhotoAssessment.media_id == outcome.media_id)
        ).first() or PhotoAssessment(
            report_id=report_id, media_id=outcome.media_id, llm_model=self.llm.model
        )
        row.gps_lat, row.gps_lon = outcome.gps or (None, None)
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

    def _mark_failed(self, report_id: int, exc: Exception) -> Report | None:
        with self._session() as session:
            report = session.get(Report, report_id)
            if report is None:
                return None
            assessment = session.exec(
                select(Assessment).where(Assessment.report_id == report_id)
            ).first() or Assessment(report_id=report_id, llm_model=self.llm.model)
            assessment.error = f"{type(exc).__name__}: {exc}"
            assessment.updated_at = utcnow()
            report.status = STATUS_ASSESSMENT_FAILED
            session.add_all([assessment, report])
            session.commit()
            return report

    def report_ids(self, *, pending_only: bool) -> list[int]:
        query = select(Report.id).order_by(col(Report.id))
        if pending_only:
            query = query.where(col(Report.status).in_(PENDING_STATUSES))
        with self._session() as session:
            return list(session.exec(query).all())


async def _none() -> tuple[None, None]:
    return None, None


def make_assessor(
    settings: Settings, engine: Engine, recorder: Recorder | None = None
) -> AssessmentService:
    return AssessmentService(
        settings, engine, make_agent_llm(settings, recorder), make_sam(settings, recorder)
    )


async def _cli(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.llm:
        settings = settings.model_copy(update={"agent_llm": args.llm})
    engine = make_engine(settings)
    init_db(engine)
    assessor = make_assessor(settings, engine, make_recorder(engine))
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
        await assessor.llm.aclose()
        await assessor.sam.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assess reports (facts, photos, barangay).")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--report", type=int, help="one report id")
    which.add_argument("--pending", action="store_true", help="new or failed reports")
    which.add_argument("--all", action="store_true", help="every report")
    parser.add_argument(
        "--llm", choices=["muse-spark-1.3", "muse-spark-open"], help="override AGENT_LLM"
    )
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    return asyncio.run(_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

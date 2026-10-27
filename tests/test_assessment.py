import io

import pytest
from PIL import Image
from sqlmodel import Session, select

from app.assessment import AssessmentService, geolocate
from app.db import init_db, make_engine, make_recorder
from app.facts import FACTS_SCHEMA
from app.gazetteer import BY_NAME
from app.intake import IntakeService
from app.meta.base import CallRecord, ModelApiError
from app.meta.sam import parse_stream
from app.models import Assessment, Media, ModelCall, PhotoAssessment, Report
from tests.test_geo import CENTRES, jpeg_with_gps
from tests.test_photo import JUDGMENT
from tests.test_sam import STREAM

POBLACION = CENTRES["Poblacion"]
MANILA = (14.5995, 120.9842)


def facts(**fields):
    reply = {key: None for key in FACTS_SCHEMA["properties"]}
    reply.update(
        hazards=[],
        flood_depth="unknown",
        crop="none",
        infrastructure=[],
        needs=[],
        is_correction=False,
        summary_en="Flooding reported.",
    )
    reply.update(fields)
    return reply


class FakeLLM:
    model = "fake-llm"

    def __init__(self, facts_reply=None, error=None, recorder=None):
        self.facts_reply = facts_reply or facts()
        self.error = error
        self.recorder = recorder
        self.calls = []

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.recorder:  # like the real clients, log the call to the DB mid-request
            self.recorder(CallRecord(self.model, 5, False, 0.01))
        if self.error:
            raise self.error
        return JUDGMENT if kwargs.get("image") is not None else self.facts_reply


class FakeSam:
    async def segment(self, image, concept, *, image_mime="image/jpeg", image_url=None):
        return parse_stream(STREAM, concept)


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    return engine


def jpeg(size=(1000, 500)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "gray").save(buffer, "JPEG")
    return buffer.getvalue()


def one(engine, model):
    with Session(engine) as session:
        return session.exec(select(model)).one()


def all_rows(engine, model):
    with Session(engine) as session:
        return session.exec(select(model)).all()


# geolocation rules


def test_pin_wins_over_names_in_text():
    geo = geolocate(
        pin=POBLACION, exif=None, text="baha sa Casile", transcript=None, llm_barangay=None
    )
    assert (geo.barangay, geo.source) == (BY_NAME["Poblacion"], "pin")


def test_photo_gps_is_used_when_there_is_no_pin():
    geo = geolocate(pin=None, exif=CENTRES["Zapote"], text=None, transcript=None, llm_barangay=None)
    assert (geo.barangay, geo.source) == (BY_NAME["Zapote"], "exif")


def test_pin_outside_biñan_falls_back_to_the_text_and_says_so():
    geo = geolocate(
        pin=MANILA, exif=None, text="Brgy. Malaban po", transcript=None, llm_barangay=None
    )
    assert (geo.barangay, geo.source) == (BY_NAME["Malaban"], "text")
    assert "pin outside Biñan" in geo.detail


def test_transcript_then_llm_name_are_used_in_order():
    geo = geolocate(
        pin=None, exif=None, text=None, transcript="taga Sto. Niño po", llm_barangay="Casile"
    )
    assert (geo.barangay, geo.source) == (BY_NAME["Santo Niño"], "transcript")
    geo = geolocate(pin=None, exif=None, text=None, transcript=None, llm_barangay="Dela Paz")
    assert (geo.barangay, geo.source) == (BY_NAME["De La Paz"], "llm")


def test_free_text_from_the_llm_never_becomes_a_barangay():
    geo = geolocate(
        pin=None, exif=None, text=None, transcript=None, llm_barangay="Barangay Sinagpulo"
    )
    assert (geo.barangay, geo.source) == (None, "none")


def test_two_names_are_ambiguous_not_guessed():
    geo = geolocate(
        pin=None, exif=None, text="mula Malaban hanggang Casile", transcript=None, llm_barangay=None
    )
    assert (geo.barangay, geo.source, geo.detail) == (None, "ambiguous", "Malaban or Casile")


# the service


def service(settings, engine, llm=None):
    return AssessmentService(settings, engine, llm or FakeLLM(), FakeSam())


async def test_photo_report_is_judged_measured_overlaid_and_mapped(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo(
        7, jpeg(), caption="Baha na po dito sa Dela Paz"
    )
    llm = FakeLLM(facts(barangay_mentioned="Dela Paz", hazards=["flood"]))

    out = await service(settings, engine, llm).assess(report.id)

    assert out.status == "assessed"
    assert out.barangay == "De La Paz"
    assessment = one(engine, Assessment)
    assert (assessment.geo_source, assessment.llm_model) == ("text", "fake-llm")
    assert assessment.facts["hazards"] == ["flood"]
    photo = one(engine, PhotoAssessment)
    assert photo.judgment == JUDGMENT
    assert photo.measurement["roof_count"] == 2
    assert photo.measurement["floodwater_present"] is True
    overlay = next(m for m in all_rows(engine, Media) if m.kind == "overlay")
    assert photo.overlay_media_id == overlay.id
    assert (settings.data_dir / overlay.path).read_bytes().startswith(b"\xff\xd8")
    assert {c.get("image") is not None for c in llm.calls} == {True, False}


async def test_voice_report_uses_the_cleaned_transcript_and_keeps_the_raw(settings, engine):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_text(7, "placeholder")
    with Session(engine) as session:
        row = session.get(Report, report.id)
        row.text, row.transcript = None, "baha baha na po sa barangay Mamplason"
        session.add(row)
        session.commit()
    llm = FakeLLM()

    out = await service(settings, engine, llm).assess(report.id)

    assert out.transcript == "baha baha na po sa barangay Mamplason"  # raw evidence kept
    assessment = one(engine, Assessment)
    assert assessment.transcript_clean == "baha na po sa barangay Mamplason"
    assert (out.barangay, assessment.geo_source) == ("Mamplasan", "transcript")
    assert "(approximate)" in assessment.geo_detail
    assert "baha na po" in llm.calls[0]["user_text"]


async def test_a_later_pin_re_maps_the_report(settings, engine):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_photo(7, jpeg(), caption="baha sa Casile")
    assessor = service(settings, engine)
    assert (await assessor.assess(report.id)).barangay == "Casile"

    await intake.ingest_location(7, *POBLACION)
    again = await assessor.assess(report.id)

    assert again.barangay == "Poblacion"
    assert one(engine, Assessment).geo_source == "pin"
    assert len(all_rows(engine, PhotoAssessment)) == 1  # updated, not duplicated
    assert len([m for m in all_rows(engine, Media) if m.kind == "overlay"]) == 1


async def test_exif_gps_from_a_photo_sent_as_a_file(settings, engine):
    lat_dms = (14.0, 18.0, 55.33)  # Zapote's centre, 14.31537 N
    lon_dms = (121.0, 5.0, 2.47)  # 121.08402 E
    report = await IntakeService(settings, engine).ingest_photo(
        7, jpeg_with_gps(lat_dms, "N", lon_dms, "E")
    )
    out = await service(settings, engine).assess(report.id)
    assert out.barangay == "Zapote"
    assert one(engine, Assessment).geo_source == "exif"
    assert one(engine, PhotoAssessment).gps_lat == pytest.approx(14.31537, abs=1e-4)
    assert out.lat is None  # EXIF GPS is not written over the reporter's pin field


async def test_model_failure_saves_the_report_as_failed_with_the_error(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo(7, jpeg(), caption="baha")
    llm = FakeLLM(error=ModelApiError("Muse Spark", "overloaded", status=503))

    out = await service(settings, engine, llm).assess(report.id)

    assert out.status == "assessment_failed"
    assert "overloaded" in one(engine, Assessment).error
    assert "overloaded" in one(engine, PhotoAssessment).error


async def test_unexpected_errors_are_stored_not_raised(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "baha")
    llm = FakeLLM(error=RuntimeError("bug"))
    out = await service(settings, engine, llm).assess(report.id)
    assert out.status == "assessment_failed"
    assert one(engine, Assessment).error == "RuntimeError: bug"


async def test_missing_report_returns_none(settings, engine):
    assert await service(settings, engine).assess(999) is None


async def test_no_db_lock_is_held_while_models_run(settings, engine):
    # Regression guard: the call log writes to the DB during each model call (2026-10-27).
    report = await IntakeService(settings, engine).ingest_photo(7, jpeg(), caption="baha")
    llm = FakeLLM(recorder=make_recorder(engine))

    out = await service(settings, engine, llm).assess(report.id)

    assert out.status == "assessed"
    assert len(all_rows(engine, ModelCall)) == 2  # facts + photo judgment


async def test_purged_photos_keep_their_earlier_assessment(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo(7, jpeg(), caption="baha")
    assessor = service(settings, engine)
    await assessor.assess(report.id)
    with Session(engine) as session:
        for media in session.exec(select(Media)).all():
            (settings.data_dir / media.path).unlink()
            media.purged_at = media.created_at
            session.add(media)
        session.commit()

    out = await assessor.assess(report.id)

    assert out.status == "assessed"
    assert one(engine, PhotoAssessment).judgment == JUDGMENT


async def test_pending_ids_skip_assessed_reports(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_text(7, "baha sa Casile")
    second = await intake.ingest_text(7, "baha sa Malaban")
    assessor = service(settings, engine)
    await assessor.assess(first.id)
    assert assessor.report_ids(pending_only=True) == [second.id]
    assert assessor.report_ids(pending_only=False) == [first.id, second.id]


async def test_a_voice_note_that_was_never_transcribed_is_not_assessed(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "placeholder")
    with Session(engine) as session:
        row = session.get(Report, report.id)
        row.text, row.status = None, "transcription_failed"
        session.add(row)
        session.add(Media(report_id=report.id, kind="voice", path="media/1/a.ogg", sha256="x"))
        session.commit()
    assessor = service(settings, engine)

    out = await assessor.assess(report.id)

    assert out.status == "assessment_failed"
    assert one(engine, Assessment).error == "voice note not transcribed"
    assert assessor.report_ids(pending_only=True) == [report.id]  # still to do


async def test_a_run_cut_short_while_assessing_is_picked_up_again(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "baha sa Casile")
    with Session(engine) as session:
        row = session.get(Report, report.id)
        row.status = "assessing"  # the process died mid-run
        session.add(row)
        session.commit()
    assert service(settings, engine).report_ids(pending_only=True) == [report.id]


async def test_an_oversized_photo_is_judged_without_an_overlay(settings, engine, monkeypatch):
    report = await IntakeService(settings, engine).ingest_photo(7, jpeg(), caption="baha")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000)  # makes the photo a "bomb" to Pillow

    out = await service(settings, engine).assess(report.id)

    assert out.status == "assessed"
    photo = one(engine, PhotoAssessment)
    assert photo.judgment == JUDGMENT
    assert photo.overlay_media_id is None


class BrokenSam:
    async def segment(self, image, concept, *, image_mime="image/jpeg", image_url=None):
        raise RuntimeError("bug in mask parsing")


async def test_an_unexpected_photo_error_keeps_the_rest_of_the_report(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo(7, jpeg(), caption="baha sa Casile")
    out = await AssessmentService(settings, engine, FakeLLM(), BrokenSam()).assess(report.id)

    assert out.status == "assessment_failed"
    assert out.barangay == "Casile"  # the caption's facts and place were still saved
    assert one(engine, Assessment).facts is not None
    assert "RuntimeError: bug in mask parsing" in one(engine, PhotoAssessment).error

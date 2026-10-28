"""Deterministic photo/location evidence, with procedural synthetic images only."""

import io
import random
from datetime import datetime, timedelta

import pytest
from PIL import Image, ImageDraw, ImageOps
from sqlmodel import Session, select

from app import trust
from app.assessment import AssessmentService
from app.db import init_db, make_engine
from app.incidents import IncidentService
from app.intake import IntakeService
from app.models import Assessment, Incident, Media, PhotoAssessment, Report, Reporter
from app.retention import purge_expired
from tests.test_assessment import FakeLLM, FakeSam
from tests.test_geo import CENTRES, jpeg_with_gps
from tests.test_incidents import FakeJudge


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def flood_scene(seed=1):
    """Two distinct invented flooded streets; no downloaded or real-person imagery."""
    rng = random.Random(seed)
    image = Image.new("RGB", (320, 240), (130, 160, 190))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 145, 320, 240), fill=(50, 100, 130))
    for _ in range(22):
        x, y = rng.randrange(290), rng.randrange(30, 180)
        w, h = rng.randrange(12, 60), rng.randrange(12, 65)
        color = tuple(rng.randrange(40, 230) for _ in range(3))
        draw.rectangle((x, y, x + w, y + h), fill=color)
        draw.polygon(((x - 5, y), (x + w // 2, y - 15), (x + w + 5, y)), fill="gray")
    return image


def encoded(image, *, quality=95, exif=None):
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, **({"exif": exif} if exif is not None else {}))
    return out.getvalue()


def row(engine, model, report_id):
    with Session(engine) as session:
        query = select(model).where(model.report_id == report_id)
        if model is Media:
            query = query.where(Media.kind == "photo")
        return session.exec(query).one()


async def signals_for(settings, engine, report):
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    await service.assess(report.id)
    return row(engine, Assessment, report.id).signals


async def test_reencoded_half_size_photo_is_reused_but_distinct_flood_is_not(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_photo("a", encoded(flood_scene()))
    second = await intake.ingest_photo("b", encoded(flood_scene().resize((160, 120)), quality=60))
    third = await intake.ingest_photo("c", encoded(flood_scene(83)))
    signals = await signals_for(settings, engine, second)
    reused = next(s for s in signals if s["code"] == "photo_reused")
    assert reused["severity"] == "high" and reused["related_report_id"] == first.id
    assert str(first.id) in reused["detail"]
    assert not await signals_for(settings, engine, third)
    for report in (first, second, third):
        media = row(engine, Media, report.id)
        assert len(media.phash) == 16


async def test_sha_match_without_readable_hash_and_same_reporter_resend(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_photo("a", b"invalid but identical image bytes")
    resent = await intake.ingest_photo("a", b"invalid but identical image bytes")
    other = await intake.ingest_photo("b", b"invalid but identical image bytes")
    assert row(engine, Media, first.id).phash == trust.HASH_UNAVAILABLE
    assert (await signals_for(settings, engine, resent))[0]["code"] == "photo_reused"
    # Cross-reporter reuse takes priority. Delete that third report's photo to isolate resend.
    with Session(engine) as session:
        session.delete(session.exec(select(Media).where(Media.report_id == other.id)).one())
        session.commit()
    signal = (await signals_for(settings, engine, resent))[0]
    assert signal["code"] == "photo_resent" and signal["severity"] == "info"
    assert signal["related_report_id"] == first.id


@pytest.mark.parametrize("days", [30, -2, 3, -1, None])
async def test_capture_time_is_manila_local_and_unknown_is_not_flagged(settings, engine, days):
    received = datetime(2026, 10, 28, 4)
    exif = Image.Exif()
    if days is not None:
        exif[0x8769] = {
            0x9003: (received - timedelta(days=days) + timedelta(hours=8)).strftime(
                "%Y:%m:%d %H:%M:%S"
            )
        }
    report = await IntakeService(settings, engine).ingest_photo(
        "a", encoded(flood_scene(), exif=exif)
    )
    with Session(engine) as session:
        r = session.get(Report, report.id)
        r.received_at = received
        session.add(r)
        session.commit()
    signals = await signals_for(settings, engine, report)
    assert any(s["code"] == "photo_old" for s in signals) == (days in (30, -2))
    photo = row(engine, PhotoAssessment, report.id)
    assert photo.taken_at == (received - timedelta(days=days) if days is not None else None)


@pytest.mark.parametrize("value", ["not a date", "2026:99:99 10:00:00", b"\xff"])
def test_malformed_exif_is_unknown(value):
    exif = Image.Exif()
    exif[0x9003] = value
    assert trust.photo_taken_at(encoded(flood_scene(), exif=exif)) is None


def test_hash_respects_exif_orientation_and_invalid_input():
    image = flood_scene()
    exif = Image.Exif()
    exif[0x0112] = 6
    data = encoded(image.rotate(90, expand=True), exif=exif)
    with Image.open(io.BytesIO(data)) as loaded:
        expected = trust.photo_hash(encoded(ImageOps.exif_transpose(loaded)))
    assert (int(trust.photo_hash(data), 16) ^ int(expected, 16)).bit_count() <= 1
    assert trust.photo_hash(b"bad") is None
    assert trust.photo_taken_at(b"bad") is None


@pytest.mark.parametrize(
    ("point", "text", "code"),
    [
        (CENTRES["Malaban"], "dito sa Casile", "pin_text_mismatch"),
        ((14.5995, 120.9842), "dito sa Casile", "pin_outside_binan"),
        (CENTRES["Malaban"], "dito sa Malaban", None),
        (CENTRES["Malaban"], "Malaban at Casile", None),
        (None, "dito sa Casile", None),
    ],
)
async def test_location_signals_preserve_geolocation_choice(settings, engine, point, text, code):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_text("a", text)
    if point:
        await intake.ingest_location("a", *point)
    signals = await signals_for(settings, engine, report)
    assert [s["code"] for s in signals] == ([code] if code else [])
    if point == CENTRES["Malaban"]:
        assert row(engine, Assessment, report.id).geo_source == "pin"


async def test_exif_point_compares_with_transcript(settings, engine):
    # Zapote GPS is retained on PhotoAssessment, separately from the selected report pin.
    report = await IntakeService(settings, engine).ingest_photo(
        "a", jpeg_with_gps((14, 18, 55.33), "N", (121, 5, 2.47), "E")
    )
    with Session(engine) as session:
        r = session.get(Report, report.id)
        r.transcript = "dito sa Casile"
        session.add(r)
        session.commit()
    signals = await signals_for(settings, engine, report)
    assert signals[0]["code"] == "pin_text_mismatch"
    assert "Zapote" in signals[0]["detail"]


async def test_lazy_hash_and_capture_evidence_survive_purge_and_reassessment(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_photo("a", encoded(flood_scene()))
    with Session(engine) as session:
        media = session.exec(select(Media).where(Media.report_id == first.id)).one()
        media.phash = None  # old database row
        session.add(media)
        session.commit()
    exif = Image.Exif()
    exif[0x9003] = "2020:01:01 12:00:00"
    second = await intake.ingest_photo("b", encoded(flood_scene(), exif=exif))
    before = await signals_for(settings, engine, second)
    assert {s["code"] for s in before} == {"photo_old", "photo_reused"}
    assert row(engine, Media, first.id).phash
    from app.models import utcnow

    purge_expired(settings, engine, now=utcnow() + timedelta(days=2))
    assert row(engine, Media, first.id).phash
    assert not (settings.data_dir / row(engine, Media, first.id).path).exists()
    assert await signals_for(settings, engine, second) == before
    assert row(engine, PhotoAssessment, second.id).taken_at == datetime(2020, 1, 1, 4)
    settings.photo_hash_max_distance = 0
    with Session(engine) as session:
        r = session.get(Report, second.id)
        r.lat, r.lon, r.text = *CENTRES["Malaban"], "dito sa Casile"
        session.add(r)
        session.commit()
    signals = await signals_for(settings, engine, second)
    assert sum(s["code"] == "photo_old" for s in signals) == 1  # replaced, never appended
    assert any(s["code"] == "pin_text_mismatch" for s in signals)


async def test_incident_counts_distinct_reporters_and_never_sends_trust_to_judge(settings, engine):
    intake = IntakeService(settings, engine)
    judge = FakeJudge()
    service = AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), IncidentService(settings, engine, judge)
    )
    reports = []
    for user in ("a", "a", "b"):
        r = await intake.ingest_text(user, "baha sa Malaban")
        await intake.ingest_location(user, *CENTRES["Malaban"])
        await service.assess(r.id)
        reports.append(r)
    with Session(engine) as session:
        incident = session.exec(select(Incident).where(Incident.active == True)).one()  # noqa: E712
        assert incident.latest_facts["report_count"] == 3
        assert incident.latest_facts["reporter_count"] == 2
        assert incident.latest_facts["flagged_report_count"] == 0
        pseudonyms = [r.pseudonym for r in session.exec(select(Reporter)).all()]
    prompt = str(judge.calls)
    assert all(p not in prompt for p in pseudonyms)
    assert "reporter_id" not in prompt and "signals" not in prompt


async def test_reassessment_keeps_exif_location_after_retention(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo(
        "a", jpeg_with_gps((14, 18, 55.33), "N", (121, 5, 2.47), "E")
    )
    service = AssessmentService(settings, engine, FakeLLM(), FakeSam())
    before = await service.assess(report.id)
    from app.models import utcnow

    purge_expired(settings, engine, now=utcnow() + timedelta(days=2))
    after = await service.assess(report.id)
    assert before.barangay == after.barangay == "Zapote"
    assert before.incident_id == after.incident_id
    assert row(engine, Assessment, report.id).geo_source == "exif"


async def test_trust_only_changes_refresh_stored_rollup_without_judge_calls(settings, engine):
    intake = IntakeService(settings, engine)
    judge = FakeJudge()
    service = AssessmentService(
        settings, engine, FakeLLM(), FakeSam(), IncidentService(settings, engine, judge)
    )
    first = await intake.ingest_photo("a", encoded(flood_scene()), caption="baha sa Malaban")
    await service.assess(first.id)
    second = await intake.ingest_photo("b", encoded(flood_scene()), caption="baha sa Malaban")
    await service.assess(second.id)
    with Session(engine) as session:
        incident = session.exec(select(Incident).where(Incident.active == True)).one()  # noqa: E712
        assert incident.latest_facts["flagged_report_count"] == 1
    calls = len(judge.calls)
    await service.assess(first.id)  # discovers another reporter's later copy
    with Session(engine) as session:
        incident = session.get(Incident, incident.id)
        assert incident.latest_facts["flagged_report_count"] == 2
    assert len(judge.calls) == calls


async def test_hash_distance_setting_is_inclusive_and_recomputed(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_photo("a", b"one")
    second = await intake.ingest_photo("b", b"two")
    with Session(engine) as session:
        for report, value in ((first, "0000000000000000"), (second, "0000000000000003")):
            media = session.exec(select(Media).where(Media.report_id == report.id)).one()
            media.phash = value
            session.add(media)
        session.commit()
    settings.photo_hash_max_distance = 1
    assert not await signals_for(settings, engine, second)
    settings.photo_hash_max_distance = 2
    signal = (await signals_for(settings, engine, second))[0]
    assert signal["code"] == "photo_reused" and signal["related_report_id"] == first.id

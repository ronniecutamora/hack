"""Spark and SAM 3.1 check each other on every photo (app.crosscheck): each rule, the trust signal
it raises, and the Review question it becomes, answered with the existing signal action."""

import asyncio
import html as html_lib

import pytest
from sqlmodel import Session, select

from app import crosscheck, trust
from app.assessment import AssessmentService
from app.counting import collect
from app.intake import IntakeService
from app.meta.sam import parse_stream
from app.models import OFFICER_SIGNAL_REVIEWED, Assessment, Media, PhotoAssessment, utcnow
from app.modes import PLAIN_SIGNALS
from app.questions import SIGNAL, SIGNAL_ASKS
from tests.test_assessment import FakeLLM, jpeg
from tests.test_incidents import FakeJudge
from tests.test_photo import JUDGMENT
from tests.test_questions import actions, answers, item, items, simple_client, tap
from tests.test_sam import STREAM

FLOOD = {"floodwater_present": True, "roof_count": 2}
DRY = {"floodwater_present": False, "roof_count": 2}
NO_ROOF = {"floodwater_present": False, "roof_count": 0}


def fired(judgment, measurement):
    return [c.code for c in crosscheck.disagreements(judgment, measurement, asked_only=False)]


@pytest.mark.parametrize(
    ("judgment", "measurement", "codes"),
    [
        ({"floodwater": True, "roof_damage": "none"}, FLOOD, []),  # both see floodwater
        ({"floodwater": False, "roof_damage": "none"}, DRY, []),  # neither does
        ({"floodwater": True, "roof_damage": "none"}, DRY, ["floodwater_not_outlined"]),
        ({"floodwater": False, "roof_damage": "none"}, FLOOD, ["floodwater_not_judged"]),
        ({"floodwater": False, "roof_damage": "partial"}, NO_ROOF, ["roof_not_outlined"]),
        ({"floodwater": False, "roof_damage": "severe"}, NO_ROOF, ["roof_gone"]),
        ({"floodwater": False, "roof_damage": "partial"}, DRY, []),  # SAM found 2 roofs
        ({"floodwater": False, "roof_damage": "none"}, NO_ROOF, []),  # no damage claimed
        ({"floodwater": False, "roof_damage": "unknown"}, NO_ROOF, []),
        (
            {"floodwater": True, "roof_damage": "partial"},
            NO_ROOF,
            ["floodwater_not_outlined", "roof_not_outlined"],
        ),
    ],
)
def test_each_rule_fires_only_when_the_judgment_and_the_measurement_disagree(
    judgment, measurement, codes
):
    assert fired(judgment, measurement) == codes


@pytest.mark.parametrize(
    ("judgment", "measurement"),
    [(None, DRY), ({"floodwater": True}, None), ({}, DRY), ({"floodwater": True}, {})],
)
def test_a_missing_judgment_or_measurement_never_fires(judgment, measurement):
    assert fired(judgment, measurement) == []


def test_only_the_rules_that_ask_become_trust_signals():
    photo = PhotoAssessment(
        report_id=1,
        media_id=7,
        llm_model="fake",
        judgment={"floodwater": False, "roof_damage": "severe"},  # floodwater_not_judged, roof_gone
        measurement={"floodwater_present": True, "roof_count": 0},
    )
    assert crosscheck.signals([photo]) == []
    photo.judgment = {"floodwater": True, "roof_damage": "partial"}
    photo.measurement = NO_ROOF
    (flood, roof) = crosscheck.signals([photo])
    assert (flood["code"], roof["code"]) == ("floodwater_not_outlined", "roof_not_outlined")
    assert flood["media_id"] == 7 and flood["detail"].startswith("Photo 7: Spark judged floodwater")
    assert trust.flagging(flood) and trust.flagging(roof)  # the report waits for the officer


def test_every_asking_rule_is_worded_once_for_the_officer():
    codes = [c.code for c in crosscheck.CHECKS]
    assert len(codes) == len(set(codes))
    asked = {c.code for c in crosscheck.ASKED}
    assert asked == set(SIGNAL_ASKS) and asked <= set(PLAIN_SIGNALS)
    assert all(c.spark_sees for c in crosscheck.ASKED)  # "Yes" agrees with the photo check


@pytest.fixture
def client(settings):
    with simple_client(settings, FakeJudge()) as client:
        client.post("/initials", data={"officer": "JDC"})
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


class NoFloodSam:
    """SAM outlines the roofs but no floodwater (the floodwater stream is empty)."""

    async def segment(self, image, concept, *, image_mime="image/jpeg", image_url=None):
        return parse_stream(STREAM if concept == "roof" else "", concept)


def flooded_photo_report(client, settings):
    """A photo Spark judges flooded (tests.test_photo.JUDGMENT) and SAM outlines no water in."""
    engine = client.app.state.engine
    report = asyncio.run(
        IntakeService(settings, engine).ingest_photo(7, jpeg(), caption="Baha dito sa Casile")
    )
    service = AssessmentService(settings, engine, FakeLLM(), NoFloodSam())
    assert asyncio.run(service.assess(report.id)).status == "assessed"
    with Session(engine) as session:
        photo = session.exec(select(PhotoAssessment)).one()
        assessment = session.exec(select(Assessment)).one()
    assert photo.judgment == JUDGMENT and photo.measurement["floodwater_present"] is False
    (signal,) = [s for s in assessment.signals if s["code"] == "floodwater_not_outlined"]
    return report, photo, trust.signal_key(signal)


def flagged(engine) -> list[int]:
    with Session(engine) as session:
        return [i for b in collect(session).barangays.values() for x in b.incidents
                for i in x.flagged_report_ids]  # fmt: skip


def test_a_disagreement_is_asked_with_the_photo_and_its_outline_and_no_keeps_it_doubtful(
    client, engine, settings
):
    report, photo, key = flooded_photo_report(client, settings)
    assert flagged(engine) == [report.id]  # flagged until the officer answers

    page = client.get("/review").text
    qkey = f"q-{report.id}-{SIGNAL}-{key}"
    assert qkey in items(page)
    question = html_lib.unescape(item(page, qkey))
    assert (
        "The photo check and the outline disagree about floodwater in this photo. "
        "Is there floodwater?"
    ) in question
    assert f'src="/media/{photo.media_id}"' in question  # the photo
    assert f'src="/media/{photo.overlay_media_id}"' in question  # beside its outline
    assert "It is evidence, not the loss figure." in question
    assert [a[0] for a in answers(question)] == ["Yes, floodwater", "No floodwater"]

    assert tap(client, question, "No floodwater").status_code == 303
    reviewed = actions(engine)[-1]
    assert reviewed.kind == OFFICER_SIGNAL_REVIEWED and reviewed.officer == "JDC"
    assert reviewed.detail == {"key": key, "code": "floodwater_not_outlined", "verdict": "problem"}
    assert reviewed.note == "Officer looked at the photo: no floodwater"
    assert qkey not in items(client.get("/review").text)  # answered
    assert flagged(engine) == [report.id]  # kept as doubtful, like any signal marked a problem


def test_yes_agrees_with_the_photo_check_and_clears_the_flag(client, engine, settings):
    report, _photo, key = flooded_photo_report(client, settings)
    page = client.get("/review").text
    question = item(page, f"q-{report.id}-{SIGNAL}-{key}")
    assert tap(client, question, "Yes, floodwater").status_code == 303
    reviewed = actions(engine)[-1]
    assert reviewed.detail["verdict"] == "genuine"
    assert reviewed.note == "Officer looked at the photo: floodwater"
    assert flagged(engine) == []


def test_a_deleted_photo_is_still_asked_about_without_the_pair(client, engine, settings):
    report, _photo, key = flooded_photo_report(client, settings)
    with Session(engine) as session:
        for media in session.exec(select(Media).where(Media.report_id == report.id)).all():
            media.purged_at = utcnow()
            session.add(media)
        session.commit()
    question = item(client.get("/review").text, f"q-{report.id}-{SIGNAL}-{key}")
    assert "Is there floodwater?" in html_lib.unescape(question)
    assert "/media/" not in question and "q__pair" not in question

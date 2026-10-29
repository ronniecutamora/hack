from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.assessment import AssessmentService
from app.intake import IntakeService
from app.main import create_app, fact_lines
from app.models import Assessment, Media
from tests.test_assessment import FakeLLM, FakeSam, facts, jpeg


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_reports_empty_state(client):
    response = client.get("/reports")
    assert response.status_code == 200
    assert "No reports yet" in response.text


async def test_reports_lists_text_voice_and_media_counts(settings, client):
    intake = IntakeService(settings, client.app.state.engine)
    await intake.ingest_text(111, "Baha sa <b>Brgy. Poblacion</b>")
    await intake.ingest_photo(111, b"jpeg", caption="Bubong")
    await intake.ingest_photo(222, b"jpeg2")
    await intake.ingest_location(222, 14.33061, 121.08072)

    html = client.get("/reports").text

    assert "No reports yet" not in html
    assert "3 most recent reports" in html
    assert "Baha sa &lt;b&gt;Brgy. Poblacion&lt;/b&gt;" in html  # escaped, not rendered
    assert "<b>Brgy" not in html
    assert "Bubong" in html
    assert "14.33061, 121.08072" in html
    assert "telegram" in html
    assert "111" not in html.replace("14.33061", "").replace("121.08072", "")  # no raw ids


async def assessed_photo_report(settings, client, **fact_fields):
    engine = client.app.state.engine
    report = await IntakeService(settings, engine).ingest_photo(
        111, jpeg(), caption="Baha po sa Brgy. Malaban"
    )
    llm = FakeLLM(facts(**fact_fields))
    await AssessmentService(settings, engine, llm, FakeSam()).assess(report.id)
    with Session(engine) as session:
        return session.exec(select(Media).where(Media.kind == "overlay")).one()


async def test_assessed_report_shows_barangay_facts_and_evidence(settings, client):
    overlay = await assessed_photo_report(
        settings,
        client,
        flood_depth="knee",
        houses_flooded=23,
        needs=["water"],
        summary_en="23 houses flooded knee-deep.",
    )

    html = client.get("/reports").text

    assert "<strong>Malaban</strong>" in html
    assert "named in text: malaban" in html
    assert "23 houses flooded knee-deep." in html
    assert "flood knee-deep" in html and "23 houses flooded" in html and "needs: water" in html
    assert f'src="/media/{overlay.id}"' in html
    assert "roof damage: partial" in html
    assert "SAM measured: 2 roofs, floodwater over" in html
    assert "Agent LLM: <code>muse-spark-1.3</code>" in html
    assert "assessed" in html


async def test_media_route_serves_files_until_purged(settings, client):
    overlay = await assessed_photo_report(settings, client)

    response = client.get(f"/media/{overlay.id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8")

    with Session(client.app.state.engine) as session:  # retention purges photo and overlay
        for row in session.exec(select(Media)).all():
            row.purged_at = row.created_at + timedelta(hours=24)
            session.add(row)
        session.commit()
    assert client.get(f"/media/{overlay.id}").status_code == 404
    assert "Photo deleted after 24 h" in client.get("/reports").text


def test_unknown_media_is_404(client):
    assert client.get("/media/999").status_code == 404
    assert client.get("/media/abc").status_code == 422


async def test_media_paths_outside_the_media_folder_are_never_served(settings, client):
    (settings.data_dir / "secret.txt").write_text("x")
    with Session(client.app.state.engine) as session:
        media = Media(report_id=1, kind="photo", path="media/../secret.txt", sha256="x")
        session.add(media)
        session.commit()
        media_id = media.id
    assert client.get(f"/media/{media_id}").status_code == 404


def test_fact_lines_skip_unstated_fields_and_flag_corrections():
    lines = fact_lines(
        facts(
            is_correction=True,
            houses_totally_damaged=4,
            houses_partially_damaged=15,
            crop="corn",
            crop_area_hectares=2.5,
            crop_damage_percent=70,
        )
    )
    assert lines[0] == "CORRECTION of an earlier report"
    assert "4 houses totally damaged" in lines and "15 houses partially damaged" in lines
    assert "corn, 2.5 ha, 70% damaged" in lines
    assert not any("None" in line for line in lines)
    assert fact_lines(None) == []


def test_incidents_empty_and_missing(client):
    assert "No incidents yet" in client.get("/incidents").text
    assert client.get("/incidents/999").status_code == 404
    assert client.get("/incidents/nope").status_code == 422


async def test_incident_list_timeline_why_and_report_links(settings, client):
    from app.incidents import IncidentService
    from app.models import AgentDecision, Incident, Report
    from tests.test_incidents import AT, SAME, FakeJudge, add_report

    engine = client.app.state.engine
    service = IncidentService(settings, engine, FakeJudge())
    first_id = add_report(engine, flood_depth="knee")
    first = await service.group(first_id)
    second_id = add_report(engine, at=AT + timedelta(days=1), flood_depth="waist")
    await service.group(second_id)
    await service.group(add_report(engine, barangay="Casile", at=AT + timedelta(days=2)))
    listing = client.get("/incidents").text
    assert listing.index("Casile") < listing.index("Malaban")
    assert f"/incidents/{first.incident_id}" in listing
    assert "Latest: waist" in listing and "Max: waist" in listing
    detail = client.get(f"/incidents/{first.incident_id}").text
    assert detail.index(f"Report #{first_id}") < detail.index(f"Report #{second_id}")
    assert "Why the agent grouped these" in detail
    assert SAME["reason"] in detail and "fake-pair-judge" in detail and "97%" in detail
    assert "Synthetic demo" in detail and "double-count" in detail
    assert "knee" in detail and "waist" in detail
    reports = client.get("/reports").text
    assert f'href="/incidents/{first.incident_id}"' in reports
    assert "Synthetic demo" in reports
    # Rendering reads stored decisions even if report facts have since changed.
    with Session(engine) as session:
        for assessment in session.exec(select(Assessment)).all():
            assessment.facts = {"summary_en": "changed after grouping"}
            session.add(assessment)
        session.commit()
    assert SAME["reason"] in client.get(f"/incidents/{first.incident_id}").text
    with Session(engine) as session:
        for decision in session.exec(select(AgentDecision)).all():
            session.delete(decision)
        for report in session.exec(select(Report)).all():
            report.incident_id = None
            session.add(report)
        session.commit()
    empty = client.get(f"/incidents/{first.incident_id}").text
    assert "No reports currently linked" in empty and "No grouping decisions recorded" in empty
    assert len(client.get("/incidents").text) > 0
    with Session(engine) as session:
        assert session.get(Incident, first.incident_id) is not None


async def test_incident_timeline_evidence_and_expiry(settings, client):
    from app.models import Report

    overlay = await assessed_photo_report(settings, client)
    with Session(client.app.state.engine) as session:
        report = session.get(Report, overlay.report_id)
        incident_id = report.incident_id
    detail = client.get(f"/incidents/{incident_id}").text
    assert f'src="/media/{overlay.id}"' in detail
    with Session(client.app.state.engine) as session:
        for media in session.exec(select(Media)).all():
            media.purged_at = media.created_at
            session.add(media)
        session.commit()
    detail = client.get(f"/incidents/{incident_id}").text
    assert f'src="/media/{overlay.id}"' not in detail
    assert "Photo evidence deleted" in detail


async def test_incident_uncertainty_badge_and_escaping(settings, client):
    from app.incidents import IncidentService
    from tests.test_incidents import FakeJudge, add_report

    engine = client.app.state.engine
    judge = FakeJudge(
        {"decision": "unsure", "confidence": 0.5, "reason": "<script>unsafe</script>"}
    )
    service = IncidentService(settings, engine, judge)
    await service.group(add_report(engine))
    second = await service.group(add_report(engine))
    assert "needs officer" in client.get("/incidents").text
    detail = client.get(f"/incidents/{second.incident_id}").text
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in detail
    assert "<script>unsafe" not in detail

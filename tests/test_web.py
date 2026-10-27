import pytest
from fastapi.testclient import TestClient

from app.intake import IntakeService
from app.main import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
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
    assert html.count("<td>1</td>") == 2  # the two photo reports have one media file each

"""The officer map: boundaries, incident markers (exact or approximate) and flagged outside pins."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.gazetteer import BARANGAYS, BY_NAME
from app.geo import barangay_at, boundaries_geojson, centroid
from app.main import create_app
from app.models import Incident
from tests.test_rdana import add_incident, add_report

OUTSIDE = (14.10, 121.40)  # well outside Biñan
FLAG = [{"code": "pin_outside_binan", "severity": "medium", "detail": "Pin is outside Biñan."}]


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def features(client):
    response = client.get("/map/incidents.geojson")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/geo+json")
    return response.json()["features"]


def test_every_barangay_centre_lies_inside_its_polygon():
    for barangay in BARANGAYS:
        point = centroid(barangay.name)
        assert point is not None and barangay_at(*point) == barangay, barangay.name
    assert centroid("Nowhere") is None


def test_boundaries_use_gazetteer_names_and_are_cached(client):
    response = client.get("/map/barangays.geojson")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"
    data = response.json()
    names = {f["properties"]["name"] for f in data["features"]}
    assert names == {b.name for b in BARANGAYS} and "Biñan" in names
    assert response.content == boundaries_geojson()
    etag = response.headers["etag"]
    again = client.get("/map/barangays.geojson", headers={"if-none-match": etag})
    assert again.status_code == 304 and not again.content


def test_markers_are_exact_approximate_or_left_off_the_map(client, engine):
    exact = add_incident(engine)
    add_report(engine, incident=exact, point=(14.3511, 121.0900))
    add_report(engine, incident=exact, reporter="b", point=(14.3521, 121.0910))
    add_report(engine, incident=exact, reporter="c", point=OUTSIDE)  # not averaged in
    approximate = add_incident(engine, barangay="Casile", status="needs_officer")
    add_report(engine, incident=approximate, barangay="Casile", point=None)
    nowhere = add_incident(engine, barangay=None)
    add_report(engine, incident=nowhere, barangay=None, point=None)
    retired = add_incident(engine, barangay="Loma")
    with Session(engine) as session:
        session.get(Incident, retired).active = False
        session.commit()

    by_id = {f["properties"].get("id"): f for f in features(client)}
    assert set(by_id) == {exact, approximate}
    first = by_id[exact]
    assert first["geometry"]["coordinates"] == [121.0905, 14.3516]
    assert first["properties"]["approximate"] is False and first["properties"]["points"] == 2
    second = by_id[approximate]["properties"]
    assert second["approximate"] is True and second["status_label"] == "Needs officer review"
    lon, lat = by_id[approximate]["geometry"]["coordinates"]
    assert barangay_at(lat, lon) == BY_NAME["Casile"]

    html = client.get("/map").text
    assert '<a href="/map" aria-current="page">Map</a>' in html
    assert 'id="live-map-list" data-live="incidents reports"' in html
    assert ">approximate</span>" in html and "GPS of 2 reports (mean)" in html
    assert "Not on the map (no GPS and no barangay)" in html and f"/incidents/{nowhere}" in html
    for text in ("Needs officer review (square)", "Approximate:", "outside Biñan"):
        assert text in html  # the legend says it in words, not colour alone


def test_only_flagged_reports_outside_binan_are_shown(client, engine):
    add_report(engine, barangay=None, point=OUTSIDE, signals=FLAG)
    add_report(engine, reporter="b", barangay=None, point=(14.11, 121.41))  # not flagged
    outside = [f for f in features(client) if f["properties"]["kind"] == "outside"]
    assert len(outside) == 1
    assert outside[0]["geometry"]["coordinates"] == [OUTSIDE[1], OUTSIDE[0]]
    report_id = outside[0]["properties"]["report_id"]
    assert outside[0]["properties"]["url"] == f"/reports?around={report_id}#report-{report_id}"
    assert f"report #{report_id}</a>" in client.get("/map").text


def test_map_data_carries_no_report_text_or_reporter_ids(client, engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, text="Si Juan Dela Cruz ang nag-report")
    add_report(engine, reporter="b", barangay=None, point=OUTSIDE, signals=FLAG)
    raw = json.dumps(features(client))
    assert "Juan" not in raw and "pseudonym-" not in raw and "Pin is outside" not in raw


def test_map_page_works_without_data_and_loads_vendored_leaflet(client):
    html = client.get("/map").text
    assert "No active incidents yet" in html
    assert "vendor/leaflet-1.9.4/leaflet.js" in html and "js/map.js" in html
    assert "unpkg.com" not in html and "cdn" not in html.lower()
    assert client.get("/static/vendor/leaflet-1.9.4/LICENSE").status_code == 200
    assert 'data-live-map data-compact="true"' in client.get("/").text

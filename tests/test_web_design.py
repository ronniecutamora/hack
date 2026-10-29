"""Dashboard layout: header, navigation state, static assets and print rules."""

import pytest
from fastapi.testclient import TestClient

from app.main import NAV, create_app, nav_current, static_url


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client


def test_header_names_the_product_and_marks_only_the_current_page(client):
    html = client.get("/incidents").text
    assert "Ulat" in html and "Biñan City DRRM · typhoon and flood reports" in html
    assert '<a href="/incidents" aria-current="page">Incidents</a>' in html
    assert html.count('aria-current="page"') == 1
    assert '<a href="/reports">Reports</a>' in html


@pytest.mark.parametrize(
    ("path", "href", "current"),
    [
        ("/incidents/7", "/incidents", True),
        ("/drafts/3", "/drafts", True),
        ("/damaged-houses", "/drafts", False),
        ("/drafts-old", "/drafts", False),
    ],
)
def test_nav_current_covers_pages_below_a_section(path, href, current):
    assert nav_current(path, href) is current


def test_every_nav_entry_is_a_page(client):
    for href, _label in NAV:
        assert client.get(href).status_code == 200, href


def test_static_assets_are_served_with_a_content_hash(client):
    url = static_url("css/app.css")
    assert url.startswith("/static/css/app.css?v=")
    response = client.get(url)
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert static_url("css/app.css") in client.get("/reports").text
    assert client.get("/static/../main.py").status_code == 404


def test_print_rules_hide_the_dashboard_chrome(client):
    css = client.get(static_url("css/app.css")).text
    print_rules = css[css.index("@media print") :]
    assert ".site-header" in print_rules and ".no-print" in print_rules
    assert "@page { size: A4" in css

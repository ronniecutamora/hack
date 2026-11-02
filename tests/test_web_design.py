"""Dashboard layout: app bar, navigation, the page shell, static assets and print rules."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.intake import IntakeService
from app.main import NAV, create_app, nav_current, static_url
from app.models import STATUS_NOT_A_REPORT, Report
from app.nav import NAV_BAR_MAX, PRIMARY, SECONDARY, more_current
from scripts.build_icons import ICONS, NAV_ICONS


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client


def test_header_names_the_product_and_marks_only_the_current_page(client):
    html = client.get("/incidents").text
    assert "Ulat" in html and "Biñan City DRRM · typhoon and flood reports" in html
    assert '<a href="/incidents" aria-current="page" class="nav__item">' in html
    assert html.count('aria-current="page"') == 1
    assert '<a href="/reports" class="nav__item">' in html


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
    for item in NAV:
        assert client.get(item.href).status_code == 200, item.href


def test_the_bottom_bar_fits_material_3_and_more_holds_the_rest():
    # The bar and the rail show the primary destinations plus "More".
    assert len(PRIMARY) + 1 <= NAV_BAR_MAX
    assert set(PRIMARY) | set(SECONDARY) == set(NAV)
    assert more_current("/map") and more_current("/accuracy")
    assert not more_current("/") and not more_current("/drafts/3")


def test_the_more_button_shows_when_its_page_is_open(client):
    html = client.get("/map").text
    assert 'class="nav__item nav__more is-current"' in html
    assert '<a href="/map" aria-current="page" class="nav__item">' in html
    assert 'class="nav__item nav__more"' in client.get("/").text


def test_every_icon_a_page_uses_is_in_the_sprite(client):
    sprite = client.get(static_url("vendor/material-symbols/icons.svg")).text
    symbols = set(re.findall(r'<symbol id="([^"]+)"', sprite))
    assert {i.icon for i in NAV} <= set(NAV_ICONS)
    assert set(ICONS) | set(NAV_ICONS) | {f"{n}-fill" for n in NAV_ICONS} == symbols
    for path in ("/", "/review", "/drafts", "/map", "/damaged-houses"):
        used = set(re.findall(r'icons\.svg\?v=\w+#([\w-]+)"', client.get(path).text))
        assert used and used <= symbols, path


def test_pages_load_into_the_shell_and_keep_working_without_javascript(client):
    html = client.get("/drafts").text
    body = html[html.index("<body") : html.index(">", html.index("<body"))]
    assert 'hx-boost="true"' in body and 'hx-target="#shell"' in body
    assert 'hx-select="#shell"' in body
    assert '<div id="shell" class="shell" hx-history-elt>' in html
    # The shell holds the navigation and the page, so both change together on Back and Forward.
    shell = html[html.index('id="shell"') :]
    assert shell.index('class="nav"') < shell.index('<main id="main"')
    # Downloads, photos, sign-in and sign-out are full page loads.
    assert 'href="/damaged-houses.csv" download hx-boost="false"' in html


def test_the_map_loads_leaflet_only_when_a_page_shows_it(client):
    overview = client.get("/").text
    assert "leaflet.js" not in overview.split("data-assets=")[0]
    assets = re.search(r'data-assets="([^"]+)"', overview).group(1).split()
    assert [a.split("?")[0] for a in assets] == [
        "/static/vendor/leaflet-1.9.4/leaflet.css",
        "/static/vendor/leaflet-1.9.4/leaflet.js",
        "/static/js/map.js",
    ]
    for url in assets:
        assert client.get(url).status_code == 200, url
    assert "leaflet" not in client.get("/drafts").text


def test_static_assets_are_served_with_a_content_hash(client):
    url = static_url("css/app.css")
    assert url.startswith("/static/css/app.css?v=")
    response = client.get(url)
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert static_url("css/app.css") in client.get("/reports").text
    assert client.get("/static/../main.py").status_code == 404


def test_fonts_and_icons_are_self_hosted_with_their_licenses(client):
    css = client.get(static_url("css/app.css")).text
    assert "font-display: optional" in css and "../vendor/roboto/roboto-latin-wght.woff2" in css
    assert "fonts.googleapis.com" not in css and "fonts.gstatic.com" not in css
    for path in (
        "/static/vendor/roboto/roboto-latin-wght.woff2",
        "/static/vendor/roboto/OFL.txt",
        "/static/vendor/material-symbols/LICENSE",
    ):
        assert client.get(path).status_code == 200, path


APP = Path(__file__).resolve().parent.parent / "app"


def test_an_element_marked_hidden_stays_hidden_where_its_class_sets_display():
    """A class rule with `display: flex` beats the browser's own [hidden] rule, so an element
    that starts hidden (Review's "All done" under a waiting question) needs a `[hidden]` rule."""
    css = re.sub(r"/\*.*?\*/", "", (APP / "static" / "css" / "app.css").read_text(), flags=re.S)
    shown, kept_hidden = set(), set()
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for selector in (s.strip() for s in selectors.split(",")):
            if (m := re.fullmatch(r"\.([\w-]+)\[hidden\]", selector)) and "display: none" in body:
                kept_hidden.add(m.group(1))
            elif (m := re.fullmatch(r"\.([\w-]+)", selector)) and re.search(
                r"display: (?!none)", body
            ):
                shown.add(m.group(1))
    leaks = [
        f"{template.name}: {tag}"
        for template in sorted((APP / "templates").rglob("*.html"))
        for tag in re.findall(r"<[a-z][^<>]*>", template.read_text(encoding="utf-8"))
        if re.search(r"\shidden(?=[\s>])", tag)
        and (
            classes := set(re.search(r'class="([^"]*)"', tag)[1].split())
            if 'class="' in tag
            else set()
        )
        & shown
        and not classes & kept_hidden
    ]
    assert leaks == [], leaks


def test_print_rules_hide_the_dashboard_chrome(client):
    css = client.get(static_url("css/app.css")).text
    print_rules = css[css.index("@media print") :]
    assert ".app-bar" in print_rules and ".nav" in print_rules and ".no-print" in print_rules
    assert "@page { size: A4" in css


def test_pages_and_assets_are_compressed_fonts_are_not(client):
    gzip = {"Accept-Encoding": "gzip"}
    for url in ("/drafts", static_url("css/app.css"), static_url("js/app.js")):
        assert client.get(url, headers=gzip).headers.get("content-encoding") == "gzip", url
    font = client.get("/static/vendor/roboto/roboto-latin-wght.woff2", headers=gzip)
    assert "content-encoding" not in font.headers  # already compressed


async def test_a_screened_out_message_has_its_own_list_with_a_chip_and_restore(settings, client):
    intake = IntakeService(settings, client.app.state.engine)
    report = await intake.ingest_text(333, "hello po")
    with Session(client.app.state.engine) as session:
        row = session.get(Report, report.id)
        row.status = STATUS_NOT_A_REPORT
        row.screening = {
            "source": "rule",
            "verdict": "no",
            "code": "greeting",
            "reason": "a greeting",
        }
        session.add(row)
        session.commit()
    reports = client.get("/reports").text
    assert f'id="report-{report.id}"' not in reports  # set aside, so not among the reports
    assert (
        '1 message set aside by the not-a-report screen: <a href="/reports/set-aside">' in reports
    )
    html = client.get("/reports/set-aside").text
    assert 'filter_alt_off"></use></svg>Set aside</span>' in html and "a greeting" in html
    assert f'action="/reports/{report.id}/review/restore"' in html
    done = client.post(
        f"/reports/{report.id}/review/restore", data={"officer": "JDC", "next": "/review"}
    )
    assert done.status_code == 200 and "restored as a report" in done.text

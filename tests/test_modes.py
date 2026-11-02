"""Simple and Technical modes (app.modes): how a browser's mode is chosen, what each mode shows,
and that both modes offer exactly the same officer actions."""

import asyncio
import html as html_lib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.dashboard import ago
from app.incidents import IncidentService
from app.intake import IntakeService
from app.main import create_app
from app.models import STATUS_NOT_A_REPORT, Report
from app.modes import GLOSSARY, GUIDE_COOKIE, MODE_COOKIE, plain_reason, resolve
from app.review import queue
from tests.test_incidents import UNSURE, FakeJudge
from tests.test_incidents import add_report as add_grouped_report
from tests.test_nol import AT, Clock, add_photo, fisherfolk_incident
from tests.test_rdana import SummaryLLM, cite_all
from tests.test_trace_web import assessed

# What Simple mode never shows: model names (the fakes' too), costs, ids, confidence numbers,
# the agent trace, cache, internal statuses.
TECHNICAL_WORDS = re.compile(
    r"#\d+|\$\d|fake-[a-z-]+|\bMuse\b|\bSpark\b|\bSAM\b|Llama|Baseten|Agent trace|Meta model"
    r"|[Cc]onfidence|from cache|needs_officer|not_a_report|kappa|\bjson\b",
)
# Markup Simple mode does not ship at all.
TECHNICAL_MARKUP = ('data-details-key="trace-', "models-strip", 'class="trace', "<code>")


def visible_text(page: str) -> str:
    """The page's title and body as a reader sees them: no tags, scripts or attributes."""
    title = re.search(r"<title>(.*?)</title>", page, re.S).group(1)
    body = page[page.index("<body") :]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S)
    return html_lib.unescape(re.sub(r"\s+", " ", f"{title} {re.sub(r'<[^>]+>', ' ', body)}"))


def forms(page: str) -> set[tuple[str, tuple[str, ...]]]:
    """Every form on the page: where it posts and the fields it sends."""
    found = set()
    for match in re.finditer(r"<form\b([^>]*)>(.*?)</form>", page, re.S):
        action = re.search(r'action="([^"]*)"', match.group(1)).group(1)
        names = tuple(sorted(set(re.findall(r'name="([^"]+)"', match.group(2)))))
        found.add((action, names))
    return found


@pytest.fixture
def clock():
    return Clock(AT + timedelta(hours=30))  # the Notice of Loss is due in 18 hours


@pytest.fixture
def judge():
    return FakeJudge()


def make_client(settings, clock, judge, mode):
    app = create_app(
        settings.model_copy(update={"dashboard_mode": mode}),
        summary_llm=SummaryLLM(*[cite_all] * 20),
        pair_judge=judge,
        clock=clock,
    )
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def client(settings, clock, judge):
    with make_client(settings, clock, judge, "simple") as client:
        yield client


def seed(client, settings, judge) -> dict[str, int]:
    """A fisherfolk loss with a photo, an uncertain grouping, and a report with a full trace."""
    engine = client.app.state.engine
    incident, first, _second = fisherfolk_incident(engine)
    add_photo(engine, first)
    service = IncidentService(settings, engine, judge)
    asyncio.run(service.group(add_grouped_report(engine, barangay="Casile", reporter="aa")))
    judge.replies = [UNSURE]
    uncertain = asyncio.run(
        service.group(
            add_grouped_report(engine, barangay="Casile", at=AT + timedelta(hours=1), reporter="bb")
        )
    )
    assert uncertain.grouping_needs_officer
    traced = asyncio.run(assessed(settings, engine, user=222))
    return {"incident": incident, "uncertain": uncertain.id, "traced": traced}


def pages(client, ids) -> list[str]:
    nol = client.post(
        "/drafts/notice-of-loss", data={"commodity": "fisheries", "incident_id": ids["incident"]}
    )
    draft = nol.url.path
    return [
        "/",
        "/review",
        "/incidents",
        f"/incidents/{ids['incident']}",
        "/reports",
        "/reports/set-aside",
        "/drafts",
        draft,
        "/map",
        "/damaged-houses",
        "/accuracy",
        "/help",
    ]


def test_simple_is_the_default_and_a_mode_link_is_remembered_by_the_browser(client):
    assert "<h1>All caught up</h1>" in client.get("/").text
    switched = client.get("/reports?around=5&mode=technical", follow_redirects=False)
    assert switched.status_code == 303
    assert switched.headers["location"] == "/reports?around=5"
    cookie = switched.headers["set-cookie"]
    assert f"{MODE_COOKIE}=technical" in cookie and "HttpOnly" in cookie and "Max-Age=" in cookie
    assert "<h1>Overview</h1>" in client.get("/").text  # the cookie now says Technical
    assert "<h1>Overview</h1>" in client.get("/?mode=nonsense").text  # unknown values ignored
    client.get("/?mode=simple")
    assert "<h1>All caught up</h1>" in client.get("/").text


def test_the_configured_default_applies_to_a_browser_without_a_choice(settings, clock, judge):
    with make_client(settings, clock, judge, "technical") as client:
        assert "<h1>Overview</h1>" in client.get("/").text
    assert resolve(None, None, "simple") == "simple"
    assert resolve("technical", "simple", "simple") == "technical"
    assert resolve("bogus", "technical", "simple") == "technical"


def test_simple_pages_name_no_models_costs_ids_or_agent_trace(client, settings, judge):
    for path in pages(client, seed(client, settings, judge)):
        page = client.get(path).text
        leaks = sorted({m.group(0) for m in TECHNICAL_WORDS.finditer(visible_text(page))})
        assert leaks == [], path
        assert not [m for m in TECHNICAL_MARKUP if m in page.split("<body", 1)[1]], path


def test_technical_pages_still_show_the_trace_models_costs_and_ids(client, settings, judge):
    ids = seed(client, settings, judge)
    client.get("/?mode=technical")
    reports = client.get("/reports").text
    assert f'data-details-key="trace-{ids["traced"]}"' in reports
    assert "<code>fake-llm</code>" in reports and "$0.0100" in reports
    assert "Meta models at work" in client.get("/").text
    review = client.get("/review").text
    assert "<code>fake-pair-judge</code>" in review and "Confidence" in review
    assert f"Report #{ids['uncertain']}" in review
    assert 'class="accuracy' in client.get("/accuracy").text


def test_both_modes_offer_exactly_the_same_officer_actions(settings, clock):
    seen = {}
    for mode in ("simple", "technical"):
        own = settings.model_copy(update={"data_dir": settings.data_dir / mode})
        judge = FakeJudge()
        with make_client(own, clock, judge, mode) as client:
            paths = pages(client, seed(client, own, judge))
            seen[mode] = {p: forms(client.get(p).text) for p in paths if p != "/accuracy"}
    assert seen["simple"].keys() == seen["technical"].keys()
    for path in seen["simple"]:
        # The first-run guide is Simple mode's own; everything else is identical.
        simple, technical = (
            {f for f in seen[mode][path] if f[0] != "/help/guide"} for mode in seen
        )
        assert simple == technical, path
    assert any(action.endswith("/review/keep") for action, _ in seen["simple"]["/review"])


def test_home_lists_what_needs_the_officer_most_urgent_first(client, settings, judge):
    ids = seed(client, settings, judge)
    page = client.get("/").text
    titles = re.findall(r'<h2 class="todo__title">(.*?)</h2>', page)
    assert titles[0] == "De La Paz fisheries: Notice of Loss"  # due in 18 hours: first
    assert titles[1] == "1 report needs your check"
    assert "barangays have new reports for the RDANA form" in titles[2] or "RDANA" in titles[2]
    assert "due in 18h 0m" in page
    assert re.search(r'href="/drafts#nol-fisheries-\d+">Make the draft</a>', page)
    assert 'href="/review">Review</a>' in page
    assert "May be the same flood as another report" in page

    client.post(
        "/drafts/notice-of-loss", data={"commodity": "fisheries", "incident_id": ids["incident"]}
    )
    page = client.get("/").text
    assert "De La Paz fisheries: Notice of Loss ready to approve" in page
    draft = re.search(r'href="(/drafts/\d+)">Check and approve</a>', page).group(1)
    client.post(f"{draft}/approve", data={"decided_by": "CDRRMO"})
    assert "De La Paz fisheries: Notice of Loss" not in client.get("/").text


def test_home_says_all_caught_up_and_counts_messages_set_aside(client, settings):
    page = client.get("/").text
    assert "All caught up." in page and "No report waits for your check" in page
    report = asyncio.run(IntakeService(settings, client.app.state.engine).ingest_text(5, "hi po"))
    with Session(client.app.state.engine) as session:
        row = session.get(Report, report.id)
        row.status = STATUS_NOT_A_REPORT
        session.add(row)
        session.commit()
    page = client.get("/").text
    assert "1 message set aside as not reports" in page
    assert '<a href="/reports/set-aside">See them</a>' in page


def test_the_first_run_guide_hides_for_this_browser_and_comes_back_from_help(client):
    assert "How Ulat works" in client.get("/").text
    hidden = client.post("/help/guide", data={"guide": "hide", "next": "/"}, follow_redirects=False)
    assert hidden.status_code == 303 and hidden.headers["location"] == "/"
    assert f"{GUIDE_COOKIE}=hidden" in hidden.headers["set-cookie"]
    assert "How Ulat works" not in client.get("/").text
    help_page = client.get("/help").text
    assert "Show this guide on Home again" in help_page and "You check and approve" in help_page
    client.post("/help/guide", data={"guide": "show"})
    assert "How Ulat works" in client.get("/").text
    # Only Home or Help: a form cannot send the browser elsewhere.
    elsewhere = client.post(
        "/help/guide",
        data={"guide": "hide", "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert elsewhere.headers["location"] == "/"


def test_navigation_shows_only_an_officers_destinations_with_the_review_count(
    client, settings, judge
):
    seed(client, settings, judge)
    page = client.get("/").text
    nav = page[page.index('<nav class="nav"') : page.index("</nav>")]
    labels = re.findall(r'<span class="nav__label">([^<]+)</span>', nav)
    assert labels[:5] == ["Home", "Review", "Incidents", "Drafts", "More"]
    assert 'href="/accuracy"' not in nav and 'href="/help"' in nav
    with Session(client.app.state.engine) as session:
        waiting = len(queue(session))
    assert waiting == 1
    assert f'data-badge="review"><span class="visually-hidden">: </span>{waiting}<' in nav
    client.get("/?mode=technical")
    nav = client.get("/").text.split('<nav class="nav"')[1].split("</nav>")[0]
    assert ">Overview<" in nav and 'href="/accuracy"' in nav


def test_a_technical_page_opened_in_simple_mode_offers_the_switch_and_shows_nothing_of_it(
    client,
):
    page = client.get("/accuracy").text
    assert 'href="?mode=technical"' in page and "Switch to Technical mode" in page
    assert "<table" not in page and "<svg role" not in page


def test_official_terms_are_explained_in_place_in_simple_mode(client, settings, judge):
    seed(client, settings, judge)
    drafts = client.get("/drafts").text
    for key in ("rdana", "dromic", "nol"):
        assert f"<strong>{GLOSSARY[key][0]}</strong>: {GLOSSARY[key][1]}" in html_lib.unescape(
            drafts
        )
    assert "within 2 calendar days" in GLOSSARY["nol"][1]  # from the deadlines table
    assert "within 72 hours" in GLOSSARY["rdana"][1]
    client.get("/?mode=technical")
    assert "whats-this" not in client.get("/drafts").text


def test_a_review_action_is_confirmed_in_plain_words(client, settings, judge):
    ids = seed(client, settings, judge)
    page = client.post(
        f"/reports/{ids['uncertain']}/review/keep", data={"officer": "JDC", "next": "/review"}
    ).text
    assert "Done: the report stays as its own incident." in page
    assert "Not the same flood, keep separate" not in page.split("data-flash")[0]


def test_review_reasons_and_times_read_plainly():
    assert plain_reason("grouping uncertain") == "May be the same flood as another report"
    assert plain_reason("2 trust signals to check") == "2 things to double-check"
    assert plain_reason("1 trust signal to check") == "Something to double-check"
    now = AT
    assert ago(now - timedelta(seconds=20), now) == "just now"
    assert ago(now - timedelta(minutes=2), now) == "2 min ago"
    assert ago(now - timedelta(hours=1), now) == "an hour ago"
    assert ago(now - timedelta(hours=26), now) == "yesterday"

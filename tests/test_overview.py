"""The overview page: every number equals the same number on the page it links to."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import create_app
from app.models import Draft, Incident, ModelCall
from tests.test_nol import Clock, fisherfolk_incident
from tests.test_rdana import AT, SummaryLLM, add_incident, add_report, cite_all


@pytest.fixture
def clock():
    return Clock(AT + timedelta(hours=5))


@pytest.fixture
def client(settings, clock):
    app = create_app(settings, summary_llm=SummaryLLM(cite_all), clock=clock)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def kpi(html: str, name: str) -> str:
    match = re.search(rf'data-kpi="{name}">([^<]*)<', html)
    assert match, name
    return match.group(1).strip()


def seed(engine):
    open_incident = add_incident(engine)
    review = add_incident(engine, barangay="Casile", status="needs_officer")
    add_report(engine, incident=open_incident, houses_partially_damaged=4)
    add_report(engine, incident=review, reporter="b", barangay="Casile", hours=1)
    add_report(engine, reporter="c", hours=-48, status="held")  # outside the 24 h window
    add_report(engine, reporter="d", hours=2, status="held")
    retired = add_incident(engine, barangay="Loma")
    with Session(engine) as session:
        session.get(Incident, retired).active = False
        session.add_all(
            [
                Draft(kind="damaged_houses", version=1),
                Draft(kind="rdana", barangay="Malaban", version=1, status="approved"),
                Draft(kind="rdana", barangay="Casile", version=1),
                ModelCall(model="fake", latency_ms=0, cached=False, cost_estimate=1.25),
            ]
        )
        session.commit()
    fisherfolk_incident(engine)


def test_overview_numbers_equal_the_linked_pages(client, engine):
    seed(engine)
    home = client.get("/").text
    incidents = client.get("/incidents").text
    reports = client.get("/reports").text
    drafts = client.get("/drafts").text

    assert int(kpi(home, "incidents_open")) == incidents.count('<tr id="incident-') == 3
    assert int(kpi(home, "incidents_review")) == incidents.count(">needs officer</span>") == 1
    total = int(re.search(r"(\d+) most recent reports", reports).group(1))
    assert int(kpi(home, "reports_total")) == total == 6
    assert int(kpi(home, "reports_held")) == reports.count('class="status-held"') == 2
    assert int(kpi(home, "reports_recent")) == 5  # all but the report from 48 h before
    history = drafts.split('id="all-drafts"')[1]
    assert int(kpi(home, "drafts_open")) == history.count(">draft</span>") == 2
    spend = re.search(r"Model spend, last 24 h: (\$[\d.]+) of", reports).group(1)
    assert kpi(home, "model_spend") == spend == "$1.25"
    # The next deadline is the Notice of Loss clock /drafts shows for that incident.
    due = kpi(home, "next_deadline")
    assert due.startswith("due in ") and "<strong data-due=" in drafts and due in drafts


def test_overview_links_every_tile_to_its_page(client, engine):
    seed(engine)
    home = client.get("/").text
    for href in ('href="/incidents"', 'href="/reports"', 'href="/drafts#all-drafts"'):
        assert href in home
    assert re.search(r'href="/drafts#nol-fisheries-\d+"', home)
    assert 'id="live-kpis" data-live="reports incidents drafts spend"' in home
    assert '<a href="/" aria-current="page">Overview</a>' in home


def test_overview_lists_attention_and_latest_reports_without_reporter_ids(client, engine):
    seed(engine)
    home = client.get("/").text
    assert "Casile flood" in home and "needs officer" in home
    assert home.count('<li id="report-') == 6
    assert "pseudonym-" not in home


def test_overview_empty_state(client, clock):
    clock.now = AT
    home = client.get("/").text
    assert kpi(home, "incidents_open") == "0"
    assert kpi(home, "next_deadline") == "None"
    assert "No reports yet" in home and "No incident waits for review" in home


def test_overview_counts_overdue_deadlines_when_none_is_ahead(client, engine, clock):
    fisherfolk_incident(engine)
    clock.now = AT + timedelta(days=30)
    home = client.get("/").text
    assert kpi(home, "next_deadline").endswith("overdue")

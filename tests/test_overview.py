"""The overview page: every number equals the same number on the page it links to."""

import re
from collections import defaultdict
from datetime import timedelta
from statistics import median

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.dashboard import model_usage
from app.main import create_app
from app.models import Draft, Incident, ModelCall, utcnow
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


# --- "Meta models at work" strip ---------------------------------------------------------


def seed_calls(engine):
    now = utcnow()
    calls = [
        ("muse-spark-1.3", 900, False, 0.002, 1),
        ("muse-spark-1.3", 1500, False, 0.003, 2),
        ("muse-spark-1.3", 4100, False, 0.004, 3),
        ("muse-spark-1.3", 2600, False, 0.003, 4),  # even count: the median averages two
        ("muse-spark-1.3", 1, True, 0.0, 5),
        ("sam-3.1", 30_000, False, 0.0025, 6),
        ("sam-3.1", 2, True, 0.0, 7),
        ("sam-3.1", 3, True, 0.0, 8),
        ("muse-voice-transcribe-1.0", 4, True, 0.0, 9),  # only cache hits: no live median
        ("muse-spark-1.3", 99_000, False, 5.0, 60 * 25),  # outside the 24 h window
    ]
    with Session(engine) as session:
        for model, latency, cached, cost, minutes_ago in calls:
            session.add(
                ModelCall(
                    at=now - timedelta(minutes=minutes_ago),
                    model=model,
                    latency_ms=latency,
                    cached=cached,
                    cost_estimate=cost,
                )
            )
        session.commit()


def direct_aggregate(engine) -> dict[str, tuple[int, float, float, float | None]]:
    """Calls, cache-hit rate, cost and median live latency per model, straight from SQLite."""
    since = utcnow() - timedelta(days=1)
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT model, cached, cost_estimate, latency_ms FROM modelcall WHERE at > ?",
            (since.isoformat(sep=" "),),
        ).all()
    by_model = defaultdict(list)
    for row in rows:
        by_model[row[0]].append(row)
    return {
        model: (
            len(rs),
            sum(r[1] for r in rs) / len(rs),
            sum(r[2] for r in rs),
            median(r[3] for r in rs if not r[1]) if any(not r[1] for r in rs) else None,
        )
        for model, rs in by_model.items()
    }


def test_the_models_strip_equals_a_direct_sql_aggregate(client, engine):
    seed_calls(engine)
    expected = direct_aggregate(engine)

    with Session(engine) as session:
        usage = {u.model: u for u in model_usage(session)}

    models = {"muse-spark-1.3", "sam-3.1", "muse-voice-transcribe-1.0"}
    assert set(usage) == set(expected) == models
    for model, (calls, rate, cost, median_ms) in expected.items():
        u = usage[model]
        assert (u.calls, u.cache_rate, u.median_ms) == (calls, pytest.approx(rate), median_ms)
        assert u.cost == pytest.approx(cost)
    assert usage["muse-spark-1.3"].median_ms == 2050  # (1500 + 2600) / 2, cache hit excluded
    assert usage["sam-3.1"].family.name == "SAM 3.1"

    html = client.get("/").text
    start = html.index('id="live-models"')
    strip = html[start : html.index("</section>", start)]
    rows = re.findall(r'<tr data-key="model-([^"]+)">(.*?)</tr>', strip, re.S)
    assert [m for m, _ in rows] == ["muse-spark-1.3", "sam-3.1", "muse-voice-transcribe-1.0"]
    cells = {model: re.findall(r'class="num"[^>]*>(.*?)</td>', row, re.S) for model, row in rows}
    assert cells["muse-spark-1.3"] == ["5", "20%", "$0.0120", "2.0 s"]
    assert cells["sam-3.1"] == ["3", "67%", "$0.0025", "30.0 s"]
    assert cells["muse-voice-transcribe-1.0"][:3] == ["1", "100%", "$0"]
    assert "all from cache" in cells["muse-voice-transcribe-1.0"][3]
    assert 'id="live-models" data-live="spend"' in html


def test_the_models_strip_has_an_empty_state(client):
    assert "No model calls in the last 24 h." in client.get("/").text

"""Regression coverage for the slice 4 review. All reports and model clients are synthetic."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import DRAFT_TRIGGERS, init_db, make_engine
from app.drafts import CSV_BOM
from app.main import create_app
from app.models import Draft, DraftEvent, Report
from tests.test_rdana import AT, SummaryLLM, add_incident, add_report, cite_all, evidence


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, summary_llm=SummaryLLM(*[cite_all] * 20))) as client:
        yield client


def generate(client, barangay="Malaban", **fields):
    response = client.post("/drafts", data={"barangay": barangay, **fields}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def stored(client, draft_id):
    with Session(client.app.state.engine) as session:
        return session.get(Draft, draft_id)


def decision_events(client, draft_id):
    with Session(client.app.state.engine) as session:
        query = select(DraftEvent).where(DraftEvent.draft_id == draft_id).order_by(DraftEvent.id)
        return [e for e in session.exec(query).all() if e.action != "generated"]


def form_fields(draft):
    return {f["key"]: f for s in draft.content["sections"] for f in s["fields"]}


def triggers(path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
        return dict(rows.fetchall())


# --- Database triggers -------------------------------------------------------------------


def test_init_db_replaces_older_trigger_definitions(settings, engine):
    path = engine.url.database
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER draft_frozen")
        connection.execute(
            "CREATE TRIGGER draft_frozen BEFORE UPDATE ON draft "
            "BEGIN SELECT RAISE(ABORT, 'old definition'); END"
        )
        connection.execute(
            "CREATE TRIGGER draft_retired BEFORE INSERT ON draft "
            "BEGIN SELECT RAISE(ABORT, 'retired trigger'); END"
        )
    assert "old definition" in triggers(path)["draft_frozen"]

    init_db(engine)
    current = triggers(path)
    assert set(current) == set(DRAFT_TRIGGERS)  # the retired trigger is gone too
    assert "old definition" not in current["draft_frozen"]
    assert (
        current["draft_frozen"] == f"CREATE TRIGGER draft_frozen {DRAFT_TRIGGERS['draft_frozen']}"
    )
    # The new definition behaves: an open draft takes its decision, a decided one is frozen.
    with Session(engine) as session:
        session.add(Draft(kind="rdana", barangay="Malaban", version=1, created_at=AT))
        session.commit()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE draft SET status = 'approved' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            connection.execute("UPDATE draft SET decision_reason = 'edited' WHERE id = 1")


# --- Single-source share -----------------------------------------------------------------


def test_a_value_only_one_reporter_stated_is_single_source(engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, reporter="a", houses_partially_damaged=40)
    add_report(engine, incident=incident, reporter="b", hours=1, houses_partially_damaged=2)
    add_report(engine, incident=incident, reporter="c", hours=2, houses_partially_damaged=2)
    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 40 and count.single_source_value == 40

    add_report(engine, incident=incident, reporter="d", hours=3, houses_partially_damaged=45)
    add_report(engine, incident=incident, reporter="b", hours=4, houses_partially_damaged=50)
    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 50 and count.single_source_value == 50  # d's 45 is below 50

    add_report(engine, incident=incident, reporter="c", hours=5, houses_partially_damaged=50)
    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 50 and count.single_source_value == 0  # b and c both say 50


# --- Pending grouping review -------------------------------------------------------------


def three_reports_of_the_same_five_houses(engine, *, first_status="needs_officer"):
    """The pair judge failed twice, so one street's 5 houses sit in three incidents."""
    ids = []
    for hours, status in enumerate((first_status, "needs_officer", "needs_officer")):
        incident = add_incident(engine, status=status)
        ids.append(
            add_report(
                engine,
                incident=incident,
                reporter=str(hours),
                hours=hours,
                houses_partially_damaged=5,
            )
        )
    return ids


def test_pending_incidents_are_never_added_to_the_figure(engine):
    ids = three_reports_of_the_same_five_houses(engine)
    count = evidence(engine).count("houses_partially_damaged")
    assert (count.confirmed, count.value) == (False, 0)
    assert (count.pending_value, count.pending_report_ids) == (5, ids)


def test_a_confirmed_incident_keeps_its_figure_and_duplicates_wait_beside_it(engine):
    ids = three_reports_of_the_same_five_houses(engine, first_status="open")
    count = evidence(engine).count("houses_partially_damaged")
    assert (count.value, count.pending_value, count.pending_report_ids) == (5, 5, ids[1:])


def test_a_grouping_error_makes_its_incident_pending(engine):
    incident = add_incident(engine)
    report = add_report(engine, incident=incident, houses_totally_damaged=3)
    with Session(engine) as session:
        row = session.get(Report, report)
        row.grouping_error = "RuntimeError: judge down"
        session.add(row)
        session.commit()
    count = evidence(engine).count("houses_totally_damaged")
    assert (count.value, count.pending_value) == (0, 3)


def test_pending_figures_show_on_the_form_csv_and_banner_and_approval_needs_a_reason(client):
    engine = client.app.state.engine
    ids = three_reports_of_the_same_five_houses(engine)
    listed = ", ".join(map(str, ids))
    draft_id = generate(client)
    field = form_fields(stored(client, draft_id))["10.2"]
    assert field["value"] is None  # never 0 and never 15
    assert field["count"]["pending"] == 5 and field["count"]["pending_report_ids"] == ids
    assert f"Not added: pending grouping review: 5 (reports {listed})." in field["note"]
    page = client.get(f"/drafts/{draft_id}").text
    assert "PENDING GROUPING REVIEW." in page and f"Reports {listed} may duplicate" in page
    assert "pending grouping review: 5, not added" in page
    assert "15" not in page.split('id="f-10.2"')[1].split("</tr>")[0]

    lines = client.get("/damaged-houses.csv").text.removeprefix(CSV_BOM).split("\r\n")
    semicolons = ";".join(map(str, ids))
    assert lines[1].endswith(f",Malaban,,,,,5,{semicolons}")  # no Total: nothing confirmed
    assert lines[2].endswith(f",City total,,,,,5,{semicolons}")
    assert '<span class="chip warn">5</span>' in client.get("/damaged-houses").text

    refused = client.post(f"/drafts/{draft_id}/approve", data={"reason": ""})
    assert refused.status_code == 422 and "wait for grouping review" in refused.text
    assert stored(client, draft_id).status == "draft"
    response = client.post(
        f"/drafts/{draft_id}/approve",
        data={"reason": "Same street; counted once by the CDRRMO"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and stored(client, draft_id).status == "approved"
    (event,) = decision_events(client, draft_id)
    assert event.reason == "Same street; counted once by the CDRRMO"
    assert event.detail == {"pending_report_ids": ids}


def test_drafts_list_shows_the_pending_chip(client):
    three_reports_of_the_same_five_houses(client.app.state.engine)
    assert "3 reports with figures pending grouping review" in client.get("/drafts").text

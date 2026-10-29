"""Officer draft pages: generate, approve/reject, versions, audit, CSV and the database upgrade."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlmodel import Session, SQLModel, select

from app.db import init_db, make_engine
from app.drafts import CSV_BOM, CSV_HEADER, DraftService
from app.main import create_app
from app.models import Draft, DraftEvent, Report
from app.rdana import NOT_IN_REPORTS
from tests.test_rdana import AT, SummaryLLM, add_incident, add_report, cite_all

SUMMARY = cite_all


@pytest.fixture
def llm():
    return SummaryLLM(*[SUMMARY] * 20)


@pytest.fixture
def client(settings, llm):
    with TestClient(create_app(settings, summary_llm=llm)) as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def generate(client, barangay="Malaban", **fields):
    response = client.post("/drafts", data={"barangay": barangay, **fields}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def draft(engine, draft_id):
    with Session(engine) as session:
        return session.get(Draft, draft_id)


def events(engine, draft_id=None):
    with Session(engine) as session:
        query = select(DraftEvent).order_by(DraftEvent.id)
        if draft_id is not None:
            query = query.where(DraftEvent.draft_id == draft_id)
        return [(e.draft_id, e.action, e.reason, e.actor) for e in session.exec(query).all()]


def test_nav_links_to_drafts_and_damaged_houses(client):
    html = client.get("/reports").text
    assert '<a href="/drafts">Drafts</a>' in html
    assert '<a href="/damaged-houses">Damaged houses</a>' in html


def test_drafts_page_lists_barangays_and_unplaced_incidents(client, engine):
    assert "No barangay has assessed reports yet" in client.get("/drafts").text
    add_report(engine, incident=add_incident(engine), houses_partially_damaged=4)
    add_report(engine, hours=1, status="held")
    nowhere = add_incident(engine, barangay=None)
    add_report(engine, incident=nowhere, barangay=None)
    html = client.get("/drafts").text
    assert "<strong>Malaban</strong>" in html
    assert "1 report not assessed yet, not counted" in html
    assert f'<a href="/incidents/{nowhere}">incident #{nowhere}</a>' in html
    assert "Generate RDANA draft" in html


def test_generate_approve_and_the_approval_is_logged(client, engine):
    add_report(engine, incident=add_incident(engine), houses_partially_damaged=4)
    draft_id = generate(client, event_name="Typhoon demo")
    page = client.get(f"/drafts/{draft_id}").text
    assert "DRAFT, NOT APPROVED." in page and "Approve this version" in page
    response = client.post(
        f"/drafts/{draft_id}/approve",
        data={"decided_by": "  CDRRMO   Head ", "reason": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    stored = draft(engine, draft_id)
    assert stored.status == "approved" and stored.decided_by == "CDRRMO Head"
    assert stored.decided_at is not None and stored.decision_reason is None
    assert events(engine, draft_id) == [
        (draft_id, "generated", None, None),
        (draft_id, "approved", None, "CDRRMO Head"),
    ]
    page = client.get(f"/drafts/{draft_id}").text
    assert "APPROVED" in page and "by CDRRMO Head" in page
    assert "Approve this version" not in page  # no decision forms on a decided draft


def test_reject_needs_a_reason_and_only_a_draft_can_be_decided(client, engine):
    add_report(engine, hazards=["flood"])
    draft_id = generate(client)
    for reason in ("", "   ", "no"):
        response = client.post(f"/drafts/{draft_id}/reject", data={"reason": reason})
        assert response.status_code == 422
        assert "at least 5 characters" in response.text
    too_long = client.post(f"/drafts/{draft_id}/reject", data={"reason": "x" * 1001})
    assert too_long.status_code == 422
    assert draft(engine, draft_id).status == "draft"
    response = client.post(
        f"/drafts/{draft_id}/reject",
        data={"reason": "Counts not yet verified by the barangay"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert draft(engine, draft_id).status == "rejected"
    for action in ("approve", "reject"):
        again = client.post(f"/drafts/{draft_id}/{action}", data={"reason": "second decision"})
        assert again.status_code == 409
        assert "only a draft can be decided" in again.text
    assert client.post("/drafts/999/approve", data={}).status_code == 404
    assert client.post(f"/drafts/{draft_id}/publish", data={}).status_code == 404
    assert client.post(f"/drafts/{draft_id}/approve", json={}).status_code == 415
    assert [e[1] for e in events(engine, draft_id)] == ["generated", "rejected"]


def test_regenerating_keeps_approved_content_and_supersedes_the_open_draft(client, engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, houses_partially_damaged=4)
    v1 = generate(client)
    client.post(f"/drafts/{v1}/approve", data={})
    approved_content = draft(engine, v1).content
    approved_page = client.get(f"/drafts/{v1}").text

    add_report(engine, incident=incident, reporter="b", hours=2, houses_partially_damaged=6)
    assert "New evidence since this version" in client.get("/drafts").text
    v2 = generate(client)
    v3 = generate(client)

    assert (draft(engine, v1).version, draft(engine, v1).status) == (1, "approved")
    assert draft(engine, v1).content == approved_content

    def form(html):
        return html.split("<h1>RDANA FORM")[1].split("How the summary (1.4) was made")[0]

    assert form(client.get(f"/drafts/{v1}").text) == form(approved_page)
    assert (draft(engine, v2).version, draft(engine, v2).status) == (2, "superseded")
    assert (draft(engine, v3).version, draft(engine, v3).status) == (3, "draft")
    assert client.post(f"/drafts/{v2}/approve", data={}).status_code == 409
    assert (v2, "superseded", "Version 3 was generated.", None) in events(engine, v2)
    assert "SUPERSEDED" in client.get(f"/drafts/{v2}").text
    assert "New evidence since this version" not in client.get("/drafts").text
    fields = {f["key"]: f for s in draft(engine, v3).content["sections"] for f in s["fields"]}
    assert fields["10.2"]["value"] == "6"  # the new version counts the new report
    add_report(engine, barangay="Casile", hazards=["flood"])
    other = generate(client, barangay="Casile")
    assert draft(engine, other).version == 1  # versions are per barangay and output
    assert draft(engine, v3).status == "draft"


def test_frozen_rows_are_enforced_by_the_database(client, engine):
    add_report(engine, hazards=["flood"])
    draft_id = generate(client)
    client.post(f"/drafts/{draft_id}/approve", data={})
    path = client.app.state.engine.url.database
    with sqlite3.connect(path) as connection:
        for statement in (
            "UPDATE draft SET content = '{}' WHERE id = ?",
            "UPDATE draft SET status = 'draft' WHERE id = ?",
            "DELETE FROM draft WHERE id = ?",
            "UPDATE draftevent SET reason = 'edited' WHERE draft_id = ?",
            "DELETE FROM draftevent WHERE draft_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (draft_id,))


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"barangay": "Atlantis"}, "Choose one of Biñan"),
        ({"barangay": "Casile"}, "No assessed reports are placed in"),
        ({"barangay": "Malaban", "event_at": "yesterday"}, "must look like"),
        ({"barangay": "Malaban", "event_name": "x" * 161}, "at most 160"),
    ],
)
def test_generate_validates_input(client, engine, form, message):
    add_report(engine, hazards=["flood"])
    response = client.post("/drafts", data=form)
    assert response.status_code == 422 and message in response.text


def test_event_time_is_read_as_philippine_time(client, engine):
    add_report(engine, hazards=["flood"])
    draft_id = generate(client, event_at="2026-10-27T20:30", event_type="Typhoon")
    stored = draft(engine, draft_id)
    assert stored.event_at.isoformat() == "2026-10-27T12:30:00"
    assert "2026-10-27 20:30" in client.get(f"/drafts/{draft_id}").text


def seed_houses(engine):
    corrected = add_incident(engine)
    a = add_report(engine, incident=corrected, reporter="a", houses_partially_damaged=5)
    b = add_report(
        engine,
        incident=corrected,
        reporter="b",
        hours=1,
        houses_partially_damaged=3,
        is_correction=True,
    )
    c = add_report(
        engine,
        incident=add_incident(engine, "Casile"),
        barangay="Casile",
        houses_totally_damaged=2,
        houses_partially_damaged=1,
    )
    add_report(engine, barangay="Poblacion", hazards=["flood"])  # no house numbers: not listed
    return a, b, c


def test_damaged_houses_csv_has_the_exact_header_rows_and_city_total(client, engine):
    a, b, c = seed_houses(engine)
    response = client.get("/damaged-houses.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="binan-damaged-houses-'
    )
    assert response.content.startswith(CSV_BOM.encode())  # Excel reads UTF-8 "Biñan"
    lines = response.text.removeprefix(CSV_BOM).split("\r\n")
    assert lines[0] == (
        "Region,Province,City/Municipality,Barangay,Total,Totally,Partially,Evidence report ids,"
        "Pending grouping review,Pending report ids"
    )
    assert lines[0] == ",".join(CSV_HEADER)
    place = "Region IV-A (CALABARZON),Laguna,City of Biñan"
    assert lines[1:] == [
        f"{place},Casile,3,2,1,{c},,",
        f"{place},Malaban,3,,3,{a};{b},,",
        f"{place},City total,6,2,4,{a};{b};{c},,",
        "",
    ]
    html = client.get("/damaged-houses").text
    assert "City total" in html and "Poblacion" not in html
    assert "3 of 3 stated by only one reporter" in html  # Casile has one reporter


def test_damaged_houses_snapshot_is_a_versioned_draft_with_a_frozen_csv(client, engine):
    seed_houses(engine)
    response = client.post("/drafts/damaged-houses", follow_redirects=False)
    assert response.status_code == 303
    draft_id = int(response.headers["location"].rsplit("/", 1)[1])
    frozen = client.get(f"/drafts/{draft_id}.csv").text
    add_report(engine, barangay="San Antonio", houses_totally_damaged=9)
    assert client.get(f"/drafts/{draft_id}.csv").text == frozen
    assert "San Antonio" in client.get("/damaged-houses.csv").text
    client.post(f"/drafts/{draft_id}/approve", data={"decided_by": "CSWDO"})
    assert "APPROVED" in client.get(f"/drafts/{draft_id}").text
    rdana = generate(client)
    assert client.get(f"/drafts/{rdana}.csv").status_code == 404


def test_empty_damaged_houses_table_cannot_be_snapshotted(client):
    assert "No report states a number of damaged houses yet." in client.get("/damaged-houses").text
    assert client.post("/drafts/damaged-houses").status_code == 422
    lines = client.get("/damaged-houses.csv").text.removeprefix(CSV_BOM).split("\r\n")
    assert lines[1] == "Region IV-A (CALABARZON),Laguna,City of Biñan,City total,,,,,,"


async def test_service_numbers_versions_inside_one_write_transaction(settings):
    engine = make_engine(settings)
    init_db(engine)
    add_report(engine, hazards=["flood"])
    service = DraftService(settings, engine, SummaryLLM(*[SUMMARY] * 3))
    drafts = [await service.generate_rdana("Malaban") for _ in range(3)]
    assert [d.version for d in drafts] == [1, 2, 3]
    with Session(engine) as session:
        statuses = [d.status for d in session.exec(select(Draft).order_by(Draft.id)).all()]
    assert statuses == ["superseded", "superseded", "draft"]
    engine.dispose()


async def test_init_db_upgrades_a_database_from_before_drafts(settings):
    """A file made by the previous main (no draft tables) opens unchanged and gains them."""
    settings.data_dir.mkdir(parents=True)
    old = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(
        old,
        tables=[
            t for t in SQLModel.metadata.sorted_tables if t.name not in ("draft", "draftevent")
        ],
    )
    report = add_report(old, hazards=["flood"])
    old.dispose()

    engine = make_engine(settings)
    init_db(engine)  # idempotent: tables, columns and triggers are created once
    init_db(engine)
    names = set(inspect(engine).get_table_names())
    assert {"draft", "draftevent", "report", "incident"} <= names
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    assert triggers == {
        "draft_frozen",
        "draft_no_delete",
        "draftevent_no_update",
        "draftevent_no_delete",
    }
    with Session(engine) as session:
        assert session.get(Report, report).received_at == AT
    service = DraftService(settings, engine, SummaryLLM(SUMMARY))
    assert (await service.generate_rdana("Malaban")).version == 1
    engine.dispose()


async def test_unfilled_fields_render_the_marker_and_filled_ones_link_evidence(settings):
    engine = make_engine(settings)
    init_db(engine)
    incident = add_incident(engine)
    report = add_report(engine, incident=incident, houses_partially_damaged=4)
    draft = await DraftService(settings, engine, SummaryLLM(cite_all)).generate_rdana("Malaban")
    engine.dispose()
    with TestClient(create_app(settings, summary_llm=SummaryLLM())) as client:
        html = client.get(f"/drafts/{draft.id}").text
    fields = {f["key"]: f for s in draft.content["sections"] for f in s["fields"]}
    empty = [k for k, f in fields.items() if f["value"] is None and not f["heading"]]
    assert html.count(NOT_IN_REPORTS) == len(empty) + 1  # +1 for the legend
    row = html.split('id="f-10.2"')[1].split("</tr>")[0]
    assert "<strong>4</strong>" in row
    assert f'href="/incidents/{incident}#report-{report}"' in row
    assert "1 reporter</span>" in row and "single source" in row
    missing = html.split('id="f-2.1"')[1].split("</tr>")[0]
    assert NOT_IN_REPORTS in missing
    assert '15.5</strong> <span class="muted">(sic)</span>' in html
    assert "Counting rule: reports in one incident" in html  # the footnote on the draft

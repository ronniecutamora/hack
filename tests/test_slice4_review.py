"""Regression coverage for the slice 4 review. All reports and model clients are synthetic."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import DRAFT_TRIGGERS, init_db, make_engine
from app.drafts import CSV_BOM, DraftService
from app.main import create_app
from app.models import Draft, DraftEvent, Report
from app.rdana import summary_counts
from app.rdana_summary import fallback_summary, summary_input, validate_summary
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


# --- Summary validator: an allowlist of computed counts, read next to their words ----------


@pytest.fixture
def corrected(engine):
    """Reports of 3, then 5, then a correction to 4 houses; 3 families affected elsewhere."""
    incident = add_incident(engine)
    add_report(engine, incident=incident, reporter="a", houses_partially_damaged=3)
    add_report(engine, incident=incident, reporter="b", hours=1, houses_partially_damaged=5)
    add_report(
        engine,
        incident=incident,
        reporter="a",
        hours=2,
        houses_partially_damaged=4,
        is_correction=True,
        place_detail="Purok 3",
    )
    add_report(engine, incident=add_incident(engine), reporter="c", hours=3, families_affected=3)
    found = evidence(engine)
    return summary_input(found, summary_counts(found))


def check(payload, text):
    cited = [r["report_id"] for i in payload["incidents"] for r in i["reports"]]
    return validate_summary({"summary": text, "cited_report_ids": cited}, payload)


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("4 houses partially damaged were reported.", True),
        ("Reported so far: houses partially damaged: 4; families affected: 3.", True),
        ("5 houses partially damaged were reported.", False),  # superseded by the correction
        ("3 houses partially damaged were reported.", False),  # superseded too
        ("3 families affected and 3 dead were reported.", False),  # 3 is a families count
        ("4 reports from 2 incidents, by 3 reporters, describe flooding.", True),
        ("Flooding was reported in Purok 3.", True),  # quoted exactly from the reports
        ("Flooding was reported in Purok 5.", False),  # a place number the reports never give
    ],
)
def test_numbers_are_the_computed_counts_they_describe(corrected, text, accepted):
    assert (check(corrected, text) == []) is accepted, check(corrected, text)


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("One person is reported missing.", False),
        ("Hundreds of families were displaced.", False),
        ("Dozens of homes are flooded.", False),
        ("twenty-five houses partially damaged.", False),
        ("No one was hurt.", True),
        ("One of the houses was reported partially damaged.", True),
        ("Four houses partially damaged.", True),
    ],
)
def test_spelled_numbers_are_read_whole_and_checked(corrected, text, accepted):
    assert (check(corrected, text) == []) is accepted, check(corrected, text)


def test_twenty_five_is_25_and_passes_only_when_25_is_a_count(corrected):
    allowed = corrected | {"computed_counts": {**corrected["computed_counts"], "missing": 25}}
    assert check(allowed, "twenty-five persons are reported missing.") == []
    assert check(corrected, "twenty-five persons are reported missing.") != []
    assert check(allowed, "twenty five persons are reported missing.") == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Flooding since 7:30.", "a time"),
        ("Flooding since 7 pm.", "a time"),
        ("Flooding on the 28th.", "an ordinal"),
        ("Flooding on the twenty-eighth.", "an ordinal"),
        ("Flooding since October 4.", "a date"),
        ("Flooding since 10/28.", "a date"),
        ("Flooding, see report 12.", "a report or incident reference"),
        ("Flooding, see #12.", "a report or incident reference"),
    ],
)
def test_dates_times_ordinals_and_references_are_rejected(corrected, text, kind):
    errors = check(corrected, text)
    assert any(e.startswith(f"The summary writes {kind} (") for e in errors), errors


def test_pending_figures_are_not_allowed_in_the_summary(engine):
    three_reports_of_the_same_five_houses(engine)
    found = evidence(engine)
    payload = summary_input(found, summary_counts(found))
    assert "houses_partially_damaged" not in payload["computed_counts"]
    assert {i.get("grouping") for i in payload["incidents"]} == {"waiting for officer review"}
    assert check(payload, "5 houses partially damaged were reported.") != []
    assert check(payload, "15 houses partially damaged were reported.") != []
    assert check(payload, "3 reports describe flooding.") == []


async def test_a_stale_report_figure_falls_back_to_the_rule_summary(engine, settings):
    incident = add_incident(engine)
    first = add_report(engine, incident=incident, reporter="a", houses_partially_damaged=5)
    add_report(
        engine,
        incident=incident,
        reporter="b",
        hours=1,
        houses_partially_damaged=4,
        is_correction=True,
    )
    stale = {"summary": "5 houses were reported partially damaged.", "cited_report_ids": [first]}
    llm = SummaryLLM(stale, stale)
    draft = await DraftService(settings, engine, llm).generate_rdana("Malaban")
    assert draft.summary_fallback is True
    assert "5 (computed: houses partially damaged 4)" in draft.summary_validation[0]["errors"][0]
    assert "houses partially damaged: 4" in form_fields(draft)["1.4"]["value"]


def test_the_rule_summary_passes_the_allowlist_with_every_count(engine):
    incident = add_incident(engine)
    add_report(
        engine,
        incident=incident,
        reporter="a",
        hazards=["flood"],
        flood_depth="waist",
        place_detail="Block 5 Lot 12, Phase 2 malapit sa bahay ni Aling Nena",
        families_affected=12,
        families_evacuated=5,
        persons_evacuated=21,
        missing=1,
        injured=2,
        dead=0,
        houses_totally_damaged=1,
        houses_partially_damaged=4,
        fish_cages_or_pens_damaged=3,
        needs=["rescue", "food"],
    )
    add_report(engine, incident=add_incident(engine), reporter="b", hours=1, hazards=["flood"])
    found = evidence(engine)
    counts = summary_counts(found)
    text = fallback_summary(found, counts)
    assert "Block 5 Lot 12" in text and "persons in evacuation centers: 21" in text
    assert check(summary_input(found, counts), text) == []

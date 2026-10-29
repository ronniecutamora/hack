"""Regression coverage for the slice 4 review. All reports and model clients are synthetic."""

import sqlite3
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import DRAFT_TRIGGERS, init_db, make_engine
from app.drafts import CSV_BOM, DraftService, damaged_houses_csv
from app.main import REPORT_LIMIT, create_app
from app.models import Draft, DraftEvent, Report, Reporter, manila_time, utcnow
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


def start_together(engine, barrier, errors):
    barrier.wait()
    try:
        init_db(engine)
    except Exception as exc:  # collected: a thread's exception would be lost
        errors.append(exc)


def test_parallel_init_db_calls_on_one_file_both_succeed(settings):
    """The bot and the web app start together: neither may see the other's half-done rebuild."""
    first = make_engine(settings)
    init_db(first)  # an existing database with triggers, like var/demo/app.db
    first.dispose()
    for _round in range(10):
        engines = [make_engine(settings) for _ in range(4)]
        barrier, errors = threading.Barrier(len(engines)), []
        threads = [
            threading.Thread(target=start_together, args=(e, barrier, errors)) for e in engines
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for engine in engines:
            engine.dispose()
        assert errors == []
        assert set(triggers(settings.db_path)) == set(DRAFT_TRIGGERS)


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
    assert lines[1].endswith(f",Malaban,,,,,,,5,{semicolons}")  # no Total: nothing confirmed
    assert lines[2].endswith(f",City total,,,,,,,5,{semicolons}")
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
    assert event.detail == {"stale": False, "pending_report_ids": ids}


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
        ("One of the houses was reported partially damaged.", False),  # claims 1, count is 4
        ("One of them is flooded.", True),  # no count word follows: an idiom
        ("Four houses partially damaged.", True),
    ],
)
def test_spelled_numbers_are_read_whole_and_checked(corrected, text, accepted):
    assert (check(corrected, text) == []) is accepted, check(corrected, text)


CASUALTY_PAYLOAD = {
    "computed_counts": {"reports": 3, "incidents": 2, "reporters": 2, "injured": 2, "dead": 0},
    "incidents": [{"place": None, "reports": [{"report_id": 1}]}],
}


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("2 residents drowned.", False),  # people with no casualty qualifier
        ("2 people were trapped.", False),
        ("2 residents reported flooding.", False),  # people are never the reporter count
        ("One of the residents died.", False),  # "one of" with a count word is a claim
        ("Water rose 2 meters.", False),  # no label at all
        ("2 persons were reported injured.", True),
        ("3 reports from 2 incidents, by 2 reporters.", True),
        ("Injured: 2; dead: 0.", True),
    ],
)
def test_every_number_needs_a_label_naming_exactly_one_count(text, accepted):
    assert (check(CASUALTY_PAYLOAD, text) == []) is accepted, check(CASUALTY_PAYLOAD, text)


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


# --- Staleness at decision time ----------------------------------------------------------


def test_approving_a_stale_draft_needs_confirmation_and_is_recorded(client):
    engine = client.app.state.engine
    incident = add_incident(engine)
    add_report(engine, incident=incident, houses_partially_damaged=4)
    draft_id = generate(client, event_name="Typhoon demo", event_at="2026-10-28T08:00")
    assert "NEW EVIDENCE SINCE THIS VERSION" not in client.get(f"/drafts/{draft_id}").text

    add_report(engine, incident=incident, reporter="b", hours=1, houses_partially_damaged=6)
    page = client.get(f"/drafts/{draft_id}").text
    assert "NEW EVIDENCE SINCE THIS VERSION" in page and 'name="confirm_stale"' in page
    # The regenerate button keeps the officer's event details.
    assert '<input type="hidden" name="event_name" value="Typhoon demo">' in page
    assert '<input type="hidden" name="event_at" value="2026-10-28T08:00">' in page

    refused = client.post(f"/drafts/{draft_id}/approve", data={"decided_by": "CDRRMO"})
    assert refused.status_code == 409
    assert "The evidence changed after version 1 was generated" in refused.text
    assert stored(client, draft_id).status == "draft" and decision_events(client, draft_id) == []

    response = client.post(
        f"/drafts/{draft_id}/approve",
        data={"decided_by": "CDRRMO", "confirm_stale": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and stored(client, draft_id).status == "approved"
    (event,) = decision_events(client, draft_id)
    assert event.detail == {"stale": True}
    assert "stays as approved" in client.get(f"/drafts/{draft_id}").text


def test_a_current_draft_is_approved_without_confirmation_and_rejection_never_needs_it(client):
    engine = client.app.state.engine
    incident = add_incident(engine)
    add_report(engine, incident=incident, houses_partially_damaged=4)
    current = generate(client)
    assert client.post(f"/drafts/{current}/approve", data={}).status_code == 200
    assert decision_events(client, current)[0].detail == {"stale": False}

    houses = client.post("/drafts/damaged-houses", follow_redirects=False)
    houses_id = int(houses.headers["location"].rsplit("/", 1)[1])
    add_report(engine, incident=incident, reporter="b", hours=1, houses_partially_damaged=6)
    assert client.post(f"/drafts/{houses_id}/approve", data={}).status_code == 409
    rejected = client.post(f"/drafts/{houses_id}/reject", data={"reason": "Counts changed"})
    assert rejected.status_code == 200 and stored(client, houses_id).status == "rejected"
    assert decision_events(client, houses_id)[0].detail == {"stale": True}


@pytest.mark.parametrize("kind", ["rdana", "damaged_houses"])
def test_a_draft_made_before_the_pending_bucket_counts_as_stale(client, kind):
    engine = client.app.state.engine
    add_report(engine, incident=add_incident(engine), houses_partially_damaged=4)
    if kind == "rdana":
        current = generate(client)
    else:
        made = client.post("/drafts/damaged-houses", follow_redirects=False)
        current = int(made.headers["location"].rsplit("/", 1)[1])
    model = stored(client, current)
    content = dict(model.content)
    if kind == "rdana":
        del content["pending_report_ids"]
    else:
        content["total"] = {k: v for k, v in content["total"].items() if k != "pending_report_ids"}
    with Session(engine) as session:  # the same evidence, as the pre-fix code stored it
        old = Draft(
            kind=model.kind,
            barangay=model.barangay,
            version=model.version + 1,
            content=content,
            evidence_fingerprint=model.evidence_fingerprint,
            created_at=model.created_at,
        )
        session.add(old)
        session.commit()
        old_id = old.id
    assert "NEW EVIDENCE SINCE THIS VERSION" not in client.get(f"/drafts/{current}").text
    assert "NEW EVIDENCE SINCE THIS VERSION" in client.get(f"/drafts/{old_id}").text
    assert "before figures pending grouping review" in client.get(f"/drafts/{old_id}").text
    refused = client.post(f"/drafts/{old_id}/approve", data={})
    assert refused.status_code == 409 and "before figures pending grouping review" in refused.text
    confirmed = client.post(f"/drafts/{old_id}/approve", data={"confirm_stale": "1"})
    assert confirmed.status_code == 200 and stored(client, old_id).status == "approved"
    assert decision_events(client, old_id)[0].detail == {"stale": True}


# --- Same-origin check -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Origin": "http://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://testserver.evil.example"}, 403),
        ({"Referer": "http://evil.example/drafts"}, 403),
        ({"Origin": "http://testserver"}, 303),
        ({"Referer": "http://testserver/drafts/1"}, 303),
        ({}, 303),
    ],
)
def test_state_changing_posts_must_be_same_origin(client, headers, status):
    engine = client.app.state.engine
    add_report(engine, incident=add_incident(engine), houses_partially_damaged=4)
    draft_id = generate(client)
    response = client.post(
        f"/drafts/{draft_id}/approve", data={}, headers=headers, follow_redirects=False
    )
    assert response.status_code == status
    assert stored(client, draft_id).status == ("draft" if status == 403 else "approved")
    if status == 403:
        assert "came from another site" in response.text
    generated = client.post(
        "/drafts", data={"barangay": "Malaban"}, headers=headers, follow_redirects=False
    )
    assert generated.status_code == status


# --- CSV status and columns --------------------------------------------------------------


def csv_rows(text):
    return [line.split(",") for line in text.removeprefix(CSV_BOM).split("\r\n") if line]


def test_every_csv_row_says_live_or_which_snapshot_it_is(client):
    engine = client.app.state.engine
    incident = add_incident(engine)
    add_report(engine, incident=incident, reporter="a", houses_totally_damaged=2)
    add_report(engine, incident=incident, reporter="b", hours=1, houses_totally_damaged=2)
    live = csv_rows(client.get("/damaged-houses.csv").text)
    assert {row[0] for row in live[1:]} == {"LIVE (NOT APPROVED)"}
    assert live[1][9:11] == ["0", "0"]  # two reporters said 2: corroborated, unflagged
    response = client.get("/damaged-houses.csv")
    assert "live-not-approved" in response.headers["content-disposition"]

    houses = client.post("/drafts/damaged-houses", follow_redirects=False)
    draft_id = int(houses.headers["location"].rsplit("/", 1)[1])
    assert csv_rows(client.get(f"/drafts/{draft_id}.csv").text)[1][0] == "DRAFT v1 (NOT APPROVED)"
    client.post(f"/drafts/{draft_id}/approve", data={})
    snapshot = csv_rows(client.get(f"/drafts/{draft_id}.csv").text)
    assert snapshot[1][0] == "APPROVED v1" and snapshot[1][1:] == live[1][1:]


def test_snapshots_made_before_the_new_columns_still_render(client):
    old = {
        "region": "R",
        "province": "P",
        "city": "C",
        "rows": [
            {
                "barangay": "Malaban",
                "total": 3,
                "totally": None,
                "partially": 3,
                "single_source": 3,
                "flagged": 0,
                "report_ids": [1, 2],
            }
        ],
        "total": {
            "total": 3,
            "totally": None,
            "partially": 3,
            "single_source": 3,
            "flagged": 0,
            "report_ids": [1, 2],
        },
    }
    rows = csv_rows(damaged_houses_csv(old, "APPROVED v1"))
    assert rows[1] == [
        "APPROVED v1",
        "R",
        "P",
        "C",
        "Malaban",
        "3",
        "",
        "3",
        "1;2",
        "3",
        "0",
        "",
        "",
    ]


# --- Event date and time -----------------------------------------------------------------


def local(value):
    return manila_time(value).replace(" ", "T")


@pytest.mark.parametrize(
    ("event_at", "message"),
    [
        (lambda: local(utcnow() + timedelta(hours=2)), "is in the future"),
        (lambda: local(AT - timedelta(days=31)), "more than 30 days before the earliest report"),
    ],
)
def test_impossible_event_times_are_rejected(client, event_at, message):
    add_report(client.app.state.engine, hazards=["flood"])
    response = client.post("/drafts", data={"barangay": "Malaban", "event_at": event_at()})
    assert response.status_code == 422 and message in response.text
    with Session(client.app.state.engine) as session:
        assert session.exec(select(Draft)).all() == []


def test_an_event_time_after_the_first_report_is_kept_with_a_warning(client):
    add_report(client.app.state.engine, hazards=["flood"])
    draft_id = generate(client, event_at=local(AT + timedelta(hours=3)))
    field = form_fields(stored(client, draft_id))["1.1.3"]
    assert field["value"] == manila_time(AT + timedelta(hours=3))
    assert field["warning"] == "Later than the earliest report: check the date."
    assert "Later than the earliest report" in client.get(f"/drafts/{draft_id}").text
    earlier = generate(client, event_at=local(AT - timedelta(days=29)))
    assert "warning" not in form_fields(stored(client, earlier))["1.1.3"]


# --- Evidence links beyond the /reports page ---------------------------------------------


def test_evidence_links_reach_reports_older_than_the_reports_page(client):
    engine = client.app.state.engine
    old = add_report(engine, hours=-1, houses_partially_damaged=2)  # ungrouped: pending
    with Session(engine) as session:
        reporter = Reporter(pseudonym="bulk")
        session.add(reporter)
        session.flush()
        for minute in range(REPORT_LIMIT + 5):
            session.add(
                Report(
                    reporter_id=reporter.id,
                    channel="demo",
                    received_at=AT + timedelta(minutes=minute),
                    status="new",
                )
            )
        session.commit()
    assert f'id="report-{old}"' not in client.get("/reports").text

    draft_id = generate(client)
    link = f'href="/reports?around={old}#report-{old}"'
    assert link in client.get(f"/drafts/{draft_id}").text
    assert f'href="/reports#report-{old}"' not in client.get(f"/drafts/{draft_id}").text
    page = client.get(f"/reports?around={old}").text
    assert f'id="report-{old}"' in page and "Showing the reports around report" in page
    assert page.count('<tr id="report-') <= REPORT_LIMIT

    newest = client.get("/reports").text.split('<tr id="report-')[1].split('"')[0]
    middle = client.get(f"/reports?around={newest}").text
    assert f'id="report-{newest}"' in middle  # the newest report: the window is all older ones
    assert client.get("/reports?around=999999").status_code == 404

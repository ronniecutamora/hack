"""PCIC Notice of Loss drafts: fields, redaction, annex, countdown, approval and a new commodity."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import init_db, make_engine
from app.deadlines import DEADLINES, Deadline
from app.main import create_app
from app.models import Draft, DraftEvent, Media, PhotoAssessment, utcnow
from app.nol import SAM_LABEL
from app.nol_form import ASSURED, COMMODITIES, FISHERIES, NolField, check
from tests.test_rdana import AT, SummaryLLM, add_incident, add_report, cite_all

PENS = "fish_pens_or_cages"
NAMED = "Ako po si Pedro Santos, mangingisda sa Purok 1. Sira ang fish pen ni Mang Kanor Reyes."
ASSURED_FIELDS = ("1", "2", "7", "8", "10", "11", "12")


class Clock:
    """The app's injected clock (naive UTC); tests move it."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(AT + timedelta(hours=5))


@pytest.fixture
def client(settings, clock):
    app = create_app(settings, summary_llm=SummaryLLM(*[cite_all] * 5), clock=clock)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def add_photo(engine, report_id, *, signals=None, purged=False):
    """A photo, its SAM overlay and the photo assessment, like the assessment service saves."""
    with Session(engine) as session:
        photo = Media(report_id=report_id, kind="photo", path="media/p.jpg", sha256="a" * 64)
        overlay = Media(report_id=report_id, kind="overlay", path="media/o.jpg", sha256="b" * 64)
        if purged:
            photo.purged_at = overlay.purged_at = utcnow()
        session.add(photo)
        session.add(overlay)
        session.flush()
        session.add(
            PhotoAssessment(
                report_id=report_id,
                media_id=photo.id,
                overlay_media_id=overlay.id,
                llm_model="fake-vision",
                judgment={"roof_damage": "unknown", "debris": True, "notes": "Broken bamboo."},
                measurement={
                    "roof_count": 0,
                    "floodwater_present": True,
                    "floodwater_fraction": 0.4,
                },
            )
        )
        session.commit()
        return photo.id, overlay.id


def fisherfolk_incident(engine, **extra):
    incident = add_incident(engine, barangay="De La Paz")
    first = add_report(
        engine,
        incident=incident,
        barangay="De La Paz",
        text=NAMED,
        hazards=["strong_wind", "flood"],
        livelihood_damage=[PENS, "fishing_gear"],
        place_detail="Purok 1 tabing-lawa, fish pen area ni Mang Kanor",
        **extra,
    )
    second = add_report(
        engine,
        incident=incident,
        reporter="b",
        barangay="De La Paz",
        hours=240,
        text="Follow-up po: hindi pa naaayos, kailangan ng lambat at kawayan.",
        livelihood_damage=[PENS],
        needs=["livelihood_materials"],
        fish_cages_or_pens_damaged=3,
    )
    return incident, first, second


def generate(client, incident_id, commodity="fisheries", **headers):
    response = client.post(
        "/drafts/notice-of-loss",
        data={"commodity": commodity, "incident_id": str(incident_id)},
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def stored(engine, draft_id):
    with Session(engine) as session:
        return session.get(Draft, draft_id)


def fields(draft):
    return {f["number"]: f for f in draft.content["fields"]}


# --- Fields, redaction and the annex ------------------------------------------------------


def test_evidence_fields_are_filled_and_identity_fields_are_left_for_the_assured(client, engine):
    incident, first, second = fisherfolk_incident(engine)
    add_photo(engine, first)
    draft = stored(engine, generate(client, incident))
    f = fields(draft)
    assert draft.kind == "pcic_nl_fisheries" and draft.incident_id == incident
    assert [x["number"] for x in draft.content["fields"]] == [str(n) for n in range(1, 13)]
    assert f["3"]["value"].startswith("Barangay De La Paz, City of Biñan, Laguna; Purok 1")
    assert "GPS 14.35110, 121.09000" in f["3"]["value"]
    assert f["4"]["value"] == "Flood, Strong wind"
    assert f["5"]["value"] == "2026-10-28 12:00 (first reported)"
    assert "may be earlier" in f["5"]["note"]
    assert f["6"]["value"] is None and "never estimated" in f["6"]["note"]
    assert f["9"]["value"].startswith(
        "Reported damage to fish pens or cages, fishing gear; "
        "fish cages or pens damaged: 3 (reported count); photos from 1 report"
    )
    assert SAM_LABEL in f["9"]["value"]
    assert "90% and above" in f["9"]["note"] and "does not classify" in f["9"]["note"]
    for number in ("3", "4", "5", "9"):
        assert f[number]["mode"] == "evidence" and f[number]["evidence"], number
    assert f["9"]["evidence"][0]["report_ids"] == [first, second]
    for number in ASSURED_FIELDS:
        assert f[number]["mode"] == "assured" and f[number]["value"] is None, number
    assert draft.content["office"]["email"] == "ro4@pcic.gov.ph"
    assert draft.content["missing_evidence"] == []

    page = client.get(f"/drafts/{draft.id}").text
    assert page.count(ASSURED) >= len(ASSURED_FIELDS) + 1  # plus the letter's date
    assert "Regional Office No. IV" in page and "Calamba City" in page
    assert "Left for the assured: type of policy, insurance policy number." in page
    assert f"SAM 3.1 measured ({SAM_LABEL}): 0 roofs, floodwater over 40% of the photo" in page
    assert "Agent: Broken bamboo." in page


def test_no_reporter_name_reaches_the_draft_or_its_page(client, engine):
    incident, first, _ = fisherfolk_incident(engine)
    draft_id = generate(client, incident)
    stored_json = json.dumps(stored(engine, draft_id).content, ensure_ascii=False)
    page = client.get(f"/drafts/{draft_id}").text
    for name in ("Pedro", "Santos", "Kanor", "Reyes"):
        assert name not in stored_json and name not in page, name
    (entry, _second) = stored(engine, draft_id).content["annex"]
    assert entry["excerpts"][0] == {
        "source": "message",
        "text": "Ako po si [pangalan], mangingisda sa Purok 1. Sira ang fish pen [redacted]",
    }


def test_phone_numbers_and_names_in_photo_notes_are_removed(client, engine):
    from app.nol import excerpt

    assert excerpt("Tawagan po ako sa 0917 123 4567 o +63-917-765-4321.") == (
        "Tawagan po ako sa [number] o [number]."
    )
    assert excerpt("Purok 1, 3 fish pens, 2026") == "Purok 1, 3 fish pens, 2026"
    incident, first, _ = fisherfolk_incident(engine)
    add_photo(engine, first)
    with Session(engine) as session:
        row = session.exec(select(PhotoAssessment)).one()
        row.judgment = {**row.judgment, "notes": "Pen of Mang Kanor, call 09171234567."}
        session.add(row)
        session.commit()
    notes = stored(engine, generate(client, incident)).content["annex"][0]["photos"][0]
    assert notes["judgment"]["notes"] == "Pen of [redacted], call [number]."


def test_a_long_excerpt_is_cut_after_redaction(client, engine):
    incident, *_ = fisherfolk_incident(engine)
    add_report(
        engine,
        incident=incident,
        reporter="c",
        hours=1,
        barangay="De La Paz",
        text="Sira po ang baklad. " * 30,
        livelihood_damage=[PENS],
    )
    annex = stored(engine, generate(client, incident)).content["annex"]
    text = annex[1]["excerpts"][0]["text"]
    assert len(text) <= 280 and text.endswith("…")


def test_only_incidents_with_fisheries_damage_are_offered(client, engine):
    flood = add_incident(engine)
    add_report(engine, incident=flood, hazards=["flood"], houses_partially_damaged=2)
    counted = add_incident(engine, barangay="De La Paz")
    add_report(
        engine, incident=counted, reporter="b", barangay="De La Paz", fish_cages_or_pens_damaged=2
    )
    html = client.get("/drafts").text
    assert f'id="nol-fisheries-{counted}"' in html and f'id="nol-fisheries-{flood}"' not in html
    refused = client.post(
        "/drafts/notice-of-loss", data={"commodity": "fisheries", "incident_id": str(flood)}
    )
    assert refused.status_code == 422 and "no assessed report of fisheries damage" in refused.text
    unknown = client.post("/drafts/notice-of-loss", data={"commodity": "rice", "incident_id": "1"})
    assert unknown.status_code == 422 and "Unknown insurance commodity" in unknown.text
    bad = client.post("/drafts/notice-of-loss", data={"commodity": "fisheries", "incident_id": "x"})
    assert bad.status_code == 422


def test_missing_photo_and_deleted_photos_are_said_plainly(client, engine):
    incident, first, _ = fisherfolk_incident(engine)
    no_photo = stored(engine, generate(client, incident))
    assert no_photo.content["missing_evidence"]
    assert "MISSING EVIDENCE" in client.get(f"/drafts/{no_photo.id}").text
    add_photo(engine, first, purged=True)
    page = client.get(f"/drafts/{generate(client, incident)}").text
    assert "MISSING EVIDENCE" not in page
    assert "photo deleted (retention)" in page and "overlay deleted" in page


# --- Deadline countdown -------------------------------------------------------------------


def test_the_countdown_shows_on_drafts_the_draft_and_the_incident(client, engine, clock):
    incident, *_ = fisherfolk_incident(engine)
    expected = "due in 43h 0m"  # first report at AT, the clock at AT + 5 h, 2 calendar days
    assert expected in client.get("/drafts").text
    assert expected in client.get(f"/incidents/{incident}").text
    draft_id = generate(client, incident)
    page = client.get(f"/drafts/{draft_id}").text
    assert expected in page and 'data-due="2026-10-30T04:00:00Z"' in page
    assert "due in 163h 0m" in page  # the claim for indemnity, 7 calendar days
    assert "official FB Messenger" in page and "Citizen&#39;s Charter 2026" in page
    assert "⚠️ Not confirmed" in page

    assert client.post(f"/drafts/{draft_id}/approve", data={}).status_code == 200
    clock.now = AT + timedelta(days=12, hours=1)
    page = client.get(f"/drafts/{draft_id}").text
    assert "APPROVED FOR FILING, NOT FILED" in page and "Ulat never sends anything" in page
    assert "overdue by 10 days" in page  # approval does not stop the clock
    assert "approved for filing, not filed" in client.get("/drafts").text
    assert "overdue by 10 days" in client.get(f"/incidents/{incident}").text


# --- The reused photo ---------------------------------------------------------------------


def test_a_reused_photo_puts_a_red_banner_on_the_notice_of_loss(client, engine):
    _, original, _ = fisherfolk_incident(engine)
    copy_incident = add_incident(engine, barangay="San Antonio")
    signal = {
        "code": "photo_reused",
        "severity": "high",
        "detail": f"Photo 9: identical bytes in report #{original}; another reporter.",
        "related_report_id": original,
    }
    copy = add_report(
        engine,
        incident=copy_incident,
        reporter="z",
        barangay="San Antonio",
        hours=300,
        livelihood_damage=[PENS],
        signals=[signal],
    )
    add_photo(engine, copy)
    draft = stored(engine, generate(client, copy_incident))
    assert draft.content["reused_photos"] == [{"report_id": copy, "related_report_id": original}]
    page = client.get(f"/drafts/{draft.id}").text
    assert '<div class="banner alert keep"><strong>PHOTO REUSED.</strong>' in page
    assert f'matches report <a href="/reports?around={original}#report-{original}">' in page
    assert '<li class="chip alert">' in page and "photo reused: Photo 9" in page
    assert "SINGLE SOURCE" in page
    assert "flagged: report" in client.get("/drafts").text


# --- Approval machinery, shared with the RDANA drafts --------------------------------------


def test_versions_staleness_approval_and_rejection_reuse_the_draft_machinery(client, engine):
    incident, *_ = fisherfolk_incident(engine)
    other, *_ = fisherfolk_incident(engine)
    first = generate(client, incident)
    second = generate(client, incident)
    elsewhere = generate(client, other)
    assert stored(engine, first).status == "superseded"
    assert (stored(engine, second).version, stored(engine, elsewhere).version) == (2, 1)

    add_report(
        engine,
        incident=incident,
        reporter="c",
        hours=2,
        barangay="De La Paz",
        livelihood_damage=[PENS],
    )
    page = client.get(f"/drafts/{second}").text
    assert "NEW EVIDENCE SINCE THIS VERSION" in page
    assert '<form class="no-print" method="post" action="/drafts/notice-of-loss">' in page
    assert f'<input type="hidden" name="incident_id" value="{incident}">' in page
    refused = client.post(f"/drafts/{second}/approve", data={})
    assert refused.status_code == 409 and "The evidence changed" in refused.text
    approved = client.post(f"/drafts/{second}/approve", data={"confirm_stale": "1"})
    assert approved.status_code == 200 and stored(engine, second).status == "approved"
    assert "NEW EVIDENCE" not in client.get(f"/drafts/{elsewhere}").text

    assert client.post(f"/drafts/{elsewhere}/reject", data={"reason": "x"}).status_code == 422
    rejected = client.post(f"/drafts/{elsewhere}/reject", data={"reason": "Wrong fish pens"})
    assert rejected.status_code == 200 and stored(engine, elsewhere).status == "rejected"
    assert client.post(f"/drafts/{second}/reject", data={"reason": "Too late"}).status_code == 409
    with Session(engine) as session:
        actions = [e.action for e in session.exec(select(DraftEvent).order_by(DraftEvent.id))]
    assert actions == ["generated", "superseded", "generated", "generated", "approved", "rejected"]

    with sqlite3.connect(engine.url.database) as connection:
        for statement in (
            "UPDATE draft SET content = '{}' WHERE id = ?",
            "UPDATE draft SET incident_id = NULL WHERE id = ?",
            "DELETE FROM draft WHERE id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (second,))


def test_versions_follow_the_incident_when_its_barangay_changes(client, engine):
    from app.models import Incident

    incident, *_ = fisherfolk_incident(engine)
    first = generate(client, incident)
    with Session(engine) as session:
        row = session.get(Incident, incident)
        row.barangay = "Malaban"  # e.g. a late pin moved the incident
        session.add(row)
        session.commit()
    second = generate(client, incident)
    assert stored(engine, first).status == "superseded"
    assert (stored(engine, second).version, stored(engine, second).barangay) == (2, "Malaban")
    page = client.get(f"/drafts/{first}").text
    assert f'<a href="/drafts/{second}">version 2</a>' in page


def test_a_pending_grouping_review_needs_a_reason_to_approve(client, engine):
    incident = add_incident(engine, barangay="De La Paz", status="needs_officer")
    report = add_report(
        engine, incident=incident, barangay="De La Paz", fish_cages_or_pens_damaged=4
    )
    draft = stored(engine, generate(client, incident))
    assert draft.content["pending_report_ids"] == [report]
    assert "pending grouping review: 4" in fields(draft)["9"]["value"]
    assert "PENDING GROUPING REVIEW" in client.get(f"/drafts/{draft.id}").text
    assert client.post(f"/drafts/{draft.id}/approve", data={}).status_code == 422
    ok = client.post(f"/drafts/{draft.id}/approve", data={"reason": "Same fish pens, checked"})
    assert ok.status_code == 200


def test_generating_a_notice_of_loss_must_be_same_origin(client, engine):
    incident, *_ = fisherfolk_incident(engine)
    refused = client.post(
        "/drafts/notice-of-loss",
        data={"commodity": "fisheries", "incident_id": str(incident)},
        headers={"Origin": "http://evil.example"},
    )
    assert refused.status_code == 403
    assert generate(client, incident, Origin="http://localhost")


def test_init_db_adds_the_incident_column_to_an_older_draft_table(settings):
    engine = make_engine(settings)
    init_db(engine)
    with engine.connect() as connection:  # rebuild the draft table as slice 4 created it
        for (name,) in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).all():
            connection.exec_driver_sql(f"DROP TRIGGER {name}")
        columns = [
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(draft)")
            if row[1] != "incident_id"
        ]
        connection.exec_driver_sql(f"CREATE TABLE old AS SELECT {', '.join(columns)} FROM draft")
        connection.exec_driver_sql("DROP TABLE draft")
        connection.exec_driver_sql("ALTER TABLE old RENAME TO draft")
        connection.commit()
    init_db(engine)
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(draft)")}
        trigger = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'draft_frozen'"
        ).scalar_one()
    assert "incident_id" in columns and "NEW.incident_id IS NOT OLD.incident_id" in trigger
    engine.dispose()


# --- A second commodity is data only --------------------------------------------------------


def test_a_new_commodity_needs_one_definition_and_no_code_change(client, engine, monkeypatch):
    monkeypatch.setitem(
        DEADLINES,
        "test_crop_nl",
        Deadline(
            key="test_crop_nl",
            label="Test crop notice",
            amount=3,
            unit="calendar_days",
            start="occurrence of loss",
            source="A test-only brochure",
            url="https://example.org/test.pdf",
        ),
    )
    crops = replace(
        FISHERIES,
        key="testcrop",
        name="test crop",
        title="NOTICE OF LOSS (Test Crop)",
        fields=(
            NolField("1", "Name of Farmer"),
            NolField("2", "Location of Farm", "evidence", "location"),
            NolField("3", "Cause", "evidence", "cause"),
            NolField("4", "Extent", "evidence", "extent"),
        ),
        deadline="test_crop_nl",
        later_deadlines=(),
        eligible_livelihood=("crops",),
        eligible_counts=(),
        extent_livelihood=("crops",),
        extent_counts=(),
        area_facts=("crop_area_hectares",),
    )
    check(crops)  # the same checks as the built-in entries
    monkeypatch.setitem(COMMODITIES, crops.key, crops)
    incident = add_incident(engine, barangay="Casile")
    add_report(
        engine, incident=incident, barangay="Casile", hazards=["flood"], livelihood_damage=["crops"]
    )
    fisherfolk_incident(engine)

    html = client.get("/drafts").text
    assert "PCIC Notice of Loss (test crop)" in html and f'id="nol-testcrop-{incident}"' in html
    assert "Test crop notice: <strong" in html and "due in 67h 0m" in html
    draft_id = generate(client, incident, commodity="testcrop")
    draft = stored(engine, draft_id)
    assert draft.kind == "pcic_nl_testcrop"
    assert [f["mode"] for f in draft.content["fields"]] == [
        "assured",
        "evidence",
        "evidence",
        "evidence",
    ]
    assert fields(draft)["4"]["value"] == "Reported damage to crops"
    page = client.get(f"/drafts/{draft_id}").text
    assert "NOTICE OF LOSS (Test Crop)" in page and "due in 67h 0m" in page
    assert client.post(f"/drafts/{draft_id}/approve", data={}).status_code == 200
    assert "PCIC Notice of Loss (test crop) · incident" in client.get("/drafts").text


def test_barangay_fingerprints_are_unchanged_by_the_per_incident_split(client, engine):
    """Drafts generated before slice 5 must not turn stale: same data, same hash."""
    import hashlib

    from app.counting import collect

    fisherfolk_incident(engine)
    with Session(engine) as session:
        evidence = collect(session).barangays["De La Paz"]
    legacy = [
        {
            "incident": i.incident_id,
            "status": i.status,
            "reporters": i.reporter_count,
            "reports": [
                [m.id, m.at.isoformat(), m.point, m.flagged, i.facts(m)] for m in i.members
            ],
        }
        for i in evidence.incidents
    ]
    digest = hashlib.sha256(json.dumps(legacy, sort_keys=True, default=str).encode()).hexdigest()
    assert evidence.fingerprint() == digest

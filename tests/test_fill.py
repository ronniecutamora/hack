"""Filling in a draft: typed controls, server validation, versions, the audit, regeneration,
personal fields kept out of the database, and the paper printout (app.fillable, slice 7h)."""

import dataclasses
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import Session, select

from app import fillable
from app.drafts import form_fields
from app.main import create_app
from app.models import Draft, DraftEvent
from app.nol_form import FISHERIES
from app.rdana_form import all_fields
from tests.test_nol import Clock, fisherfolk_incident
from tests.test_nol import generate as generate_nol
from tests.test_rdana import AT, SummaryLLM, add_incident, add_report, cite_all

CSS = Path(__file__).parent.parent / "app/static/css/app.css"
SECRET = "Juana Dela Cruz 0917-555-0101"


@pytest.fixture
def clock():
    return Clock(AT + timedelta(hours=5))


@pytest.fixture
def client(settings, clock):
    app = create_app(settings, summary_llm=SummaryLLM(*[cite_all] * 10), clock=clock)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def generate(client, **fields):
    response = client.post(
        "/drafts", data={"barangay": "Malaban", **fields}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def fill(client, draft_id, fields, officer="RC", **extra):
    data = [("officer", officer), *((f"f.{k}", v) for k, v in fields), *extra.items()]
    return client.post(
        f"/drafts/{draft_id}/fill",
        content=urlencode(data),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def saved_id(response):
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.endswith("?saved=1"), location
    return int(location.split("/")[-1].split("?")[0])


def stored(engine, draft_id):
    with Session(engine) as session:
        return session.get(Draft, draft_id)


def all_drafts(engine):
    with Session(engine) as session:
        return list(session.exec(select(Draft).order_by(Draft.id)).all())


def draft_events(engine, draft_id):
    with Session(engine) as session:
        query = select(DraftEvent).where(DraftEvent.draft_id == draft_id).order_by(DraftEvent.id)
        return list(session.exec(query).all())


def approve(client, draft_id, **data):
    return client.post(f"/drafts/{draft_id}/approve", data=data, follow_redirects=False)


def rdana(engine, client, **report):
    incident = add_incident(engine)
    add_report(engine, incident=incident, houses_partially_damaged=4, **report)
    return generate(client)


# --- The form as data ----------------------------------------------------------------------


def test_every_field_has_a_control_and_personal_fields_are_the_identifying_ones():
    fields = [f for f in all_fields() if not f.heading]
    for f in [*fields, *FISHERIES.letter_fields, *FISHERIES.fields]:
        assert f.control.kind in fillable.KINDS, f.key
        if f.control.kind in fillable.CHOICES:
            assert f.options, f.key
    personal = {f.key for f in fields if fillable.is_personal(f)}
    assert personal == {
        *(f"1.3.{n}" for n in range(1, 7)),
        "submitted.name",
        "submitted.contact",
        "submitted.email",
    }
    nol_personal = {f.key for f in FISHERIES.fields if fillable.is_personal(f)}
    assert nol_personal == {"1", "2", "7", "10", "12"}
    asked = {f.key for f in fields if f.ask and not f.personal}
    assert asked == {
        "1.1.1",
        "1.1.2",
        "1.1.3",
        "20.1",
        "20.2",
        "submitted.designation",
        "submitted.organization",
        "submitted.team",
    }
    assert not any(f.editable for f in fields if f.mode == "fixed")
    assert not next(f for f in fields if f.key == "1.2.4").editable  # the draft's barangay


def every_field():
    return [
        *(f for f in all_fields() if not f.heading),
        *FISHERIES.letter_fields,
        *FISHERIES.fields,
    ]


def test_every_field_has_short_help_with_a_source():
    for f in every_field():
        assert f.help and len(f.help) <= fillable.HELP_MAX, f.key
        assert f.help_source, f.key
        if fillable.cites_document(f):  # a named official document, with its page or section
            assert re.match(r"(NDRRMC Memo 107 s\.2021|PCIC )", f.help_source), f.help_source
            assert re.search(r"(p\. \d+|sec\. [\d.]+|A\.\d|: )", f.help_source), f.help_source
        else:
            assert f.help_source.startswith("The form's own label ("), f.help_source
    cited = sum(fillable.cites_document(f) for f in every_field())
    assert 0 < cited < len(every_field())
    with pytest.raises(ValueError, match="needs help"):
        fillable.check_field(dataclasses.replace(FISHERIES.fields[0], help=None), "test")


@pytest.mark.parametrize(
    ("key", "typed", "message"),
    [
        ("2.2", "many", "Enter a number"),
        ("2.2", "3.5", "whole number"),
        ("2.2", "-1", "0 or more"),
        ("2.2", "2000000", "1,000,000 or less"),
        ("1.1.3", "yesterday", "Enter a date and time"),
        ("1.1.3", "2099-01-01T10:00", "cannot be in the future"),
        ("1.1.3", "1990-01-01T10:00", "too far in the past"),
        ("3.1", "Maybe", "own options"),
        ("3.2", "Jeepney", "own options"),
        ("1.1.1", "x" * 301, "at most 300 characters"),
    ],
)
def test_bad_values_are_refused_with_the_message_under_the_field(
    client, engine, key, typed, message
):
    draft_id = rdana(engine, client)
    response = fill(client, draft_id, [(key, typed), ("2.5", "12")])
    assert response.status_code == 422
    page = response.text
    assert "NOT SAVED." in page and "Everything you typed is still here" in page
    block = page.split(f'id="f-{key}"')[1].split('class="fill-field')[0]
    assert message in block and 'aria-invalid="true"' in block
    other = page.split('id="f-2.5"')[1].split('class="fill-field')[0]
    assert 'value="12"' in other  # the valid entry typed beside it is kept on screen
    assert len(all_drafts(engine)) == 1 and stored(engine, draft_id).entries is None


def test_saving_makes_a_version_and_an_audit_with_field_keys_only(client, engine):
    draft_id = rdana(engine, client)
    before = stored(engine, draft_id)
    response = fill(
        client,
        draft_id,
        [
            ("1.1.1", "Typhoon Kristine response"),
            ("2.2", "1,250"),
            ("3.2", ""),
            ("3.2", "Foot"),
            ("3.2", "Boat"),
            ("20.1", "People facing serious problems"),
            ("10.2", "4"),  # unchanged: the reports' value
        ],
        officer="RC",
    )
    new_id = saved_id(response)
    new = stored(engine, new_id)
    assert (new.version, new.status) == (2, "draft")
    assert stored(engine, draft_id).status == "superseded"
    assert new.content == before.content  # evidence values as generated, never touched
    assert {k: v["value"] for k, v in new.entries.items()} == {
        "1.1.1": "Typhoon Kristine response",
        "2.2": "1250",
        "3.2": "Foot, Boat",
        "20.1": "People facing serious problems",
    }
    assert all(v["by"] == "RC" for v in new.entries.values())
    (edited,) = draft_events(engine, new_id)
    assert (edited.action, edited.actor) == ("edited", "RC")
    assert edited.detail["fields"] == ["1.1.1", "2.2", "20.1", "3.2"]
    assert "Kristine" not in json.dumps(edited.detail)  # keys only, never values
    page = client.get(f"/drafts/{new_id}?saved=1").text
    assert "Saved as version 2" in page
    block = page.split('id="f-1.1.1"')[1].split('class="fill-field')[0]
    assert "Edited by RC" in block and 'value="Typhoon Kristine response"' in block
    assert "Saved as version 2; 4 fields now the office" in page


def test_nothing_changed_saves_nothing(client, engine):
    draft_id = rdana(engine, client)
    response = fill(client, draft_id, [("10.2", "4"), ("2.2", "")])
    assert response.headers["location"] == f"/drafts/{draft_id}?saved=0"
    assert len(all_drafts(engine)) == 1
    assert "Nothing to save" in client.get(response.headers["location"]).text


def test_the_reports_value_is_one_tap_away_and_can_be_restored(client, engine):
    draft_id = rdana(engine, client)
    v2 = saved_id(fill(client, draft_id, [("10.2", "6")]))
    page = client.get(f"/drafts/{v2}").text
    block = page.split('id="f-10.2"')[1].split('class="fill-field')[0]
    assert "Edited by RC" in block and "Before: <strong>4</strong>" in block
    assert 'name="restore" value="10.2"' in block
    v3 = saved_id(fill(client, v2, [], restore="10.2"))
    assert stored(engine, v3).entries is None
    assert draft_events(engine, v3)[0].detail["restored"] == ["10.2"]


def test_approve_acts_on_the_exact_version(client, engine):
    draft_id = rdana(engine, client)
    v2 = saved_id(fill(client, draft_id, [("20.2", "Roofs gone along the lake.")]))
    wrong = approve(client, v2, version="1")
    assert wrong.status_code == 409 and "not version 1" in wrong.text
    old = approve(client, draft_id, version="1")
    assert old.status_code == 409  # superseded by the save
    ok = approve(client, v2, version="2", decided_by="RC")
    assert ok.status_code == 303 and stored(engine, v2).status == "approved"


def test_editing_an_approved_version_makes_a_new_undecided_one(client, engine):
    draft_id = rdana(engine, client)
    assert approve(client, draft_id, version="1").status_code == 303
    page = client.get(f"/drafts/{draft_id}").text
    assert "Saving makes version 2, which needs approval again." in page
    v2 = saved_id(fill(client, draft_id, [("1.1.1", "Typhoon response")]))
    assert stored(engine, draft_id).status == "approved"  # frozen as approved
    assert (stored(engine, v2).status, stored(engine, v2).version) == ("draft", 2)


def test_a_superseded_version_takes_no_entries(client, engine):
    draft_id = rdana(engine, client)
    saved_id(fill(client, draft_id, [("2.2", "10")]))
    late = fill(client, draft_id, [("2.2", "11")])
    assert late.status_code == 409 and "Version 2 is newer" in late.text
    page = client.get(f"/drafts/{draft_id}").text
    assert "cannot be changed" in page and "disabled" in page.split('id="f-2.2"')[1][:1500]


def test_regenerating_keeps_the_officer_values_and_shows_moved_evidence(client, engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, houses_partially_damaged=4)
    draft_id = generate(client)
    v2 = saved_id(fill(client, draft_id, [("10.2", "7"), ("20.2", "Seen on site.")]))
    add_report(engine, incident=incident, reporter="b", hours=1, houses_partially_damaged=9)
    v3 = generate(client)
    new = stored(engine, v3)
    assert new.version == 3
    assert {k: v["value"] for k, v in new.entries.items()} == {"10.2": "7", "20.2": "Seen on site."}
    assert draft_events(engine, v3)[0].detail["carried_fields"] == ["10.2", "20.2"]
    field = {f["key"]: f for s in new.content["sections"] for f in s["fields"]}["10.2"]
    assert field["value"] not in (None, "4")  # the evidence moved underneath the officer's 7
    page = client.get(f"/drafts/{v3}").text
    block = page.split('id="f-10.2"')[1].split('class="fill-field')[0]
    assert 'value="7"' in block
    assert f"The reports now say: {field['value']} (was 4 when edited)" in block
    assert stored(engine, v2).status == "superseded"


def test_an_event_name_given_when_generating_replaces_the_carried_one(client, engine):
    draft_id = rdana(engine, client)
    saved_id(fill(client, draft_id, [("1.1.1", "Old name")]))
    v3 = generate(client, event_name="New name")
    assert stored(engine, v3).entries is None


def test_personal_fields_are_never_posted_stored_or_logged(client, engine, caplog):
    caplog.set_level(logging.DEBUG)
    draft_id = rdana(engine, client)
    page = client.get(f"/drafts/{draft_id}").text
    for key in ("1.3.1", "1.3.5", "submitted.name", "submitted.email"):
        block = page.split(f'id="f-{key}"')[1].split('class="fill-field')[0]
        assert f'data-personal="{key}"' in block and "name=" not in block.split("<input")[1]
    response = fill(
        client,
        draft_id,
        [("1.3.1", SECRET), ("submitted.contact", SECRET), ("20.2", "Checked.")],
    )
    new_id = saved_id(response)
    assert set(stored(engine, new_id).entries) == {"20.2"}
    dump = json.dumps([d.model_dump(mode="json") for d in all_drafts(engine)])
    events = json.dumps([e.model_dump(mode="json") for e in draft_events(engine, new_id)])
    assert "Juana" not in dump and "0917" not in dump and "Juana" not in events
    assert "Juana" not in caplog.text


def test_no_placeholder_text_in_either_form_or_mode(client, engine):
    draft_id = rdana(engine, client)
    incident, _first, _second = fisherfolk_incident(engine)
    nol_id = generate_nol(client, incident)
    for mode in ("technical", "simple"):
        client.get(f"/?mode={mode}")
        for page_id in (draft_id, nol_id):
            page = client.get(f"/drafts/{page_id}").text
            assert "not in reports" not in page and "to be filled by" not in page, (mode, page_id)


def test_simple_mode_asks_the_office_first_with_progress(client, engine):
    draft_id = rdana(engine, client)
    client.get("/?mode=simple")
    page = client.get(f"/drafts/{draft_id}").text
    ask = page.split('id="ask-title"')[1].split('id="check-title"')[0]
    done = int(re.search(r"(\d) of 8 done", ask).group(1))  # 1.1.3 comes from the reports
    assert ask.index('id="f-1.1.1"') < ask.index('id="f-20.2"') < ask.index('id="f-1.3.1"')
    assert "For the printout only" in ask
    assert "For the RDANA team on site (optional now)" in page
    v2 = saved_id(fill(client, draft_id, [("1.1.1", "Typhoon"), ("20.2", "Seen.")]))
    assert f"{done + 2} of 8 done" in client.get(f"/drafts/{v2}").text


def test_nol_fields_are_filled_and_saved_like_the_rdana_form(client, engine, clock):
    incident, _first, _second = fisherfolk_incident(engine)
    nol_id = generate_nol(client, incident)
    v2 = saved_id(
        fill(
            client,
            nol_id,
            [
                ("date", "2026-10-28"),
                ("policy_type", "Fish Cage Insurance"),
                ("6", "0.5"),
                ("8", "2026-12-15"),  # a harvest date may be in the future
                ("10", "PCIC-123"),  # personal: dropped
            ],
        )
    )
    entries = stored(engine, v2).entries
    assert {k: v["value"] for k, v in entries.items()} == {
        "date": "2026-10-28",
        "policy_type": "Fish Cage Insurance",
        "6": "0.5",
        "8": "2026-12-15",
    }
    future = fill(client, v2, [("date", "2026-12-01")])
    assert future.status_code == 422 and "cannot be in the future" in future.text
    assert [f.key for f in form_fields(stored(engine, v2))][:3] == ["date", "policy_type", "1"]


# --- The printout --------------------------------------------------------------------------


def paper(page: str) -> str:
    return page[page.index('<div class="paper') :].split("</main>")[0]


def test_the_printout_is_the_paper_form_only(client, engine):
    draft_id = rdana(engine, client)
    v2 = saved_id(fill(client, draft_id, [("1.1.1", "Typhoon Kristine response")]))
    page = client.get(f"/drafts/{v2}").text
    sheet = paper(page)
    assert "DRAFT, NOT APPROVED · version 2" in sheet and "edited by RC" in sheet
    assert "RDANA FORM" in sheet and "To be accomplished within the first 72 HOURS" in sheet
    assert "Typhoon Kristine response" in sheet and ">4<" in sheet
    assert "1.3.1" in sheet and 'data-personal-print="1.3.1"' in sheet
    assert '<td class="paper__num"></td>' in sheet and 'paper__num">submitted' not in sheet
    assert "☐ Yes" in sheet and "☐ No" in sheet  # empty boxes for 3.1
    for absent in (
        "<form",
        "<input",
        "<button",
        "<a ",
        "Hazards named in the reports",
        "Time of the earliest report",
        "/reports",  # no evidence links
        "#report-",
        "What's RDANA",
        "chip",
        "fill-hint",
        "Drafted ",
    ):
        assert absent not in sheet, absent
    assert '<div class="screen-only">' in page  # the rest of the page is screen only


def test_the_print_stylesheet_hides_everything_but_the_paper():
    css = CSS.read_text()
    for rule in (
        ".app-bar, .nav, .snackbar, .progress, .skip-link, nav, .no-print "
        "{ display: none !important; }",
        ".live, .live-notice { display: none !important; }",
        ".screen-only, .fill-bar, .help-btn, .help-pop, .print-check, .fill-private, "
        ".fill-private-note { display: none !important; }",
        ".print-only { display: block !important; }",
        "@page { size: A4;",
    ):
        assert rule in css, rule


def test_the_nol_printout_is_the_letter(client, engine):
    incident, _first, _second = fisherfolk_incident(engine)
    nol_id = generate_nol(client, incident)
    response = approve(client, nol_id, decided_by="RC", version="1", reason="Checked on site.")
    assert response.status_code == 303, response.text
    sheet = paper(client.get(f"/drafts/{nol_id}").text)
    assert sheet.startswith('<div class="paper paper--letter print-only">')
    assert "APPROVED · version 1" in sheet and "· RC" in sheet
    assert "FIP CAS Form No. 1" in sheet and "THE REGIONAL MANAGER" in sheet
    assert "with Insurance Policy No. <span" in sheet and 'data-personal-print="10"' in sheet
    assert "Flood, Strong wind" in sheet
    for absent in ("<form", "<a ", "annex", "Annex", "PHOTO REUSED", "Filing clock", "chip"):
        assert absent not in sheet, absent


def test_a_whole_form_save_fits_the_size_limit(client, engine):
    draft_id = rdana(engine, client)
    long = [(f.key, "Ñ" * 2000) for f in all_fields() if f.control.kind == "long" and f.editable]
    assert len(long) >= 5
    saved_id(fill(client, draft_id, long))


# --- Help, personal details and printing (slice 7i) ---------------------------------------


def field_block(page: str, key: str) -> str:
    return page.split(f'id="f-{key}"')[1].split('class="fill-field')[0]


def test_every_field_has_its_help_button_in_both_modes_and_none_on_paper(client, engine):
    draft_id = rdana(engine, client)
    incident, _first, _second = fisherfolk_incident(engine)
    nol_id = generate_nol(client, incident)
    nol_fields = [*FISHERIES.letter_fields, *FISHERIES.fields]
    rdana_fields = [f for f in all_fields() if not f.heading]
    for mode in ("technical", "simple"):
        client.get(f"/?mode={mode}")
        for page_id, fields in ((draft_id, rdana_fields), (nol_id, nol_fields)):
            page = client.get(f"/drafts/{page_id}").text
            for f in fields:
                block = field_block(page, f.key)
                assert f'popovertarget="help-{f.key}" aria-label="Help: {escape(f.label)}"' in (
                    block
                ), (mode, f.key)
                assert f'id="help-{f.key}" popover role="dialog"' in block, (mode, f.key)
                assert str(escape(f.help)) in block and str(escape(f.help_source)) in block
            sheet = paper(page)
            assert "help-" not in sheet and "popover" not in sheet
            assert not any(str(escape(f.help)) in sheet for f in fields)


def test_personal_fields_say_where_they_are_kept_and_can_be_cleared(client, engine):
    draft_id = rdana(engine, client)
    for mode in ("technical", "simple"):
        client.get(f"/?mode={mode}")
        page = client.get(f"/drafts/{draft_id}").text
        for key in ("1.3.1", "submitted.contact"):
            assert fillable.PERSONAL_NOTE in field_block(page, key)
        assert page.count("data-personal-clear") == 1 and "Clear personal details" in page
        assert "data-shared-warning hidden" in page  # app.js shows it once, at the first entry
        assert fillable.PERSONAL_NOTE not in field_block(page, "1.1.1")
    # The save endpoint keeps nothing personal, even when a personal key is posted.
    v2 = saved_id(fill(client, draft_id, [("1.3.2", "52"), ("1.3.6", "a@b.ph"), ("1.1.1", "T")]))
    assert set(stored(engine, v2).entries) == {"1.1.1"}


def test_print_is_final_on_an_approved_version_and_checks_for_empty_fields(client, engine):
    draft_id = rdana(engine, client)
    page = client.get(f"/drafts/{draft_id}").text
    assert "Print draft for checking" in page and "Print final form" not in page
    gaps = page.split("data-print-gaps")[1].split("</ul>")[0]
    assert int(re.search(r'data-count="(\d+)"', gaps).group(1)) == gaps.count("data-fill-jump")
    assert 'data-fill-jump="1.1.1"' in gaps and 'data-fill-jump="1.3.1"' not in gaps
    entries = [
        ("1.1.1", "Typhoon Kristine response"),
        ("1.1.2", "Typhoon and flood"),
        ("20.1", "People facing serious problems"),
        ("20.2", "Flooded puroks."),
        ("submitted.designation", "CDRRMO Head"),
        ("submitted.organization", "Biñan City DRRM Office"),
        ("submitted.team", "Team Alpha"),
        ("1.1.3", "2026-10-20 22:00"),
        ("1.2.7", "2026-10-21 09:00"),
    ]
    v2 = saved_id(fill(client, draft_id, entries))
    page = client.get(f"/drafts/{v2}").text
    assert 'data-count="0"' in page
    response = approve(client, v2, decided_by="RC", version="2", reason="Checked on site.")
    assert response.status_code == 303, response.text
    page = client.get(f"/drafts/{v2}").text
    assert "Print final form" in page and "Print draft for checking" not in page
    assert paper(page).count("APPROVED · version 2") >= 1

"""Officer review: the queue, audited overrides that stick, and their effect on counts and drafts.

No model call is real: the pair judge and the summary LLM are fakes.
"""

import asyncio
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import trust
from app.assessment import AssessmentService
from app.counting import collect
from app.incidents import IncidentService
from app.intake import Admission, IntakeService
from app.live import read_tokens
from app.main import create_app
from app.models import (
    AgentDecision,
    Assessment,
    Draft,
    Incident,
    OfficerAction,
    PhotoAssessment,
    Report,
    Reporter,
)
from app.privacy import pseudonymize
from tests.test_assessment import FakeLLM, FakeSam, facts
from tests.test_incidents import AT, DIFFERENT, SAME, UNSURE, FakeJudge, add_report
from tests.test_rdana import SummaryLLM, cite_all

HEX_A, HEX_B = "a1" * 32, "b2" * 32


@pytest.fixture
def judge():
    return FakeJudge()


@pytest.fixture
def client(settings, judge):
    app = create_app(settings, summary_llm=SummaryLLM(*[cite_all] * 20), pair_judge=judge)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def rows(engine, model):
    with Session(engine) as session:
        return session.exec(select(model).order_by(model.id)).all()


def report(engine, report_id) -> Report:
    with Session(engine) as session:
        return session.get(Report, report_id)


def casile_with_uncertain_photo(engine, settings, judge):
    """The live shape: a voice note (3 houses) and a photo report the judge was unsure about."""
    service = IncidentService(settings, engine, judge)
    judge.replies = [UNSURE]
    voice = asyncio.run(
        service.group(
            add_report(
                engine,
                barangay="Casile",
                houses_partially_damaged=3,
                place="Purok 2",
                reporter=HEX_A,
            )
        )
    )
    photo = asyncio.run(
        service.group(
            add_report(
                engine,
                barangay="Casile",
                at=AT + timedelta(hours=1),
                houses_partially_damaged=5,
                place="Purok 2 covered court",
                reporter=HEX_B,
            )
        )
    )
    assert photo.grouping_needs_officer and photo.incident_id != voice.incident_id
    return voice, photo


def act(client, report_id, action, **form):
    data = {"officer": "JDC", "next": "/review", **form}
    return client.post(f"/reports/{report_id}/review/{action}", data=data, follow_redirects=False)


def houses(engine):
    with Session(engine) as session:
        return collect(session).barangays["Casile"].count("houses_partially_damaged")


# --- Merge -----------------------------------------------------------------------------------


def test_merge_moves_the_report_counts_it_stales_the_draft_and_sticks(
    client, engine, settings, judge
):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    before = houses(engine)
    assert (before.value, before.pending_value) == (3, 5)
    response = client.post("/drafts", data={"barangay": "Casile"}, follow_redirects=False)
    draft_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert not client.app.state.drafts.is_stale(rows(engine, Draft)[0])

    response = act(client, photo.id, "merge", incident_id=voice.incident_id, note="same purok")
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/review?done=merge&report={photo.id}&incident={voice.incident_id}"
    )

    moved = report(engine, photo.id)
    assert (moved.incident_id, moved.officer_incident_id) == (voice.incident_id, voice.incident_id)
    assert moved.grouping_needs_officer is False
    (action,) = rows(engine, OfficerAction)
    assert (action.kind, action.officer, action.note) == ("officer_merged", "JDC", "same purok")
    assert (action.from_incident_id, action.incident_id) == (photo.incident_id, voice.incident_id)
    # The pending bucket drops, the confirmed figure rises, the old incident retires.
    after = houses(engine)
    assert (after.value, after.pending, after.pending_value) == (5, (), None)
    statuses = {i.id: (i.status, i.active) for i in rows(engine, Incident)}
    assert statuses[voice.incident_id] == ("open", True)
    assert statuses[photo.incident_id][1] is False  # retired; its history stays readable
    assert client.app.state.drafts.is_stale(rows(engine, Draft)[0])
    assert "stale" in client.get(f"/drafts/{draft_id}").text.lower()

    # A forced automatic re-group (fingerprint gone, a judge that would disagree) changes nothing.
    with Session(engine) as session:
        row = session.get(Report, photo.id)
        row.grouping_fingerprint = None
        session.add(row)
        session.commit()
    regrouper = FakeJudge(DIFFERENT, DIFFERENT)
    again = asyncio.run(IncidentService(settings, engine, regrouper).group(photo.id))
    assert again.incident_id == voice.incident_id and regrouper.calls == []
    assert not [d for d in rows(engine, AgentDecision) if d.kind == "re-linked"]


def test_merge_only_into_a_compared_or_same_barangay_incident(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    elsewhere = asyncio.run(
        IncidentService(settings, engine, FakeJudge()).group(
            add_report(engine, barangay="Malaban", reporter="c3" * 32)
        )
    )
    response = act(client, photo.id, "merge", incident_id=elsewhere.incident_id)
    assert response.status_code == 400
    assert "same barangay" in response.text
    assert report(engine, photo.id).incident_id == photo.incident_id
    assert rows(engine, OfficerAction) == []


# --- Keep separate, set place ----------------------------------------------------------------


def test_keep_separate_confirms_the_incident_and_survives_a_judge_saying_same(
    client, engine, settings, judge
):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    assert act(client, photo.id, "keep").status_code == 303
    kept = report(engine, photo.id)
    assert (kept.incident_id, kept.officer_incident_id) == (photo.incident_id, photo.incident_id)
    assert kept.grouping_needs_officer is False
    with Session(engine) as session:
        assert session.get(Incident, photo.incident_id).status == "open"
    after = houses(engine)
    assert (after.value, after.pending_value) == (3 + 5, None)  # two confirmed incidents
    with Session(engine) as session:
        row = session.get(Report, photo.id)
        row.grouping_fingerprint = None
        session.add(row)
        session.commit()
    regrouper = FakeJudge(SAME)
    again = asyncio.run(IncidentService(settings, engine, regrouper).group(photo.id))
    assert again.incident_id == photo.incident_id and regrouper.calls == []
    assert [a.kind for a in rows(engine, OfficerAction)] == ["officer_kept_separate"]


def casile_wrongly_merged(engine, settings):
    """Two Casile reports the judge confidently (and wrongly) merged into one incident."""
    service = IncidentService(settings, engine, FakeJudge(SAME))
    voice = asyncio.run(
        service.group(
            add_report(engine, barangay="Casile", houses_partially_damaged=3, reporter=HEX_A)
        )
    )
    photo = asyncio.run(
        service.group(
            add_report(
                engine,
                barangay="Casile",
                at=AT + timedelta(hours=1),
                houses_partially_damaged=5,
                place="Purok 5 bodega",
                reporter=HEX_B,
            )
        )
    )
    assert photo.incident_id == voice.incident_id and not photo.grouping_needs_officer
    return voice, photo


def test_keep_separate_splits_a_wrong_merge_into_a_new_incident(client, engine, settings):
    voice, photo = casile_wrongly_merged(engine, settings)
    assert houses(engine).value == 5  # one incident: the highest figure, not the sum
    response = client.post("/drafts", data={"barangay": "Casile"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(f"/incidents/{voice.incident_id}").text
    assert page.count("Split into its own incident</button>") == 2  # one per member

    assert act(client, photo.id, "keep", note="different purok").status_code == 303

    split = report(engine, photo.id)
    assert split.incident_id not in (None, voice.incident_id)
    assert split.officer_incident_id == split.incident_id
    assert report(engine, voice.id).incident_id == voice.incident_id
    (action,) = rows(engine, OfficerAction)
    assert (action.kind, action.from_incident_id, action.incident_id) == (
        "officer_split",
        voice.incident_id,
        split.incident_id,
    )
    with Session(engine) as session:
        old = session.get(Incident, voice.incident_id)
        new = session.get(Incident, split.incident_id)
        assert (old.active, old.latest_facts["report_count"]) == (True, 1)
        assert (new.active, new.latest_facts["report_count"], new.barangay) == (True, 1, "Casile")
        assert "Purok 5" in new.title
    assert houses(engine).value == 3 + 5  # two incidents now: summed
    assert client.app.state.drafts.is_stale(rows(engine, Draft)[0])
    # Both incident pages show the split; the lone report is now offered "Keep separate".
    for incident_id in (voice.incident_id, split.incident_id):
        html = client.get(f"/incidents/{incident_id}").text
        assert "split into its own incident by the officer" in html
        assert "Split into its own incident</button>" not in html
    # Re-grouping, even with a judge that says "same", never merges it back.
    with Session(engine) as session:
        row = session.get(Report, photo.id)
        row.grouping_fingerprint = None
        session.add(row)
        session.commit()
    regrouper = FakeJudge(SAME)
    again = asyncio.run(IncidentService(settings, engine, regrouper).group(photo.id))
    assert again.incident_id == split.incident_id and regrouper.calls == []


def test_set_place_regroups_with_the_new_barangay_and_reassessment_keeps_it(
    client, engine, settings, judge
):
    service = IncidentService(settings, engine, judge)
    voice = asyncio.run(
        service.group(add_report(engine, barangay="Casile", houses_partially_damaged=3))
    )
    lost = asyncio.run(service.group(add_report(engine, barangay=None, at=AT + timedelta(hours=1))))
    assert lost.grouping_needs_officer and lost.incident_id != voice.incident_id
    page = client.get("/review").text
    assert "place unknown" in page and f'id="review-{lost.id}"' in page

    judge.replies = [SAME]
    response = act(
        client, lost.id, "place", barangay="Casile", note="Caller confirmed Casile by phone"
    )
    assert response.status_code == 303, response.text
    placed = report(engine, lost.id)
    assert (placed.barangay, placed.officer_barangay) == ("Casile", "Casile")
    assert placed.incident_id == voice.incident_id  # re-grouped by the judge with its new place
    assert placed.grouping_needs_officer is False and placed.officer_incident_id is None
    (action,) = rows(engine, OfficerAction)
    assert action.detail == {"barangay": "Casile", "previous": None}
    assert action.note == "Caller confirmed Casile by phone"
    assert any(d.kind == "merged" and d.report_id == lost.id for d in rows(engine, AgentDecision))

    # Re-assessment never overwrites the officer's barangay; the agent's finding is kept beside.
    assessor = AssessmentService(
        settings, engine, FakeLLM(facts(hazards=["flood"])), FakeSam(), service
    )
    with Session(engine) as session:
        row = session.get(Report, lost.id)
        row.status = "new"
        session.add(row)
        session.commit()
    reassessed = asyncio.run(assessor.assess(lost.id))
    assert (reassessed.status, reassessed.barangay) == ("assessed", "Casile")
    with Session(engine) as session:
        stored = session.exec(select(Assessment).where(Assessment.report_id == lost.id)).one()
    assert stored.geo_source == "officer" and "the agent found no barangay" in stored.geo_detail


def test_set_place_needs_a_gazetteer_barangay_and_a_reason(client, engine, settings, judge):
    _voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    assert (
        act(client, photo.id, "place", barangay="Atlantis", note="long reason").status_code == 400
    )
    response = act(client, photo.id, "place", barangay="Malaban", note="")
    assert response.status_code == 400 and "Reason" in response.text
    assert rows(engine, OfficerAction) == []


# --- Trust signals, fallen trees, blocking ---------------------------------------------------


def flag_photo_reuse(engine, report_id):
    signal = {
        "code": "photo_reused",
        "severity": "high",
        "detail": "Photo 9: identical bytes in report #1; another reporter.",
        "related_report_id": 1,
    }
    with Session(engine) as session:
        assessment = session.exec(select(Assessment).where(Assessment.report_id == report_id)).one()
        assessment.signals = [signal]
        session.add(assessment)
        session.commit()
    return trust.signal_key(signal)


def test_trust_signal_reviewed_genuine_unflags_the_report(client, engine, settings, judge):
    voice, _photo = casile_with_uncertain_photo(engine, settings, judge)
    key = flag_photo_reuse(engine, voice.id)
    asyncio.run(IncidentService(settings, engine, judge).group(voice.id))  # no-op grouping
    with Session(engine) as session:
        assert collect(session).barangays["Casile"].incidents[0].flagged_report_ids == [voice.id]
    assert "1 trust signal to check" in client.get("/review").text

    response = act(
        client, voice.id, "signal", signal=key, verdict="genuine", note="same family, two phones"
    )
    assert response.status_code == 303
    (action,) = rows(engine, OfficerAction)
    assert action.detail == {"key": key, "code": "photo_reused", "verdict": "genuine"}
    with Session(engine) as session:
        assert collect(session).barangays["Casile"].incidents[0].flagged_report_ids == []
        assert session.get(Incident, voice.incident_id).latest_facts["flagged_report_count"] == 0
    assert "1 trust signal to check" not in client.get("/review").text
    # A stale key (the evidence changed) is refused.
    assert act(client, voice.id, "signal", signal="nope", verdict="problem").status_code == 400


def test_fallen_tree_confirmation_reaches_the_counted_hazards(client, engine, settings, judge):
    voice, _photo = casile_with_uncertain_photo(engine, settings, judge)
    with Session(engine) as session:
        session.add(
            PhotoAssessment(
                report_id=voice.id, media_id=1, llm_model="fake", judgment={"fallen_tree": True}
            )
        )
        session.commit()
    assert "fallen tree to confirm" in client.get("/review").text
    assert act(client, voice.id, "tree", verdict="confirmed").status_code == 303
    with Session(engine) as session:
        incident = collect(session).incident(voice.incident_id)
        assert incident.reports_listing("hazards", "fallen_tree") == [voice.id]
    assert "fallen tree to confirm" not in client.get("/review").text
    assert act(client, voice.id, "tree", verdict="absent").status_code == 303
    with Session(engine) as session:
        incident = collect(session).incident(voice.incident_id)
        assert incident.reports_listing("hazards", "fallen_tree") == []


def test_the_incident_title_follows_the_officers_tree_verdict(client, engine, settings):
    """The model said a fallen tree; the officer rules it out: the title stops saying so."""
    report_id = add_report(engine, barangay="Casile")
    with Session(engine) as session:
        assessment = session.exec(select(Assessment).where(Assessment.report_id == report_id)).one()
        assessment.facts = {**assessment.facts, "hazards": ["fallen_tree", "flood"]}
        session.add(assessment)
        session.commit()
    report = asyncio.run(IncidentService(settings, engine, FakeJudge()).group(report_id))

    def title():
        with Session(engine) as session:
            return session.get(Incident, report.incident_id).title

    assert "fallen tree" in title()
    assert act(client, report.id, "tree", verdict="absent").status_code == 303
    assert "fallen tree" not in title() and "flood" in title()
    assert act(client, report.id, "tree", verdict="confirmed").status_code == 303
    assert "flood" in title()  # a confirmed tree counts, after the hazards stated first
    assert "fallen tree" in client.get(f"/incidents/{report.incident_id}").text


def test_block_from_the_ui_has_the_same_effect_as_the_cli(
    client, engine, settings, judge, monkeypatch
):
    intake = IntakeService(settings, engine)
    by_ui = asyncio.run(intake.ingest_text(1001, "Baha sa Casile"))
    by_cli = asyncio.run(intake.ingest_text(1002, "Baha sa Malaban"))
    salt = settings.pseudonym_salt.get_secret_value()
    assert act(client, by_ui.id, "block", note="spam").status_code == 303
    monkeypatch.setattr(trust, "get_settings", lambda: settings)
    assert trust.main(["block", pseudonymize(salt, 1002)[:12]]) == 0
    with Session(engine) as session:
        reporters = {r.id: r for r in session.exec(select(Reporter)).all()}
    assert reporters[by_ui.reporter_id].blocked and reporters[by_cli.reporter_id].blocked
    assert intake.admit(1001) == intake.admit(1002) == Admission.BLOCKED
    held = [asyncio.run(intake.ingest_text(user, "ulit")) for user in (1001, 1002)]
    assert [(r.status, r.hold_reason) for r in held] == [("held", "reporter blocked")] * 2
    (action,) = rows(engine, OfficerAction)
    assert (action.kind, action.reporter_id, action.note) == (
        "officer_blocked",
        by_ui.reporter_id,
        "spam",
    )
    assert act(client, by_ui.id, "unblock").status_code == 303
    assert intake.admit(1001) == Admission.OK


# --- Rejected posts, audit, live updates and pages ------------------------------------------


def test_cross_origin_post_is_refused(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    response = client.post(
        f"/reports/{photo.id}/review/keep",
        data={"officer": "JDC"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert rows(engine, OfficerAction) == []
    assert report(engine, photo.id).grouping_needs_officer is True


@pytest.mark.parametrize("back", ["/review", "incident"])
def test_missing_officer_is_a_400_with_the_form_preserved(client, engine, settings, judge, back):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    back = f"/incidents/{photo.incident_id}" if back == "incident" else back
    response = client.post(
        f"/reports/{photo.id}/review/merge",
        data={
            "officer": "  ",
            "note": "kept <b>note</b>",
            "incident_id": voice.incident_id,
            "next": back,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    html = response.text
    assert "Write your initials" in html and 'role="alert"' in html
    assert "kept &lt;b&gt;note&lt;/b&gt;</textarea>" in html  # typed text kept, escaped
    assert f'<option value="{voice.incident_id}" selected>' in html
    assert 'aria-invalid="true"' in html
    assert rows(engine, OfficerAction) == []
    too_long = act(client, photo.id, "keep", officer="x" * 41)
    assert too_long.status_code == 400 and "at most 40 characters" in too_long.text


def test_officer_actions_are_append_only(client, engine, settings, judge):
    _voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    act(client, photo.id, "keep")
    with sqlite3.connect(settings.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE officeraction SET officer = 'X'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM officeraction")


def test_live_topics_change_after_an_officer_action(client, engine, settings, judge):
    _voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    connection = sqlite3.connect(settings.db_path, isolation_level=None)
    before = read_tokens(connection)
    act(client, photo.id, "keep")
    after = read_tokens(connection)
    connection.close()
    assert before["incidents"] != after["incidents"] and before["reports"] != after["reports"]


def test_review_queue_shows_the_judges_comparisons_and_is_linked(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    page = client.get("/review").text
    assert '<a href="/review" aria-current="page">Review</a>' in page
    assert 'id="live-review" data-live="reports incidents"' in page
    assert "grouping uncertain" in page and UNSURE["reason"] in page
    assert ">70%<" in page and "fake-pair-judge" in page
    assert f"incident #{voice.incident_id}" in page
    home = client.get("/").text
    assert 'href="/review"' in home and 'data-kpi="review_total">1<' in home
    incident = client.get(f"/incidents/{photo.incident_id}").text
    assert 'id="review"' in incident and "Officer review" in incident


def test_incident_history_shows_agent_and_officer_entries_labelled(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    act(
        client,
        photo.id,
        "merge",
        incident_id=voice.incident_id,
        note="same purok",
        next=f"/incidents/{photo.incident_id}",
    )
    page = client.get(f"/incidents/{voice.incident_id}").text
    assert "what officers changed" in page
    assert '<span class="chip info">Agent</span>' in page
    assert '<span class="chip ok">Officer JDC</span>' in page
    assert "merged by the officer" in page and "“same purok”" in page
    assert "Officer actions for report" in page  # every report keeps its override forms
    retired = client.get(f"/incidents/{photo.incident_id}").text
    assert "merged by the officer" in retired  # the old incident's history shows the move


def test_merge_from_an_incident_page_follows_the_report(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    response = act(
        client,
        photo.id,
        "merge",
        incident_id=voice.incident_id,
        next=f"/incidents/{photo.incident_id}",
    )
    assert response.headers["location"] == f"/incidents/{voice.incident_id}#report-{photo.id}"
    unsafe = act(client, photo.id, "keep", next="//evil.example/x")
    assert unsafe.headers["location"].startswith("/review?")

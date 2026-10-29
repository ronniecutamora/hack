"""Counting rule, RDANA field mapping and summary validation, with fake LLMs only."""

import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.counting import collect
from app.db import init_db, make_engine
from app.drafts import DraftService
from app.models import Assessment, DraftEvent, Incident, ModelCall, Report, Reporter
from app.rdana import EventInput
from app.rdana_form import SECTIONS, all_fields
from app.rdana_summary import (
    SUMMARY_SCHEMA,
    fallback_summary,
    summary_input,
    validate_summary,
)
from tests.test_assessment import facts

AT = datetime(2026, 10, 28, 4)  # naive UTC; 12:00 in Manila


class SummaryLLM:
    """Replies in order (dict or exception); records every request like a real client."""

    model = "fake-summary-llm"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def chat_json(self, **kwargs):
        assert kwargs["schema"] == SUMMARY_SCHEMA
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(json.loads(kwargs["user_text"]))
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def aclose(self):
        pass


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def add_incident(engine, barangay="Malaban", status="open"):
    with Session(engine) as session:
        incident = Incident(
            barangay=barangay, title=f"{barangay} flood", first_seen=AT, last_seen=AT, status=status
        )
        session.add(incident)
        session.commit()
        return incident.id


def add_report(
    engine,
    *,
    incident=None,
    reporter="a",
    barangay="Malaban",
    hours=0,
    point=(14.3511, 121.0900),
    signals=None,
    status="assessed",
    text="Raw report text with Juan Dela Cruz",
    transcript=None,
    **fields,
):
    with Session(engine) as session:
        pseudonym = f"pseudonym-{reporter}"
        row = session.exec(select(Reporter).where(Reporter.pseudonym == pseudonym)).first()
        if row is None:
            row = Reporter(pseudonym=pseudonym)
            session.add(row)
            session.flush()
        report = Report(
            reporter_id=row.id,
            incident_id=incident,
            channel="demo",
            barangay=barangay,
            received_at=AT + timedelta(hours=hours),
            text=text,
            transcript=transcript,
            status=status,
            lat=point[0] if point else None,
            lon=point[1] if point else None,
        )
        session.add(report)
        session.flush()
        session.add(
            Assessment(
                report_id=report.id, llm_model="fake", facts=facts(**fields), signals=signals
            )
        )
        session.commit()
        return report.id


def evidence(engine, barangay="Malaban"):
    with Session(engine) as session:
        return collect(session).barangays[barangay]


# --- Counting rule -----------------------------------------------------------------------


def test_correction_wins_within_an_incident_and_incidents_are_summed(engine):
    shared = add_incident(engine)
    add_report(engine, incident=shared, reporter="a", houses_partially_damaged=3)
    add_report(engine, incident=shared, reporter="b", hours=1, houses_partially_damaged=5)
    corrected = add_report(
        engine,
        incident=shared,
        reporter="a",
        hours=2,
        houses_partially_damaged=4,
        is_correction=True,
    )
    single = add_incident(engine)
    lone = add_report(engine, incident=single, reporter="c", hours=3, houses_partially_damaged=2)

    first = evidence(engine).count("houses_partially_damaged")
    by_incident = {c.incident.incident_id: c for c in first.contributions}
    assert by_incident[shared].value == 4
    assert by_incident[shared].rule == "latest correction"
    assert by_incident[shared].source_report_id == corrected
    assert by_incident[single].value == 2 and by_incident[single].source_report_id == lone
    assert first.value == 6
    # Only reporter a stated the corrected 4, and only c stated the 2: both are single-source.
    assert first.single_source_value == 6
    assert first.flagged_value == 0


def test_figures_after_the_latest_correction_can_raise_it_and_earlier_ones_cannot(engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, reporter="a", houses_totally_damaged=9)
    add_report(
        engine,
        incident=incident,
        reporter="b",
        hours=1,
        houses_totally_damaged=2,
        is_correction=True,
    )
    add_report(engine, incident=incident, reporter="a", hours=2, houses_totally_damaged=1)
    # A correction about something else does not reset this count.
    add_report(engine, incident=incident, reporter="b", hours=3, is_correction=True, dead=0)
    count = evidence(engine).count("houses_totally_damaged")
    assert count.value == 2 and count.contributions[0].rule == "latest correction"

    add_report(engine, incident=incident, reporter="c", hours=4, houses_totally_damaged=7)
    count = evidence(engine).count("houses_totally_damaged")
    assert count.value == 7
    assert count.contributions[0].rule == "maximum since the latest correction"
    assert evidence(engine).count("dead").value == 0  # a stated zero is evidence, kept as 0


def test_without_a_correction_the_maximum_is_used_never_the_sum(engine):
    incident = add_incident(engine)
    for hours, value in enumerate((3, 5, 4)):
        add_report(
            engine, incident=incident, reporter=str(hours), hours=hours, families_evacuated=value
        )
    count = evidence(engine).count("families_evacuated")
    assert count.value == 5 and count.contributions[0].rule == "maximum"
    assert evidence(engine).count("persons_evacuated") is None  # never stated: no count


def test_ungrouped_reports_are_pending_and_unplaced_or_unassessed_reports_are_kept_apart(engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, houses_partially_damaged=4)
    loose = add_report(engine, reporter="b", hours=1, houses_partially_damaged=1)
    pending = add_report(engine, hours=2, status="held", houses_partially_damaged=50)
    nowhere = add_incident(engine, barangay=None)
    add_report(engine, incident=nowhere, barangay=None, houses_partially_damaged=70)

    with Session(engine) as session:
        city = collect(session)
    malaban = city.barangays["Malaban"]
    count = malaban.count("houses_partially_damaged")
    assert count.value == 4  # the ungrouped report may repeat the incident: never added
    assert (count.pending_value, count.pending_report_ids) == (1, [loose])
    assert [i.key for i in malaban.incidents] == [f"incident {incident}", f"report {loose}"]
    assert malaban.unassessed_report_ids == [pending]
    assert [i.incident_id for i in city.unplaced] == [nowhere]


def test_flagged_share_counts_only_values_resting_on_flagged_reports(engine):
    flag = [{"code": "photo_reused", "severity": "high", "detail": "match"}]
    info = [{"code": "photo_resent", "severity": "info", "detail": "same reporter"}]
    tie = add_incident(engine)
    add_report(engine, incident=tie, reporter="a", signals=flag, houses_partially_damaged=3)
    clean = add_report(engine, incident=tie, reporter="b", houses_partially_damaged=3)
    flagged_only = add_incident(engine)
    add_report(engine, incident=flagged_only, signals=flag, houses_partially_damaged=2)
    add_report(engine, incident=flagged_only, signals=info, houses_partially_damaged=1)

    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 5
    assert count.contributions[0].source_report_id == clean  # an unflagged report agrees
    assert count.flagged_value == 2


# --- The form as data --------------------------------------------------------------------


def test_form_keeps_official_order_numbering_and_sic_typos():
    assert [s.number for s in SECTIONS] == [*map(str, range(1, 21)), ""]
    printed = {f.key: f.number for f in all_fields() if f.sic}
    assert printed == {
        "5.4": "5.3",
        "13.3": "14.3",
        "13.4": "14.4",
        "14.5": "15.5",
        "16.3": "17.3",
        "16.4": "17.4",
        "17.5": "117.5",
        "20.1": "21.1",
    }
    keys = [f.key for f in all_fields()]
    assert len(keys) == len(set(keys))
    assert {f.key for f in all_fields() if f.required} == {
        "1.2.1",
        "1.2.2",
        "1.2.3",
        "1.2.4",
        "1.2.7",
    }


# --- Field mapping -----------------------------------------------------------------------

EVIDENCE_TEXT = "Summary from the reports."


def cite_all(payload):
    """A well-behaved model: no numbers, cites every report it was given."""
    ids = [r["report_id"] for incident in payload["incidents"] for r in incident["reports"]]
    return {"summary": EVIDENCE_TEXT, "cited_report_ids": ids}


async def generate(engine, settings, *, event=None, llm=None, barangay="Malaban"):
    llm = llm or SummaryLLM(cite_all)
    service = DraftService(settings, engine, llm)
    return await service.generate_rdana(barangay, event), llm


def fields(draft):
    return {f["key"]: f for s in draft.content["sections"] for f in s["fields"]}


async def test_every_evidence_rule_fills_its_official_field(engine, settings):
    flood = add_incident(engine)
    a = add_report(
        engine,
        incident=flood,
        reporter="a",
        point=(14.3510, 121.0900),
        hazards=["flood"],
        flood_depth="knee",
        place_detail="Purok 3 malapit sa bahay ni Aling Nena",
        families_affected=12,
        families_evacuated=5,
        persons_evacuated=21,
        missing=1,
        injured=2,
        dead=0,
        houses_totally_damaged=1,
        houses_partially_damaged=4,
        infrastructure=["road", "power"],
        needs=["rescue", "transport", "food", "water", "shelter", "medicine"],
    )
    b = add_report(
        engine,
        incident=flood,
        reporter="b",
        hours=5,
        point=(14.3512, 121.0902),
        hazards=["flood", "strong_wind"],
        flood_depth="waist",
        needs=["clearing"],
        fish_cages_or_pens_damaged=3,
        crop="rice",
        crop_area_hectares=1.5,
    )
    draft, _llm = await generate(engine, settings)
    f = fields(draft)
    value = {key: field["value"] for key, field in f.items()}
    assert value["1.1.2"] == "Flood, Strong wind"
    assert value["1.1.3"] == "2026-10-28 12:00"  # earliest report, Philippine time
    assert value["1.2.1"] == "Region IV-A (CALABARZON)"
    assert value["1.2.2"] == "Laguna" and value["1.2.3"] == "City of Biñan"
    assert value["1.2.4"] == "Malaban"
    assert value["1.2.5"] == "Purok 3 malapit sa bahay [redacted]"  # no person's name
    assert value["1.2.6"] == "14.35110, 121.09010"
    assert value["1.2.7"] is not None
    assert value["1.4"] == EVIDENCE_TEXT and f["1.4"]["source"] == "agent"
    assert f["1.4"]["evidence"][0]["report_ids"] == [a, b]
    assert value["2.1"] == "12" and value["2.3"] == "5" and value["2.4"] == "21"
    assert value["2.9"] == "1" and value["2.12"] == "2" and value["2.15"] == "0"
    for split in ("2.7", "2.8", "2.10", "2.11", "2.13", "2.14", "2.2", "2.5", "2.6"):
        assert value[split] is None
    assert value["3.3"] == "Yes" and f["3.3"]["evidence"][0]["report_ids"] == [a]
    assert value["3.4"] == "Transport, Debris clearing"
    assert value["4.2"] == "Power outage or damage reported"
    assert value["8.1"] == "Yes" and value["8.2"] == "Water"
    assert value["10.1"] == "1" and value["10.2"] == "4"
    assert value["10.3"].startswith("Shelter assistance requested")
    assert value["11.5"] == "Food requested" and value["12.5"] == "Water requested"
    assert value["14.5"] == "Medicine requested" and f["14.5"]["number"] == "15.5"
    assert f["14.5"]["sic"] is True
    assert value["18.1"] == "fish cages or pens damaged: 3; crops damaged: rice (1.5 ha reported)"
    assert value["18.2"] is None
    for officer in ("1.1.1", "1.3.1", "20.1", "20.2", "submitted.name"):
        assert value[officer] is None
    # Counts carry their evidence; depth goes to 1.4 support data, not a numbered field.
    assert f["10.2"]["evidence"][0]["report_ids"] == [a]
    # Two reporters in the incident, but only one of them stated the 4 houses.
    assert f["10.2"]["note"] == "4 of 4 stated by only one reporter; 0 of 4 from flagged reports."
    assert draft.content["flood_depths"][0]["latest"] == "waist"
    assert draft.content["flood_depths"][0]["max"] == "waist"
    assert draft.content["report_ids"] == [a, b]


async def test_officer_values_override_and_missing_evidence_stays_empty(engine, settings):
    incident = add_incident(engine)
    add_report(engine, incident=incident, hazards=["flood"], needs=[])
    event = EventInput(name="Typhoon response", type="Typhoon", at=AT - timedelta(days=1))
    draft, _ = await generate(engine, settings, event=event)
    f = fields(draft)
    assert f["1.1.1"]["value"] == "Typhoon response" and f["1.1.1"]["source"] == "officer"
    assert f["1.1.2"]["value"] == "Typhoon" and f["1.1.2"]["note"] == "Reports mention: Flood."
    assert f["1.1.3"]["value"] == "2026-10-27 12:00" and f["1.1.3"]["source"] == "officer"
    # No rescue, road or need evidence: never "No", only empty.
    for key in ("3.3", "3.4", "4.2", "8.1", "8.2", "10.1", "10.3", "11.5", "18.1"):
        assert f[key]["value"] is None, key
    assert "No" not in [field["value"] for field in f.values()]


async def test_water_srr_needs_a_rescue_request_in_a_flood_incident(engine, settings):
    wind = add_incident(engine)
    add_report(engine, incident=wind, hazards=["strong_wind"], needs=["rescue"])
    flood = add_incident(engine)
    add_report(engine, incident=flood, reporter="b", hazards=["flood"])
    draft, _ = await generate(engine, settings)
    assert fields(draft)["8.1"]["value"] == "Yes"
    assert fields(draft)["8.2"]["value"] is None


# --- Summary validation ------------------------------------------------------------------


def payload_for(engine):
    ev = evidence(engine)
    return ev, summary_input(ev, {"houses_partially_damaged": 4})


def test_numbers_must_be_computed_counts_and_place_digits_only_quoted(engine):
    add_report(engine, houses_partially_damaged=4, place_detail="Purok 3", flood_depth="knee")
    _ev, payload = payload_for(engine)
    report_id = payload["incidents"][0]["reports"][0]["report_id"]

    def check(text, cited=(report_id,)):
        return validate_summary({"summary": text, "cited_report_ids": list(cited)}, payload)

    not_accepted = "Every number must be a computed count, next to what it counts. Not accepted: "
    assert check("Four houses were reported damaged in Purok 3.") == []
    houses = "(computed: houses damaged (totally and partially together) 4)."
    assert check("12 houses.") == [not_accepted + "12 " + houses]
    assert check("Seven houses.") == [not_accepted + "Seven " + houses]
    assert check(f"{report_id} houses.") != []  # a report id is not a count
    assert check("Flooding.", [999]) == ["cited_report_ids [999] are not in the input."]
    assert check("Flooding.", []) != []
    assert any("5 sentences" in e for e in check("A. B. C. D. E."))
    assert check("No one was hurt; 1,000 jobs.") != []  # 1,000 parsed as one number


def test_the_deterministic_summary_passes_its_own_validation(engine):
    incident = add_incident(engine)
    add_report(
        engine,
        incident=incident,
        hazards=["flood"],
        flood_depth="waist",
        place_detail="Purok 3",
        houses_partially_damaged=4,
        needs=["rescue"],
    )
    add_report(engine, incident=incident, reporter="b", hours=1, flood_depth="none")
    ev, payload = payload_for(engine)
    text = fallback_summary(ev, {"houses_partially_damaged": 4})
    assert "waist deep; the latest report says no flooding" in text
    reply = {"summary": text, "cited_report_ids": ev.report_ids}
    assert validate_summary(reply, payload) == []


async def test_invented_numbers_are_retried_once_then_the_fallback_is_stored(engine, settings):
    incident = add_incident(engine)
    report = add_report(engine, incident=incident, houses_partially_damaged=4)
    llm = SummaryLLM(
        {"summary": "About 40 houses were damaged.", "cited_report_ids": [report]},
        {"summary": "Some 40 houses were damaged.", "cited_report_ids": [report]},
    )
    draft, _ = await generate(engine, settings, llm=llm)
    assert len(llm.calls) == 2
    assert "40 (computed: houses damaged" in llm.calls[1]["user_text"]  # the retry sees why
    assert draft.summary_fallback is True and draft.summary_model is None
    assert [a["errors"] != [] for a in draft.summary_validation] == [True, True]
    assert fields(draft)["1.4"]["source"] == "rule"
    assert "houses partially damaged: 4" in fields(draft)["1.4"]["value"]
    reason = draft.content["summary"]["fallback_reason"]
    assert reason.startswith("Model reply rejected twice") and "40" in reason
    with Session(engine) as session:
        event = session.exec(select(DraftEvent)).one()
    assert event.detail["summary_fallback"] is True and event.detail["fallback_reason"] == reason


async def test_unknown_citation_is_rejected_and_a_valid_retry_is_accepted(engine, settings):
    report = add_report(engine, incident=add_incident(engine), houses_partially_damaged=4)
    llm = SummaryLLM(
        {"summary": "4 houses were reported damaged.", "cited_report_ids": [report, 999]},
        {"summary": "4 houses were reported damaged.", "cited_report_ids": [report]},
    )
    draft, _ = await generate(engine, settings, llm=llm)
    assert draft.summary_fallback is False and draft.summary_model == "fake-summary-llm"
    assert draft.summary_validation[0]["errors"] == ["cited_report_ids [999] are not in the input."]
    assert draft.summary_validation[1]["errors"] == []
    assert fields(draft)["1.4"]["evidence"][0]["report_ids"] == [report]


async def test_model_failure_falls_back_without_a_retry(engine, settings):
    add_report(engine, hazards=["flood"])
    llm = SummaryLLM(RuntimeError("provider down"))
    draft, _ = await generate(engine, settings, llm=llm)
    assert len(llm.calls) == 1 and draft.summary_fallback is True
    assert draft.content["summary"]["fallback_reason"] == (
        "Model call failed: RuntimeError: provider down"
    )


async def test_spend_cap_blocks_the_summary_call_and_records_why(engine, settings):
    add_report(engine, hazards=["flood"])
    with Session(engine) as session:
        session.add(
            ModelCall(
                model="x", latency_ms=1, cached=False, cost_estimate=settings.daily_spend_cap_usd
            )
        )
        session.commit()
    llm = SummaryLLM()
    draft, _ = await generate(engine, settings, llm=llm)
    assert llm.calls == []
    assert draft.summary_fallback is True and draft.summary_validation == []
    assert draft.content["summary"]["fallback_reason"].startswith("Spend cap reached")


async def test_the_model_input_has_no_reporter_raw_text_transcript_or_name(engine, settings):
    incident = add_incident(engine)
    add_report(
        engine,
        incident=incident,
        reporter="secret-reporter",
        text="Ako po si Juan Dela Cruz, rawtextmarker dito",
        transcript="transcript-only words from Pedro Penduko",
        place_detail="tapat ng bahay ni Aling Nena, Purok 3",
        summary_en="Juan Dela Cruz reports flooding.",
        hazards=["flood"],
        houses_partially_damaged=4,
    )
    llm = SummaryLLM(cite_all)
    await generate(engine, settings, llm=llm)
    request = llm.calls[0]
    sent = request["system"] + request["user_text"]
    secrets = ("Juan", "Dela Cruz", "Pedro", "Nena", "secret-reporter", "pseudonym", "rawtext")
    for secret in secrets:
        assert secret not in sent
    payload = json.loads(request["user_text"])

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value)) if value else set()
        return set()

    assert not keys(payload) & {"reporter_id", "text", "transcript", "summary_en", "pseudonym"}

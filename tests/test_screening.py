"""The not-a-report screen (app.screening): the free rule, the model verdict, the one predicate
that keeps screened-out messages out of every group and count, the officer's restore, the bot's
nudge and the trace. Every model call is a fake."""

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

from app import bot
from app.agent_trace import report_traces
from app.assessment import AssessmentService, screen_existing
from app.counting import collect
from app.dashboard import overview
from app.db import init_db, make_engine
from app.demo import SCENARIO_PATH
from app.demo.scenario import load_scenario
from app.incidents import IncidentService, observations
from app.intake import IntakeService
from app.main import create_app
from app.mapdata import map_features
from app.meta.base import ModelApiError
from app.models import (
    AgentDecision,
    Assessment,
    Incident,
    OfficerAction,
    Report,
    utcnow,
)
from app.review import ReviewError, ReviewService, queue
from app.screening import (
    Attachments,
    NudgeLimiter,
    Screening,
    kept_without_model,
    not_reports,
    photo_cues,
    rule_screen,
    screen_text,
    screen_with_model,
    states_report_facts,
)
from tests.test_assessment import FakeSam, facts
from tests.test_incidents import FakeJudge, add_report
from tests.test_photo import JUDGMENT
from tests.test_rdana import SummaryLLM, cite_all

MESSAGES = Path(__file__).resolve().parents[1] / "data/eval/screen_messages.json"
NOTHING = facts(flood_depth="none", summary_en="A greeting.")  # facts that state nothing
NO_CUES = {**JUDGMENT, "roof_damage": "none", "floodwater": False, "debris": False}


def verdict(answer, reason="only small talk"):
    return {"is_disaster_report": answer, "reason": reason}


class ScreenLLM:
    """Answers each step by its schema: facts, the screening verdict, or a photo judgment."""

    model = "fake-llm"

    def __init__(self, facts_reply=None, screen_reply=None, judgment=None, error=None):
        self.facts_reply = facts_reply or NOTHING
        self.screen_reply = screen_reply or verdict("no")
        self.judgment = judgment or JUDGMENT
        self.error = error
        self.calls = []

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs["schema_name"])
        if kwargs.get("image") is not None:
            return self.judgment
        if kwargs["schema_name"] == "DisasterReportScreen":
            if self.error:
                raise self.error
            return self.screen_reply
        return self.facts_reply


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    return engine


def assessor(settings, engine, llm):
    return AssessmentService(
        settings, engine, llm, FakeSam(), IncidentService(settings, engine, FakeJudge())
    )


def get(engine, model, row_id):
    with Session(engine) as session:
        return session.get(model, row_id)


def jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (800, 400), "gray").save(buffer, "JPEG")
    return buffer.getvalue()


def labelled(label):
    return [m for m in json.loads(MESSAGES.read_text("utf-8"))["messages"] if m["label"] == label]


# --- the free rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("test", "test"),
        ("Testing lang po", "test"),
        ("hello po", "greeting"),
        ("Good morning po!", "greeting"),
        ("salamat po", "greeting"),
        ("helloooo", "greeting"),
        ("hahahaha", "laughter"),
        ("HAHAHA 😂😂", "laughter"),
        ("xD", "laughter"),
        ("asdfghjkl", "keyboard_mash"),
        ("jsjsjsjs", "keyboard_mash"),
        ("ajsjsjsk dkdkdk", "keyboard_mash"),
        ("😂😂😂", "emoji_only"),
        ("👍", "emoji_only"),
        ("???", "punctuation_only"),
        ("   ", "empty"),
    ],
)
def test_the_rule_drops_only_messages_that_say_nothing(text, code):
    screening = rule_screen(text, Attachments())
    assert (screening.source, screening.verdict, screening.code) == ("rule", "no", code)


@pytest.mark.parametrize(
    "text",
    [
        "baha dito",
        "tulong po purok 3",
        "lubog n bhay nmin",
        "mali po, tatlo lang",
        "3",  # a reply that corrects a count
        "test 123",  # digits: the model decides
        "🆘",  # a distress emoji alone
        "okay na po",
        "help",
        "SOS",
        "huhu",
        "nmn",
        "brgy",
        "Bili na kayo ng longganisa",  # spam goes to the model, not the rule
    ],
)
def test_the_rule_never_drops_words_that_may_carry_meaning(text):
    assert rule_screen(text, Attachments()) is None


@pytest.mark.parametrize(
    "attachments",
    [Attachments(photos=1), Attachments(voice=True), Attachments(location=True)],
)
def test_a_photo_voice_note_or_pin_is_never_dropped_by_the_rule(attachments):
    assert rule_screen("hahaha", attachments) is None


def test_no_labelled_real_report_and_no_demo_report_is_dropped_by_the_rule():
    real = labelled("report")
    assert len(real) == 30
    assert [m["id"] for m in real if rule_screen(m["text"], Attachments())] == []
    scenario = load_scenario(SCENARIO_PATH)
    assert len(scenario) == 18
    assert [r.key for r in scenario if rule_screen(r.text, Attachments())] == []


# --- the model stage -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stated",
    [
        {"hazards": ["flood"]},
        {"flood_depth": "unknown"},  # flooding mentioned without a depth
        {"houses_partially_damaged": 0},
        {"needs": ["rescue"]},
        {"livelihood_damage": ["fish_pens_or_cages"]},
        {"crop": "rice"},
        {"barangay_mentioned": "Malaban"},
        {"place_detail": "Purok 3"},
        {"is_correction": True},
    ],
)
def test_any_stated_fact_keeps_the_message_without_a_screening_call(stated):
    assert states_report_facts({**NOTHING, **stated})
    kept = kept_without_model({**NOTHING, **stated}, Attachments())
    assert (kept.verdict, kept.code) == ("yes", "facts")


def test_photos_are_only_supporting_evidence():
    assert photo_cues([JUDGMENT]) is True
    assert photo_cues([NO_CUES]) is False
    assert photo_cues([NO_CUES, None]) is None  # one photo not judged: unknown
    assert kept_without_model(NOTHING, Attachments(photos=1, photo_cues=True)).code == "photo"
    assert kept_without_model(NOTHING, Attachments(photos=1, photo_cues=None)).code == "photo"
    assert kept_without_model(NOTHING, Attachments(photos=1, photo_cues=False)) is None
    assert kept_without_model(NOTHING, Attachments(), correction=True).code == "correction"
    assert kept_without_model(NOTHING, Attachments()) is None


async def test_the_model_verdict_and_its_failures_keep_the_message():
    llm = ScreenLLM(screen_reply=verdict("unsure", "  could be a  report "))
    unsure = await screen_with_model(llm, "Ako po si Lando, nasa labas", Attachments())
    assert (unsure.source, unsure.verdict, unsure.reason, unsure.model) == (
        "model",
        "unsure",
        "could be a report",
        "fake-llm",
    )
    assert not unsure.filtered
    bad = await screen_with_model(
        ScreenLLM(screen_reply={"is_disaster_report": "maybe"}), "x", Attachments()
    )
    failed = await screen_with_model(
        ScreenLLM(error=ModelApiError("Muse Spark", "busy", status=503)), "x", Attachments()
    )
    assert bad.verdict == failed.verdict == "error"
    assert not bad.filtered and not failed.filtered


async def test_screen_text_runs_the_pipeline_order():
    llm = ScreenLLM()
    assert (await screen_text(llm, "hahaha")).code == "laughter" and llm.calls == []
    assert (await screen_text(llm, "Bili na kayo")).filtered
    assert llm.calls == ["DamageReportFacts", "DisasterReportScreen"]
    reported = ScreenLLM(facts(hazards=["flood"]))
    assert (await screen_text(reported, "baha dito")).code == "facts"
    assert reported.calls == ["DamageReportFacts"]


# --- the pipeline ----------------------------------------------------------------------------


async def test_a_rule_dropped_message_makes_no_model_call(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "hahaha")
    assert report.status == "not_a_report"
    assert report.screening == {
        "source": "rule",
        "verdict": "no",
        "code": "laughter",
        "reason": "only laughter",
        "model": None,
    }
    llm = ScreenLLM()
    out = await assessor(settings, engine, llm).assess(report.id)  # e.g. `--all`
    assert out.status == "not_a_report" and llm.calls == []


async def test_a_model_no_screens_the_message_out_of_everything(settings, engine):
    intake = IntakeService(settings, engine)
    real = await intake.ingest_text(6, "baha sa Malaban")
    await assessor(settings, engine, ScreenLLM(facts(hazards=["flood"]))).assess(real.id)
    report = await intake.ingest_text(7, "Bili na kayo ng longganisa sa Malaban, PM lang")
    llm = ScreenLLM(facts(flood_depth="none", barangay_mentioned=None))
    out = await assessor(settings, engine, llm).assess(report.id)
    assert llm.calls == ["DamageReportFacts", "DisasterReportScreen"]
    assert out.status == "not_a_report" and out.incident_id is None
    assert out.barangay == "Malaban"  # placed, yet never counted or pending
    assert out.screening["source"] == "model" and out.screening["verdict"] == "no"
    with Session(engine) as session:
        assert set(observations(session)) == {real.id}
        city = collect(session)
        assert [i.report_ids for i in city.incidents()] == [[real.id]]
        assert city.barangays["Malaban"].unassessed_report_ids == []
        features, _ = map_features(session)
        assert [f["properties"]["report_count"] for f in features["features"]] == [1]
        assert len(session.exec(select(Incident)).all()) == 1
        assert [r.id for r in not_reports(session)] == [report.id]
        assert queue(session) == []


async def test_unsure_stays_a_report_and_is_flagged_for_the_officer(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "nasa labas po kami")
    llm = ScreenLLM(screen_reply=verdict("unsure", "may describe people outside"))
    out = await assessor(settings, engine, llm).assess(report.id)
    assert out.status == "assessed" and out.incident_id is not None
    signals = get_assessment(engine, report.id).signals
    assert [s["code"] for s in signals] == ["maybe_not_a_report"]
    with Session(engine) as session:
        card = queue(session)[0]
        assert card.report.id == report.id and "1 trust signal to check" in card.reasons


def get_assessment(engine, report_id):
    with Session(engine) as session:
        return session.exec(select(Assessment).where(Assessment.report_id == report_id)).one()


async def test_a_report_with_facts_never_pays_for_screening(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "baha dito sa Malaban")
    llm = ScreenLLM(facts(hazards=["flood"], barangay_mentioned="Malaban"))
    out = await assessor(settings, engine, llm).assess(report.id)
    assert out.status == "assessed" and llm.calls == ["DamageReportFacts"]
    assert out.screening["code"] == "facts"


async def test_a_photo_needs_both_a_no_and_a_photo_without_damage(settings, engine):
    intake = IntakeService(settings, engine)
    damaged = await intake.ingest_photo(7, jpeg(), caption="hahaha")  # the rule never drops it
    llm = ScreenLLM()
    assert (await assessor(settings, engine, llm).assess(damaged.id)).status == "assessed"
    assert "DisasterReportScreen" not in llm.calls
    plain = await intake.ingest_photo(8, jpeg(), caption="hahaha")
    llm = ScreenLLM(judgment=NO_CUES)
    assert (await assessor(settings, engine, llm).assess(plain.id)).status == "not_a_report"
    assert llm.calls.count("DisasterReportScreen") == 1
    alone = await intake.ingest_photo(9, jpeg())  # no words: never screened
    assert (
        await assessor(settings, engine, ScreenLLM(judgment=NO_CUES)).assess(alone.id)
    ).status == ("assessed")


async def test_a_grouped_report_screened_out_leaves_its_incident(settings, engine):
    """A greeting assessed and grouped before the filter existed: the free backfill moves it."""
    report_id = add_report(engine, reporter="early-tester")
    with Session(engine) as session:
        row = session.get(Report, report_id)
        row.text = "Hello po, kumusta?"
        session.add(row)
        session.commit()
    first = await IncidentService(settings, engine, FakeJudge()).group(report_id)
    assert first.incident_id is not None
    report = first
    assert screen_existing(engine) == [report.id]
    assert screen_existing(engine) == []  # idempotent
    with Session(engine) as session:
        incident = session.get(Incident, first.incident_id)
        assert not incident.active and incident.latest_facts["report_count"] == 0
        decision = session.exec(
            select(AgentDecision).where(AgentDecision.kind == "screened_out")
        ).one()
        assert decision.incident_id == first.incident_id and "greeting" in decision.reason


async def test_a_pin_or_photo_joining_a_screened_out_message_reopens_it(settings, engine):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_text(7, "test")
    joined = await intake.ingest_location(7, 14.3383, 121.0843)
    assert joined.id == report.id and joined.status == "new"
    llm = ScreenLLM(screen_reply=verdict("yes", "a test, but with a pin"))
    assert (await assessor(settings, engine, llm).assess(report.id)).status == "assessed"


# --- restore ---------------------------------------------------------------------------------


async def test_restore_is_audited_runs_the_pipeline_and_sticks(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "test")
    llm = ScreenLLM()
    service = ReviewService(
        engine, IncidentService(settings, engine, FakeJudge()), assessor(settings, engine, llm)
    )
    with pytest.raises(ReviewError, match="initials"):
        await service.restore(report.id, officer=" ")
    incident = await service.restore(report.id, officer="JDC", note="a resident testing first")
    restored = get(engine, Report, report.id)
    assert restored.officer_restored and restored.status == "assessed"
    assert restored.screening["source"] == "officer"
    assert incident == restored.incident_id is not None
    assert llm.calls == ["DamageReportFacts"]  # the officer's word: no screening call
    with Session(engine) as session:
        action = session.exec(select(OfficerAction)).one()
        assert (action.kind, action.officer, action.note) == (
            "officer_restored",
            "JDC",
            "a resident testing first",
        )
        assert action.detail == {"source": "rule", "code": "test", "model": None}
        assert report.id in observations(session)
    again = await assessor(settings, engine, llm).assess(report.id)  # re-assessment obeys
    assert again.status == "assessed"
    with pytest.raises(ReviewError, match="nothing to restore"):
        await service.restore(report.id, officer="JDC")


async def test_restore_without_an_assessor_waits_as_new(settings, engine):
    report = await IntakeService(settings, engine).ingest_text(7, "hi")
    service = ReviewService(engine, IncidentService(settings, engine, FakeJudge()))
    assert await service.restore(report.id, officer="JDC") is None
    assert get(engine, Report, report.id).status == "new"


def test_restore_is_a_review_route_with_the_same_guards(settings):
    app = create_app(settings, summary_llm=SummaryLLM(cite_all), pair_judge=FakeJudge())
    with TestClient(app, base_url="http://localhost") as client:
        engine = client.app.state.engine
        report = asyncio.run(IntakeService(settings, engine).ingest_text(7, "hahaha"))
        url = f"/reports/{report.id}/review/restore"
        foreign = client.post(
            url, data={"officer": "JDC"}, headers={"origin": "https://evil.example"}
        )
        assert foreign.status_code == 403
        missing = client.post(url, data={"officer": ""}, follow_redirects=False)
        assert missing.status_code == 400
        done = client.post(url, data={"officer": "JDC", "next": "/review"}, follow_redirects=False)
        assert done.status_code == 303 and "done=restore" in done.headers["location"]
        assert get(engine, Report, report.id).status == "new"  # no bot here: it waits
        page = client.get(done.headers["location"])
        assert "restored as a report" in page.text


# --- the bot ---------------------------------------------------------------------------------


def make_update(**message_fields):
    message = SimpleNamespace(reply_text=AsyncMock(), **message_fields)
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))


def bot_context(settings, engine):
    created = []

    def create_task(coroutine, name=None):
        created.append(name)
        coroutine.close()

    data = {
        "intake": IntakeService(settings, engine),
        "settings": settings,
        "assessor": object(),
        "nudges": NudgeLimiter(),
    }
    return SimpleNamespace(
        application=SimpleNamespace(bot_data=data, create_task=create_task)
    ), created


async def test_the_bot_nudges_instead_of_receiving_and_queues_nothing(settings, engine):
    context, created = bot_context(settings, engine)
    first = make_update(text="test")
    await bot.on_text(first, context)
    first.effective_message.reply_text.assert_awaited_once_with(bot.NOT_A_REPORT_NUDGE)
    assert created == []  # never queued for a model
    again = make_update(text="hello?")
    await bot.on_text(again, context)
    again.effective_message.reply_text.assert_not_awaited()  # nudged recently: silent
    sticker = make_update(sticker=object())
    await bot.on_sticker(sticker, context)
    sticker.effective_message.reply_text.assert_not_awaited()
    real = make_update(text="baha dito")
    await bot.on_text(real, context)
    real.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)
    assert len(created) == 1


async def test_no_nudge_for_a_reporter_who_just_sent_a_real_report(settings, engine):
    context, created = bot_context(settings, engine)
    await bot.on_text(make_update(text="baha sa Casile"), context)
    thanks = make_update(text="salamat po")  # after the bot's summary
    await bot.on_text(thanks, context)
    thanks.effective_message.reply_text.assert_not_awaited()
    assert len(created) == 1  # only the real report was queued


async def test_a_model_screened_message_gets_the_nudge_not_a_summary(settings, engine):
    intake = IntakeService(settings, engine)
    report = await intake.ingest_text(42, "Bili na kayo ng longganisa")
    service = assessor(settings, engine, ScreenLLM())
    acks = bot.Acknowledgements(quiet_s=0)
    message = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace()))
    generation = acks.touch(report.id, message)
    application = SimpleNamespace(
        bot_data={"intake": intake, "acks": acks, "nudges": NudgeLimiter()}
    )
    await bot._assess_and_acknowledge(application, service, report.id, generation)
    message.reply_text.assert_awaited_once_with(bot.NOT_A_REPORT_NUDGE)


def test_the_nudge_limiter_is_per_reporter_and_bounded():
    now = [0.0]
    limiter = NudgeLimiter(interval_s=60, limit=2, clock=lambda: now[0])
    assert limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")
    now[0] = 61
    assert limiter.allow("a")
    limiter.allow("c")
    assert len(limiter._last) == 2


def test_the_nudge_is_kind_bilingual_and_asks_for_what_makes_a_report():
    text = bot.NOT_A_REPORT_NUDGE
    assert "Salamat" in text and "Thanks" in text and "911" in text
    assert all(word in text for word in ("barangay", "location pin", "larawan", "voice note"))
    assert "reject" not in text.lower()


# --- the trace, the overview and the recorded eval --------------------------------------------


async def test_the_trace_says_why_a_message_was_screened(settings, engine):
    intake = IntakeService(settings, engine)
    ruled = await intake.ingest_text(7, "asdfghjkl")
    modelled = await intake.ingest_text(8, "Bili na kayo")
    await assessor(settings, engine, ScreenLLM()).assess(modelled.id)
    with Session(engine) as session:
        reports = [session.get(Report, ruled.id), session.get(Report, modelled.id)]
        traces = report_traces(session, reports)
    (rule_step,) = traces[ruled.id].steps
    assert (rule_step.label, rule_step.actor) == ("Screen the message", "rule")
    assert rule_step.output == "not a report (keyboard mashing)"
    screen = next(s for s in traces[modelled.id].steps if s.label == "Screen the message")
    assert screen.actor == "model" and screen.output == "not a report (only small talk)"
    assert screen.model == "fake-llm"


async def test_the_overview_does_not_count_screened_out_messages(settings, engine):
    intake = IntakeService(settings, engine)
    await intake.ingest_text(7, "hahaha")
    kept = await intake.ingest_text(8, "baha dito")
    drafts = SimpleNamespace(
        overview=lambda: (None, None, []), nol_statuses=lambda city, history: []
    )
    with Session(engine) as session:
        data = overview(session, drafts, utcnow())
    assert (data["reports_total"], data["reports_recent"], data["not_reports"]) == (1, 1, 1)
    assert [r.id for r, _ in data["latest"]] == [kept.id]


def test_screening_round_trips_and_ignores_garbage():
    screening = Screening("model", "no", "no", "spam", "muse-spark-1.3")
    assert Screening.from_dict(screening.as_dict()) == screening
    assert Screening.from_dict(None) is None and Screening.from_dict({"source": "rule"}) is None

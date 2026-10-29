"""The bot tells the reporter what it understood, once, from the stored facts, and a reply to
that summary is a correction of the report (no model call is real)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session

from app import bot
from app.assessment import AssessmentService
from app.db import init_db, make_engine
from app.incidents import IncidentService
from app.models import AgentDecision, Assessment, Report
from app.reporter_summary import FOOTER, HEADER, PLACE_UNKNOWN, Acknowledgements, summary_text
from tests.test_abuse_guards import block, context, rows
from tests.test_assessment import FakeLLM, FakeSam, facts, jpeg
from tests.test_bot import Downloadable, make_update
from tests.test_geo import CENTRES
from tests.test_incidents import FakeJudge

QUIET_S = 0.2


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def summary_context(settings, engine, reply=None, judge=None):
    ctx, tasks, _ = context(settings, engine)
    llm = FakeLLM(reply or facts())
    ctx.application.bot_data["assessor"] = AssessmentService(
        settings, engine, llm, FakeSam(), IncidentService(settings, engine, judge or FakeJudge())
    )
    ctx.application.bot_data["acks"] = Acknowledgements(quiet_s=QUIET_S)
    return ctx, tasks, llm


def message(**fields):
    """A Telegram message in chat 42 whose reply returns a sent message with an id."""
    update = make_update(chat_id=42, **fields)
    sent = SimpleNamespace(chat_id=42, message_id=900 + len(fields))
    update.effective_message.reply_text = AsyncMock(return_value=sent)
    return update


def replies(*updates):
    return [c.args[0] for u in updates for c in u.effective_message.reply_text.await_args_list]


def summaries(*updates):
    return [text for text in replies(*updates) if text.startswith(HEADER)]


async def settle(tasks):
    while not all(t.done() for t in tasks):
        await asyncio.gather(*tasks)


def test_summary_text_is_bilingual_and_built_from_the_facts():
    report = Report(id=1, reporter_id=1, channel="telegram", barangay="Casile", status="assessed")
    stated = facts(
        flood_depth="knee",
        houses_partially_damaged=3,
        families_evacuated=2,
        needs=["food", "water"],
        place_detail="Purok 2",
    )
    assert summary_text(report, stated) == "\n".join(
        [
            HEADER,
            "Lugar / Place: Purok 2, Casile",
            "Baha / Flood: hanggang tuhod / knee-deep",
            "Bilang / Counts: 3 bahay na may sira, 2 pamilyang lumikas / "
            "3 houses partly damaged, 2 families evacuated",
            "Kailangan / Needs: pagkain, tubig / food, water",
            FOOTER,
        ]
    )
    report.barangay = None
    text = summary_text(report, facts(is_correction=True))
    assert PLACE_UNKNOWN in text and "send a location pin" in text
    assert "Recorded as a correction" in text and "none stated" in text


@pytest.mark.parametrize("status", ["held", "assessment_failed", "new", "assessing"])
def test_no_facts_go_back_for_a_report_that_is_not_assessed(status):
    report = Report(id=1, reporter_id=1, channel="telegram", barangay="Casile", status=status)
    assert summary_text(report, facts(houses_flooded=4)) is None


async def test_one_summary_for_a_photo_its_pin_and_its_caption(settings, engine):
    ctx, tasks, _ = summary_context(
        settings, engine, facts(flood_depth="waist", houses_flooded=4, needs=["shelter"])
    )
    photo = message(photo=[Downloadable(jpeg())], caption=None)
    lat, lon = CENTRES["Casile"]
    pin = message(location=SimpleNamespace(latitude=lat, longitude=lon))
    caption = message(text="Pinasok ng baha ang apat na bahay")
    await bot.on_photo(photo, ctx)
    await bot.on_location(pin, ctx)
    await bot.on_text(caption, ctx)
    await settle(tasks)
    (report,) = rows(engine, Report)
    assert (report.status, report.barangay, report.text) == (
        "assessed",
        "Casile",
        "Pinasok ng baha ang apat na bahay",
    )
    (text,) = summaries(photo, pin, caption)  # one, not three
    assert caption.effective_message.reply_text.await_args.args[0] == text  # the latest message
    assert "Lugar / Place: Casile" in text
    assert "hanggang baywang / waist-deep" in text and "4 houses flooded" in text
    assert "matutuluyan / shelter" in text and text.endswith(FOOTER)


async def test_no_summary_for_a_held_report(settings, engine):
    ctx, tasks, _ = summary_context(settings, engine)
    first = message(text="Baha sa Casile")
    await bot.on_text(first, ctx)
    await settle(tasks)
    assert len(summaries(first)) == 1
    block(engine, rows(engine, Report)[0].reporter_id)
    held = message(text="Baha ulit sa Casile")
    await bot.on_text(held, ctx)
    await settle(tasks)
    assert replies(held) == [bot.RECEIVED] and rows(engine, Report)[1].status == "held"


async def test_no_summary_for_a_failed_assessment(settings, engine):
    ctx, tasks, _ = summary_context(settings, engine)
    ctx.application.bot_data["assessor"].llm.error = RuntimeError("boom")
    update = message(text="Baha sa Casile")
    await bot.on_text(update, ctx)
    await settle(tasks)
    assert replies(update) == [bot.RECEIVED]


async def test_the_summary_never_repeats_a_name_or_phone_number(settings, engine):
    stated = facts(
        place_detail="bahay ni Aling Nena Cruz, tawagan si Mang Jose 09171234567, Purok 2",
        houses_partially_damaged=1,
    )
    ctx, tasks, _ = summary_context(settings, engine, stated)
    update = message(
        text="Ako si Nena Cruz, 0917 123 4567, sira ang bubong namin sa Purok 2 Casile"
    )
    await bot.on_text(update, ctx)
    await settle(tasks)
    (text,) = summaries(update)
    for leak in ("Nena", "Cruz", "Jose", "0917", "1234567", "123 4567"):
        assert leak not in text
    assert "Purok 2" in text and "Casile" in text


async def test_a_reply_to_the_summary_is_a_correction_of_that_report(settings, engine):
    """The live case, closed by the reporter: "dalawang bahay lang po" as a reply to the summary
    joins the report it answers, by rule, even though the judge was unsure."""
    judge = FakeJudge()
    ctx, tasks, llm = summary_context(
        settings, engine, facts(houses_partially_damaged=3, place_detail="Purok 2"), judge
    )
    first = message(text="Tatlong bahay ang nasira sa Casile, Purok 2")
    await bot.on_text(first, ctx)
    await settle(tasks)
    (summary,) = summaries(first)
    sent = first.effective_message.reply_text.return_value
    original = rows(engine, Report)[0]

    llm.facts_reply = facts(houses_partially_damaged=2)  # no is_correction from the model
    judge.replies = [{"decision": "unsure", "confidence": 0.6, "reason": "omits purok"}]
    correction = message(
        text="Dalawang bahay lang po", reply_to_message=SimpleNamespace(message_id=sent.message_id)
    )
    await bot.on_text(correction, ctx)
    assert replies(correction)[0] == bot.CORRECTION_RECEIVED
    await settle(tasks)
    second = rows(engine, Report)[1]
    assert second.corrects_report_id == original.id
    with Session(engine) as session:
        stored = session.get(Assessment, 2).facts
    assert stored["is_correction"] is True
    assert second.incident_id == original.incident_id
    (decision,) = [d for d in rows(engine, AgentDecision) if d.report_id == second.id]
    assert decision.judge_model is None and "reply to the bot's summary" in decision.reason
    (answer,) = summaries(correction)
    assert "Recorded as a correction" in answer
    assert "Lugar / Place: Casile" in answer  # the incident's place: the reply named none


async def test_a_reply_to_anything_else_is_an_ordinary_report(settings, engine):
    ctx, tasks, _ = summary_context(settings, engine)
    update = message(text="Baha rin dito", reply_to_message=SimpleNamespace(message_id=12345))
    await bot.on_text(update, ctx)
    await settle(tasks)
    assert replies(update)[0] == bot.RECEIVED
    assert rows(engine, Report)[0].corrects_report_id is None


async def test_another_reporters_report_is_never_linked(settings, engine):
    from app.intake import IntakeService

    intake = IntakeService(settings, engine)
    theirs = await intake.ingest_text(1, "Baha sa Casile")
    mine = await intake.ingest_text(2, "Mali po", corrects_report_id=theirs.id)
    assert mine.corrects_report_id is None


def test_acknowledgements_keep_only_the_latest_generation_and_forget_after_the_window():
    now = [0.0]
    acks = Acknowledgements(quiet_s=0, reply_window_s=60, limit=2, clock=lambda: now[0])
    first, second = object(), object()
    g1 = acks.touch(7, first)
    g2 = acks.touch(7, second)
    assert acks.due(7, g1) is None and acks.due(7, g2) is second and acks.due(7, g2) is None
    for message_id in (1, 2, 3):
        acks.remember(SimpleNamespace(chat_id=42, message_id=message_id), report_id=message_id)
    reply = lambda message_id: SimpleNamespace(  # noqa: E731
        chat_id=42, reply_to_message=SimpleNamespace(message_id=message_id)
    )
    assert acks.replied_to(reply(1)) is None  # forgotten: over the limit
    assert acks.replied_to(reply(3)) == 3
    assert (
        acks.replied_to(SimpleNamespace(chat_id=99, reply_to_message=reply(3).reply_to_message))
        is None
    )
    now[0] = 61
    assert acks.replied_to(reply(3)) is None  # outside the reply window
    # A fresh process (a restart) knows no summary: replies become ordinary reports.
    assert Acknowledgements().replied_to(reply(2)) is None


def test_the_bot_turns_acknowledgements_on(settings):
    from pydantic import SecretStr

    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123456:ABC-test")})
    application = bot.build_application(configured)
    assert isinstance(application.bot_data["acks"], Acknowledgements)

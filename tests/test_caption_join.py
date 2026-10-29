"""A text sent right after a caption-less photo or album becomes its caption (live test
2026-10-29); an album is one report."""

import asyncio
from datetime import timedelta

import pytest
from sqlmodel import Session

from app import bot
from app.assessment import AssessmentService
from app.db import init_db, make_engine
from app.incidents import IncidentService
from app.intake import ALBUM_JOIN_WINDOW, CAPTION_JOIN_WINDOW, IntakeService
from app.models import Assessment, Incident, Media, PhotoAssessment, Report
from tests.test_abuse_guards import add_cost, block, context, rows
from tests.test_assessment import FakeLLM, FakeSam, jpeg
from tests.test_bot import Downloadable, image_document, make_update
from tests.test_incidents import FakeJudge

CAPTION = "Sira rin po bubong namin sa Casile"


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def age(engine, report_id, delta):
    """Move a report back in time, as if it had arrived `delta` ago."""
    with Session(engine) as session:
        report = session.get(Report, report_id)
        report.received_at -= delta
        session.add(report)
        session.commit()


def facts_texts(llm):
    return [c["user_text"] for c in llm.calls if c.get("image") is None]


def photo_file(caption=None):
    """An image sent as a file (as in the live test), caption-less unless given."""
    return make_update(document=image_document("image/jpeg", data=jpeg()), caption=caption)


async def test_text_14_seconds_after_a_photo_becomes_its_caption(settings, engine):
    ctx, tasks, llm = context(settings, engine)
    await bot.on_image_document(photo_file(), ctx)
    await asyncio.gather(*tasks)
    (photo,) = rows(engine, Report)
    assert (photo.status, photo.barangay, photo.incident_id) == ("assessed", None, 1)
    age(engine, photo.id, timedelta(seconds=14))
    update = make_update(text=CAPTION)
    await bot.on_text(update, ctx)
    update.effective_message.reply_text.assert_awaited_once_with(bot.CAPTION_ADDED)
    await asyncio.gather(*tasks)
    (report,) = rows(engine, Report)
    assert (report.id, report.text, report.status) == (photo.id, CAPTION, "assessed")
    assert report.barangay == "Casile"
    assert any(CAPTION in text for text in facts_texts(llm))
    assert rows(engine, Assessment)[0].facts is not None
    # Re-grouped: the singleton incident moved to the caption's barangay.
    assert [(i.id, i.barangay) for i in rows(engine, Incident)] == [(1, "Casile")]


@pytest.mark.parametrize(
    "case", ["late", "photo had a caption", "another reporter", "voice note", "report between"]
)
async def test_text_stays_its_own_report(settings, engine, case):
    intake = IntakeService(settings, engine)
    if case == "voice note":
        first = await intake.ingest_voice(7, b"OggS")  # no transcriber: transcript stays empty
    else:
        caption = "Baha sa Malaban" if case == "photo had a caption" else None
        first = await intake.ingest_photo(7, jpeg(), caption=caption)
    if case == "late":
        age(engine, first.id, timedelta(minutes=10))
    if case == "report between":
        await intake.ingest_location(8, 14.33, 121.08)  # another reporter: does not count
        await intake.ingest_text(7, "Baha rin sa Casile")
    sender = 8 if case == "another reporter" else 7
    assert not intake.can_attach_caption(sender)
    assert await intake.attach_caption(sender, CAPTION) is None
    assert rows(engine, Report)[0].text == first.text


async def test_window_and_pin_do_not_break_the_join(settings, engine):
    intake = IntakeService(settings, engine)
    photo = await intake.ingest_photo(7, jpeg())
    await intake.ingest_location(7, 14.33, 121.08)  # a pin attaches to the photo: no new report
    age(engine, photo.id, CAPTION_JOIN_WINDOW - timedelta(seconds=5))
    assert intake.can_attach_caption(7)
    joined = await intake.attach_caption(7, f"  {CAPTION}  ")
    assert (joined.id, joined.text, joined.lat) == (photo.id, CAPTION, 14.33)
    # A report that has text is never overwritten.
    assert await intake.attach_caption(7, "pangalawang text") is None


async def test_blocked_reporter_text_never_joins(settings, engine):
    intake = IntakeService(settings, engine)
    photo = await intake.ingest_photo(7, jpeg())
    block(engine, photo.reporter_id)
    assert not intake.can_attach_caption(7)
    assert await intake.attach_caption(7, CAPTION) is None
    held = await intake.ingest_text(7, CAPTION)
    assert held.id != photo.id and held.status == "held"


async def test_text_after_a_voice_note_is_a_new_report_in_the_bot(settings, engine):
    ctx, tasks, _ = context(settings, engine)
    await bot.on_voice(make_update(voice=Downloadable(b"OggS")), ctx)
    update = make_update(text=CAPTION)
    await bot.on_text(update, ctx)
    update.effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)
    await asyncio.gather(*tasks)
    assert len(rows(engine, Report)) == 2


async def test_joined_caption_does_not_count_toward_the_rate_limit(settings, engine):
    settings.reports_per_hour = 1
    ctx, tasks, _ = context(settings, engine)
    await bot.on_image_document(photo_file(), ctx)
    for expected in (bot.CAPTION_ADDED, bot.RATE_LIMITED):
        update = make_update(text=CAPTION)
        await bot.on_text(update, ctx)
        update.effective_message.reply_text.assert_awaited_once_with(expected)
    await asyncio.gather(*tasks)
    (report,) = rows(engine, Report)
    assert report.text == CAPTION


class GatedLLM(FakeLLM):
    """Holds the first photo judgment until released, to land a caption mid-assessment."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def chat_json(self, **kwargs):
        if kwargs.get("image") is not None and not self.release.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().chat_json(**kwargs)


def gated_context(settings, engine):
    ctx, tasks, _ = context(settings, engine)
    llm = GatedLLM()
    ctx.application.bot_data["assessor"] = AssessmentService(
        settings, engine, llm, FakeSam(), IncidentService(settings, engine, FakeJudge())
    )
    return ctx, tasks, llm


async def test_caption_during_the_first_assessment_ends_in_the_facts(settings, engine):
    ctx, tasks, llm = gated_context(settings, engine)
    await bot.on_image_document(photo_file(), ctx)
    await llm.entered.wait()
    assert rows(engine, Report)[0].status == "assessing"
    update = make_update(text=CAPTION)
    await bot.on_text(update, ctx)
    update.effective_message.reply_text.assert_awaited_once_with(bot.CAPTION_ADDED)
    llm.release.set()
    await asyncio.gather(*tasks)
    (report,) = rows(engine, Report)
    assert (report.text, report.status, report.barangay) == (CAPTION, "assessed", "Casile")
    assert rows(engine, Assessment)[0].facts is not None
    assert any(CAPTION in text for text in facts_texts(llm))
    assert [i.barangay for i in rows(engine, Incident)] == ["Casile"]


async def test_caption_during_assessment_survives_a_spend_hold(settings, engine):
    """The stale first pass must not end "assessed": the held re-run resumes with the caption."""
    ctx, tasks, llm = gated_context(settings, engine)
    await bot.on_image_document(photo_file(), ctx)
    await llm.entered.wait()
    await bot.on_text(make_update(text=CAPTION), ctx)
    add_cost(engine, 11)  # over the $10 cap: the queued re-assessment is held
    llm.release.set()
    await asyncio.gather(*tasks)
    (report,) = rows(engine, Report)
    assert (report.status, report.hold_reason, report.incident_id) == ("held", "spend cap", None)
    assessor = ctx.application.bot_data["assessor"]
    assert assessor.report_ids(pending_only=True) == [report.id]
    settings.daily_spend_cap_usd = 12
    resumed = await assessor.assess(report.id)
    assert (resumed.status, resumed.barangay) == ("assessed", "Casile")
    assert any(CAPTION in text for text in facts_texts(llm))


def album_photo(index, album="album-1", caption=None):
    """One photo of a Telegram album: each arrives as its own message sharing media_group_id."""
    return make_update(
        photo=[Downloadable(jpeg(size=(800 + index, 400)))],
        caption=caption,
        media_group_id=album,
    )


async def test_caption_less_album_then_one_text_is_one_captioned_report(settings, engine):
    """Live gap from 6a: a text after a caption-less album captioned only the last photo.
    The album is one report, so the text reaches every photo, and each photo is assessed."""
    ctx, tasks, llm = context(settings, engine)
    updates = [album_photo(i) for i in range(3)]
    for update in updates:
        await bot.on_photo(update, ctx)
    updates[0].effective_message.reply_text.assert_awaited_once_with(bot.RECEIVED)
    for later in updates[1:]:
        later.effective_message.reply_text.assert_not_awaited()  # one "received" per album
    text = make_update(text=CAPTION)
    await bot.on_text(text, ctx)
    text.effective_message.reply_text.assert_awaited_once_with(bot.CAPTION_ADDED)
    await asyncio.gather(*tasks)
    (report,) = rows(engine, Report)
    assert (report.text, report.status, report.barangay) == (CAPTION, "assessed", "Casile")
    photos = [m for m in rows(engine, Media) if m.kind == "photo"]
    assert len(photos) == 3 and {m.report_id for m in photos} == {report.id}
    judged = {p.media_id for p in rows(engine, PhotoAssessment)}
    assert judged == {m.id for m in photos}
    # The photos of one album are not "resent" duplicates of each other.
    assert not rows(engine, Assessment)[0].signals


async def test_album_caption_on_a_later_photo_and_separate_albums(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_photo(7, jpeg(), album="a")
    joined = await intake.ingest_photo(7, jpeg(size=(900, 500)), caption=CAPTION, album="a")
    assert (joined.id, joined.text) == (first.id, CAPTION)
    # Another album, a photo without an album, and another reporter's same id start new reports.
    other = await intake.ingest_photo(7, jpeg(), album="b")
    loose = await intake.ingest_photo(7, jpeg())
    stranger = await intake.ingest_photo(8, jpeg(), album="a")
    assert len({first.id, other.id, loose.id, stranger.id}) == 4
    # Too late for the album window: a new report.
    age(engine, other.id, ALBUM_JOIN_WINDOW + timedelta(seconds=1))
    assert (await intake.ingest_photo(7, jpeg(), album="b")).id != other.id


async def test_album_photos_count_once_toward_the_rate_limit(settings, engine):
    settings.reports_per_hour = 1
    ctx, tasks, _ = context(settings, engine)
    for index in range(4):
        await bot.on_photo(album_photo(index), ctx)
    refused = album_photo(9, album="album-2")
    await bot.on_photo(refused, ctx)
    refused.effective_message.reply_text.assert_awaited_once_with(bot.RATE_LIMITED)
    await asyncio.gather(*tasks)
    (report,) = rows(engine, Report)
    assert len([m for m in rows(engine, Media) if m.kind == "photo"]) == 4


async def test_blocked_reporters_album_photo_never_joins(settings, engine):
    intake = IntakeService(settings, engine)
    first = await intake.ingest_photo(7, jpeg(), album="a")
    block(engine, first.reporter_id)
    assert not intake.can_attach_album_photo(7, "a")
    held = await intake.ingest_photo(7, jpeg(), album="a")
    assert held.id != first.id and held.status == "held"

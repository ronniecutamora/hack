"""Telegram bot intake: text, photos, voice notes and locations become Reports.

Run with: uv run python -m app.bot (long polling, local development). A deploy runs the same
Application inside the web app instead and Telegram posts updates to it (app.telegram_webhook).
A token has either a webhook or polling, never both, so polling refuses to start while the
token has a webhook (it would otherwise delete the webhook and take the bot from the deploy).
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from functools import wraps

from sqlalchemy import Engine
from sqlmodel import Session, select
from telegram import Bot, Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import BaseRequest

from app.assessment import make_assessor
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.intake import Admission, IntakeService
from app.logs import configure_logging
from app.meta.factory import make_transcriber
from app.models import STATUS_HELD, Assessment, Incident, Report
from app.reporter_summary import Acknowledgements, summary_text
from app.retention import purge_loop

logger = logging.getLogger(__name__)

# Telegram's library defaults (5 s) time out on slow home connections; voice downloads and
# replies need more headroom.
TELEGRAM_CONNECT_TIMEOUT_S = 20
TELEGRAM_READ_WRITE_TIMEOUT_S = 30
# The TLS handshake to Telegram sometimes takes 1-8 s from a PH home line (measured
# 2026-10-27), and now and then a connect fails outright, so network calls are retried.
TELEGRAM_RETRY_DELAYS_S = (1, 3)

# The handlers below read only new messages, so Telegram need not send anything else.
ALLOWED_UPDATES = [Update.MESSAGE]

MISSING_TOKEN_MESSAGE = (
    "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather on Telegram, then put the "
    "token in .env (see .env.example) and run again."
)


# Shared by /start and the bot's public profile (app.bot_profile), so both promise the same.
NO_NAMES_STORED = (
    "Hindi namin iniimbak ang pangalan o numero mo, pseudonymous ID lang. / "
    "We store no names or phone numbers, only a pseudonymous ID."
)
# Ulat is a hackathon prototype the city has not adopted: nothing may say or imply that a report
# reaches City Hall or that anyone at the LGU watches these chats.
PROTOTYPE_NOTICE = (
    "Prototype para sa disaster office ng Biñan; hindi pa opisyal o binabantayan ng LGU. / "
    "A prototype for Biñan's disaster office: not yet an official city service, and not "
    "monitored by the city yet."
)
NOT_A_HOTLINE = (
    "Hindi ito emergency hotline. Para sa rescue, tumawag sa 911. / "
    "Not an emergency line. For rescue, call 911."
)


def start_text(ttl_hours: int, held_max_hours: int) -> str:
    return (
        "Kumusta! Ito ang bot para sa pag-report ng pinsala ng bagyo. Magpadala ng larawan, "
        "voice note, text, o location ng pinsala.\n"
        "Hello! Send photos, voice notes, text or your location to report typhoon damage.\n\n"
        f"{PROTOTYPE_NOTICE}\n{NOT_A_HOTLINE}\n\n"
        "Privacy:\n"
        f"- {NO_NAMES_STORED}\n"
        f"- Binubura ang orihinal na larawan at audio pagkatapos ng {ttl_hours} oras, "
        f"o hanggang {held_max_hours} oras kung nakahinto ang pagproseso; "
        "text at assessment lang ang itinatabi. / "
        f"Original photos and audio are deleted after {ttl_hours} hours, "
        f"or up to {held_max_hours} hours if processing is paused; only text and "
        "assessments are kept.\n"
        "- Ipoproseso ng AI models ng Meta ang mga ito para sa damage-assessment prototype. / "
        "Meta AI models process them for the damage-assessment prototype.\n"
        "- Huwag magpadala ng mukha ng tao o ID. / Please do not send faces or ID documents."
    )


RECEIVED = "Natanggap na ang report mo. Salamat! / Report received. Thank you!"
CORRECTION_RECEIVED = (
    "Natanggap ang pagtatama mo. Salamat! / Your correction was received. Thank you!"
)
RATE_LIMITED = (
    "Marami ka nang naipadalang report ngayon. Subukan ulit mamaya. "
    "Kung emergency, tumawag sa 911. / "
    "You've sent many reports recently. Please try again later. For emergencies, call 911."
)
LOCATION_RECEIVED = "Natanggap ang location. / Location received."
CAPTION_ADDED = "Idinugtong sa iyong larawan. / Added to your photo."
VOICE_NOT_TRANSCRIBED = (
    "Natanggap ang voice note pero hindi pa ito na-transcribe. Subukan ulit o mag-text. / "
    "Voice note received but not transcribed yet. Please try again or send text."
)
UNSUPPORTED_IMAGE = (
    "Hindi mabasa ang file na ito. Paki-send bilang JPEG o PNG, o bilang karaniwang photo. / "
    "This file type can't be read. Please send it as a JPEG or PNG, or as a normal photo."
)
TOO_LARGE = (
    "Masyadong malaki ang file (max 20 MB). Paki-send bilang karaniwang photo. / "
    "The file is too large (max 20 MB). Please send it as a normal photo."
)
# Images sent as files keep their EXIF GPS (normal photos are stripped by Telegram).
IMAGE_DOCUMENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API download limit
ERROR_REPLY = (
    "May problema sa pagproseso. Subukan ulit mamaya. / "
    "Something went wrong. Please try again later."
)


def _intake(context: ContextTypes.DEFAULT_TYPE) -> IntakeService:
    return context.application.bot_data["intake"]


async def _with_retries[T](call: Callable[[], Awaitable[T]]) -> T:
    """Run a Telegram network call, retrying transient network failures."""
    for delay in TELEGRAM_RETRY_DELAYS_S:
        try:
            return await call()
        except BadRequest:  # a subclass of NetworkError, but retrying won't fix it
            raise
        except NetworkError as exc:
            logger.warning("Telegram call failed (%s); retrying in %d s", exc, delay)
            await asyncio.sleep(delay)
    return await call()


async def _download(media: object) -> bytes:
    file = await _with_retries(media.get_file)
    return bytes(await _with_retries(file.download_as_bytearray))


async def _reply(message: object, text: str) -> None:
    await _with_retries(lambda: message.reply_text(text))


def _assess_later(
    context: ContextTypes.DEFAULT_TYPE, report: Report, message: object | None = None
) -> None:
    """Assess in the background so the reporter gets "received" without waiting on models, then
    tell them what was understood (when acknowledgements are on and `message` is given)."""
    application = context.application
    assessor = application.bot_data.get("assessor")
    if assessor is None or report.status == STATUS_HELD:
        return
    acks: Acknowledgements | None = application.bot_data.get("acks")
    generation = acks.touch(report.id, message) if acks and message is not None else None
    application.create_task(
        _assess_and_acknowledge(application, assessor, report.id, generation),
        name=f"assess-{report.id}",
    )


async def _assess_and_acknowledge(
    application: Application, assessor: object, report_id: int, generation: int | None
) -> None:
    report = await assessor.assess(report_id)
    acks: Acknowledgements | None = application.bot_data.get("acks")
    if generation is None or acks is None or report is None:
        return
    # Wait for the quiet period: a later caption, pin or album photo supersedes this task.
    await asyncio.sleep(acks.quiet_s)
    message = acks.due(report_id, generation)
    if message is None:
        return
    text = reporter_summary(application.bot_data["intake"].engine, report_id)
    if text is None:  # held, blocked or failed: the reporter already has "received"
        return
    try:
        sent = await _with_retries(lambda: message.reply_text(text))
    except TelegramError as exc:
        logger.warning("could not send the summary of report %s: %s", report_id, exc)
        return
    acks.remember(sent, report_id)


def reporter_summary(engine: object, report_id: int) -> str | None:
    """The bilingual summary of a report as stored now (app.reporter_summary)."""
    with Session(engine) as session:
        report = session.get(Report, report_id)
        if report is None:
            return None
        facts = session.exec(
            select(Assessment.facts).where(Assessment.report_id == report_id)
        ).first()
        incident = session.get(Incident, report.incident_id) if report.incident_id else None
        return summary_text(report, facts, incident.barangay if incident else None)


def _admitted(*, attaches: Callable[[IntakeService, int, object], bool] | None = None):
    """One admission per report message, before download.

    A message that `attaches` to the sender's existing report (a late pin, a late caption, the
    next photo of an album) creates no report, so it is exempt from the rate limit.
    """

    def decorate(handler):
        @wraps(handler)
        async def guarded(update, context):
            intake = _intake(context)
            user_ref = update.effective_user.id
            async with intake.admission_lock(user_ref):
                admission = intake.admit(user_ref)
                attached = attaches is not None and attaches(
                    intake, user_ref, update.effective_message
                )
                if admission == Admission.RATE_LIMITED and not attached:
                    await _reply(update.effective_message, RATE_LIMITED)
                    return
                return await handler(update, context)

        return guarded

    return decorate


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    await _reply(
        update.effective_message,
        start_text(settings.raw_media_ttl_hours, settings.held_media_max_hours),
    )


def _album(message: object) -> str | None:
    return getattr(message, "media_group_id", None)


@_admitted(attaches=lambda intake, user_ref, _message: intake.can_attach_caption(user_ref))
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    intake = _intake(context)
    # Photo first, words second: a text right after a caption-less photo is its caption.
    report = await intake.attach_caption(update.effective_user.id, message.text)
    if report is not None:
        await _reply(message, CAPTION_ADDED)
    else:
        # A reply to one of our summaries corrects that report ("Mali? I-reply…").
        acks: Acknowledgements | None = context.application.bot_data.get("acks")
        corrects = acks.replied_to(message) if acks else None
        report = await intake.ingest_text(
            update.effective_user.id, message.text, corrects_report_id=corrects
        )
        await _reply(message, CORRECTION_RECEIVED if report.corrects_report_id else RECEIVED)
    _assess_later(context, report, message)  # re-assesses a photo that got its caption


def _joins_album(intake: IntakeService, user_ref: int, message: object) -> bool:
    return intake.can_attach_album_photo(user_ref, _album(message))


async def _ingest_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: bytes, ext: str
) -> None:
    """One report per photo, or per album: later photos of an album join the first one's report
    silently (one "received" per album, not one per photo)."""
    message = update.effective_message
    intake = _intake(context)
    joins = _joins_album(intake, update.effective_user.id, message)
    report = await intake.ingest_photo(
        update.effective_user.id, data, caption=message.caption, ext=ext, album=_album(message)
    )
    if not joins:
        await _reply(message, RECEIVED)
    _assess_later(context, report, message)


@_admitted(attaches=_joins_album)
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.effective_message.photo[-1]  # Telegram lists sizes smallest to largest
    await _ingest_photo(update, context, await _download(photo), "jpg")


@_admitted(attaches=_joins_album)
async def on_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    document = message.document
    ext = IMAGE_DOCUMENT_TYPES.get(document.mime_type or "")
    if ext is None:  # e.g. HEIC from iPhones: neither Pillow nor the models read it here
        await _reply(message, UNSUPPORTED_IMAGE)
        return
    if (document.file_size or 0) > MAX_DOWNLOAD_BYTES:
        await _reply(message, TOO_LARGE)
        return
    await _ingest_photo(update, context, await _download(document), ext)


@_admitted()
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    data = await _download(message.voice)
    report = await _intake(context).ingest_voice(update.effective_user.id, data, ext="ogg")
    await _reply(
        message,
        RECEIVED if report.transcript or report.status == STATUS_HELD else VOICE_NOT_TRANSCRIBED,
    )
    _assess_later(context, report, message)


@_admitted(attaches=lambda intake, user_ref, _message: intake.can_attach_location(user_ref))
async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    report = await _intake(context).ingest_location(
        update.effective_user.id, message.location.latitude, message.location.longitude
    )
    await _reply(message, LOCATION_RECEIVED)
    _assess_later(context, report, message)  # re-maps a report assessed before the pin came


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("update failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await _reply(update.effective_message, ERROR_REPLY)
        except TelegramError as exc:  # e.g. the network that failed the update is still down
            logger.warning("could not send the error reply: %s", exc)


def add_handlers(application: Application) -> None:
    private = filters.ChatType.PRIVATE
    # /help repeats /start: Telegram clients offer both (see the command list in app.bot_profile).
    application.add_handler(CommandHandler(["start", "help"], on_start, filters=private))
    application.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(private & filters.PHOTO, on_photo))
    application.add_handler(MessageHandler(private & filters.Document.IMAGE, on_image_document))
    application.add_handler(MessageHandler(private & filters.VOICE, on_voice))
    application.add_handler(MessageHandler(private & filters.LOCATION, on_location))
    application.add_error_handler(on_error)


def build_application(
    settings: Settings, *, engine: Engine | None = None, request: BaseRequest | None = None
) -> Application:
    """The bot with its handlers, intake and background assessor. `engine` shares the web app's
    database pool in webhook mode; `request` replaces the HTTP layer to Telegram (tests)."""
    if engine is None:
        engine = make_engine(settings)
        init_db(engine)
    recorder = make_recorder(engine)
    transcriber = make_transcriber(settings, recorder)
    assessor = make_assessor(settings, engine, recorder, transcriber=transcriber)

    async def post_init(application: Application) -> None:
        application.bot_data["purge_task"] = asyncio.create_task(purge_loop(settings, engine))

    async def post_shutdown(application: Application) -> None:
        application.bot_data["purge_task"].cancel()
        await transcriber.aclose()
        await assessor.llm.aclose()
        await assessor.sam.aclose()
        await assessor.incidents.judge.aclose()

    builder = Application.builder().token(settings.telegram_bot_token.get_secret_value())
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    else:
        builder = (
            builder.connect_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
            .read_timeout(TELEGRAM_READ_WRITE_TIMEOUT_S)
            .write_timeout(TELEGRAM_READ_WRITE_TIMEOUT_S)
            .get_updates_connect_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
        )
    application = builder.post_init(post_init).post_shutdown(post_shutdown).build()
    application.bot_data["settings"] = settings
    application.bot_data["intake"] = IntakeService(settings, engine, transcriber)
    application.bot_data["assessor"] = assessor
    application.bot_data["acks"] = Acknowledgements()  # in memory only: chats are never stored
    add_handlers(application)
    return application


async def start_application(application: Application) -> None:
    """Start the bot without polling (webhook mode), with the same hooks run_polling calls."""
    await application.initialize()
    if application.post_init:
        await application.post_init(application)
    await application.start()


async def stop_application(application: Application) -> None:
    """Handle the updates already queued and finish background tasks, then close the clients."""
    if application.running:
        await application.stop()
    await application.shutdown()
    if application.post_shutdown:
        await application.post_shutdown(application)


WEBHOOK_SET_MESSAGE = (
    "This bot token has a webhook ({url}), so a deployed Ulat receives its messages. Polling "
    "here would delete that webhook and take the bot from the deploy. To run the bot locally "
    "on purpose, first roll back: uv run python -m app.deploy webhook delete --target <target>"
)


async def current_webhook(bot: Bot) -> str:
    """The webhook URL Telegram has for this token ("" when none is set)."""
    async with bot:
        return (await _with_retries(bot.get_webhook_info)).url or ""


def main(request: BaseRequest | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.secret_values())
    if not settings.telegram_bot_token.get_secret_value():
        print(MISSING_TOKEN_MESSAGE, file=sys.stderr)
        return 2
    if not settings.pseudonym_salt.get_secret_value():
        print("PSEUDONYM_SALT is not set. Generate one: openssl rand -hex 32", file=sys.stderr)
        return 2
    if settings.telegram_mode == "webhook":
        print(
            "TELEGRAM_MODE=webhook: the web app runs the bot (uvicorn app.main:app). "
            "Unset it to poll from this machine.",
            file=sys.stderr,
        )
        return 2
    token = settings.telegram_bot_token.get_secret_value()
    bot = Bot(token, request=request, get_updates_request=request)
    if url := asyncio.run(current_webhook(bot)):
        host = url.split("/")[2] if url.count("/") >= 2 else "set"
        print(WEBHOOK_SET_MESSAGE.format(url=f"https://{host}/…"), file=sys.stderr)
        return 3
    build_application(settings, request=request).run_polling(allowed_updates=ALLOWED_UPDATES)
    return 0


if __name__ == "__main__":
    sys.exit(main())

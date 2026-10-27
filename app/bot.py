"""Telegram bot intake: text, photos, voice notes and locations become Reports.

Run with: uv run python -m app.bot
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.assessment import make_assessor
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.intake import IntakeService
from app.meta.factory import make_transcriber
from app.models import Report
from app.retention import purge_loop

logger = logging.getLogger(__name__)

# Telegram's library defaults (5 s) time out on slow home connections; voice downloads and
# replies need more headroom.
TELEGRAM_CONNECT_TIMEOUT_S = 20
TELEGRAM_READ_WRITE_TIMEOUT_S = 30
# The TLS handshake to Telegram sometimes takes 1-8 s from a PH home line (measured
# 2026-10-27), and now and then a connect fails outright, so network calls are retried.
TELEGRAM_RETRY_DELAYS_S = (1, 3)

MISSING_TOKEN_MESSAGE = (
    "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather on Telegram, then put the "
    "token in .env (see .env.example) and run again."
)


def start_text(ttl_hours: int) -> str:
    return (
        "Kumusta! Ito ang bot para sa pag-report ng pinsala ng bagyo. Magpadala ng larawan, "
        "voice note, text, o location ng pinsala.\n"
        "Hello! Send photos, voice notes, text or your location to report typhoon damage.\n\n"
        "Privacy:\n"
        "- Hindi namin iniimbak ang pangalan o numero mo, pseudonymous ID lang. / "
        "We store no names or phone numbers, only a pseudonymous ID.\n"
        f"- Binubura ang orihinal na larawan at audio pagkatapos ng {ttl_hours} oras; "
        "text at assessment lang ang itinatabi. / "
        f"Original photos and audio are deleted after {ttl_hours} hours; only text and "
        "assessments are kept.\n"
        "- Ipoproseso ng AI models ng Meta ang mga ito para sa damage assessment ng LGU. / "
        "Meta AI models process them for the LGU damage assessment.\n"
        "- Huwag magpadala ng mukha ng tao o ID. / Please do not send faces or ID documents."
    )


RECEIVED = "Natanggap na ang report mo. Salamat! / Report received. Thank you!"
LOCATION_RECEIVED = "Natanggap ang location. / Location received."
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


def _assess_later(context: ContextTypes.DEFAULT_TYPE, report: Report) -> None:
    """Assess in the background so the reporter gets "received" without waiting on models."""
    assessor = context.application.bot_data.get("assessor")
    if assessor is not None:
        context.application.create_task(assessor.assess(report.id), name=f"assess-{report.id}")


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ttl = context.application.bot_data["settings"].raw_media_ttl_hours
    await _reply(update.effective_message, start_text(ttl))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    report = await _intake(context).ingest_text(update.effective_user.id, message.text)
    await _reply(message, RECEIVED)
    _assess_later(context, report)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    photo = message.photo[-1]  # Telegram lists sizes smallest to largest
    data = await _download(photo)
    report = await _intake(context).ingest_photo(
        update.effective_user.id, data, caption=message.caption, ext="jpg"
    )
    await _reply(message, RECEIVED)
    _assess_later(context, report)


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
    data = await _download(document)
    report = await _intake(context).ingest_photo(
        update.effective_user.id, data, caption=message.caption, ext=ext
    )
    await _reply(message, RECEIVED)
    _assess_later(context, report)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    data = await _download(message.voice)
    report = await _intake(context).ingest_voice(update.effective_user.id, data, ext="ogg")
    await _reply(message, RECEIVED if report.transcript else VOICE_NOT_TRANSCRIBED)
    _assess_later(context, report)


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    report = await _intake(context).ingest_location(
        update.effective_user.id, message.location.latitude, message.location.longitude
    )
    await _reply(message, LOCATION_RECEIVED)
    _assess_later(context, report)  # re-maps a report that was assessed before the pin came


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("update failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await _reply(update.effective_message, ERROR_REPLY)
        except TelegramError as exc:  # e.g. the network that failed the update is still down
            logger.warning("could not send the error reply: %s", exc)


def add_handlers(application: Application) -> None:
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", on_start, filters=private))
    application.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(private & filters.PHOTO, on_photo))
    application.add_handler(MessageHandler(private & filters.Document.IMAGE, on_image_document))
    application.add_handler(MessageHandler(private & filters.VOICE, on_voice))
    application.add_handler(MessageHandler(private & filters.LOCATION, on_location))
    application.add_error_handler(on_error)


def build_application(settings: Settings) -> Application:
    engine = make_engine(settings)
    init_db(engine)
    recorder = make_recorder(engine)
    transcriber = make_transcriber(settings, recorder)
    assessor = make_assessor(settings, engine, recorder)

    async def post_init(application: Application) -> None:
        application.bot_data["purge_task"] = asyncio.create_task(purge_loop(settings, engine))

    async def post_shutdown(application: Application) -> None:
        application.bot_data["purge_task"].cancel()
        await transcriber.aclose()
        await assessor.llm.aclose()
        await assessor.sam.aclose()

    application = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
        .read_timeout(TELEGRAM_READ_WRITE_TIMEOUT_S)
        .write_timeout(TELEGRAM_READ_WRITE_TIMEOUT_S)
        .get_updates_connect_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["intake"] = IntakeService(settings, engine, transcriber)
    application.bot_data["assessor"] = assessor
    add_handlers(application)
    return application


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs contain the bot token
    settings = get_settings()
    if not settings.telegram_bot_token.get_secret_value():
        print(MISSING_TOKEN_MESSAGE, file=sys.stderr)
        return 2
    if not settings.pseudonym_salt.get_secret_value():
        print("PSEUDONYM_SALT is not set. Generate one: openssl rand -hex 32", file=sys.stderr)
        return 2
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())

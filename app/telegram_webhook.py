"""Webhook mode (TELEGRAM_MODE=webhook): the web app runs the Telegram bot in its own event loop.

One process does both jobs, so a deploy has a single SQLite writer. Telegram POSTs each update to
`webhook_path(secret)` with the secret in X-Telegram-Bot-Api-Secret-Token. The handler checks the
header in constant time, puts the update on the Application's queue and answers 200 at once; the
Application then handles updates one at a time, in order, exactly as polling does (an album's
photos and a caption sent right after a photo depend on that order). The assessor's background
tasks run in the same loop as the dashboard.

Shutdown is bounded (the constants in app.bot): new updates get 503 (Telegram re-sends them to
the next container), the queued ones are handled, then `checkpoint` makes them durable (a
database snapshot) before anything slower; in-flight assessments get a short grace and resume at
the next start when cut off.

Registering the webhook with Telegram is a separate, explicit command (`python -m app.deploy
webhook set`), never done at startup, so booting a staging copy cannot take a bot from another
deploy.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response
from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application

from app.bot import drain_updates, start_application, stop_application

logger = logging.getLogger(__name__)

WEBHOOK_PREFIX = "/telegram/webhook/"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_UPDATE_BYTES = 1_000_000  # an update is JSON metadata; files are fetched separately
START_RETRY_S = 30.0


def webhook_path(secret: str) -> str:
    """The URL path Telegram posts to, derived from the secret so the two cannot drift apart."""
    digest = hmac.new(secret.encode(), b"ulat-webhook-path-v1", hashlib.sha256).hexdigest()
    return WEBHOOK_PREFIX + digest[:32]


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


class TelegramWebhook:
    """The bot Application inside the web app: started in the lifespan, fed by the route."""

    def __init__(
        self, application: Application, secret: str, *, retry_s: float | None = None
    ) -> None:
        self.application = application
        self._secret = secret
        self.path = webhook_path(secret)
        self.retry_s = START_RETRY_S if retry_s is None else retry_s
        self.ready = False
        self._starter: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the bot now, or keep retrying in the background: the dashboard must come up
        even when Telegram is unreachable, and Telegram re-sends updates refused with 503."""
        if await self._try_start():
            return
        self._starter = asyncio.create_task(self._keep_trying(), name="telegram-webhook-start")

    async def _try_start(self) -> bool:
        """True when done trying: started, or the token is wrong (retrying cannot fix that)."""
        try:
            await start_application(self.application)
        except InvalidToken:
            # Its message contains the token: log without it. The dashboard stays up.
            logger.error("Telegram rejected TELEGRAM_BOT_TOKEN; the bot is off until it is fixed")
            return True
        except (TelegramError, OSError) as exc:
            logger.warning("Telegram bot did not start (%s); retrying", type(exc).__name__)
            with contextlib.suppress(Exception):
                await self.application.shutdown()
            return False
        self.ready = True
        logger.info("Telegram bot running in webhook mode")
        return True

    async def _keep_trying(self) -> None:
        while True:
            await asyncio.sleep(self.retry_s)
            if await self._try_start():
                return

    async def stop(self, checkpoint: Callable[[], Awaitable[object]] | None = None) -> None:
        """Refuse new updates, handle the acknowledged ones, run `checkpoint` (the deploy's
        database snapshot), then stop the bot. Every step is bounded (app.bot)."""
        if self._starter is not None:
            self._starter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._starter
        if not self.ready:
            return
        self.ready = False  # from here on handle() answers 503
        await drain_updates(self.application)
        if checkpoint is not None:
            try:
                await checkpoint()
            except Exception:
                logger.exception("shutdown: the snapshot after the queued updates failed")
        await stop_application(self.application)

    async def handle(self, request: Request, path_token: str) -> Response:
        """One update from Telegram. 404 for a wrong path, 403 for a wrong secret header."""
        if not _same(WEBHOOK_PREFIX + path_token, self.path):
            return Response(status_code=404)
        if not _same(request.headers.get(SECRET_HEADER, ""), self._secret):
            logger.warning("refused a webhook call without the right secret token")
            return Response(status_code=403)
        if not self.ready:
            return Response(status_code=503, headers={"Retry-After": "30"})
        body = await request.body()
        if len(body) > MAX_UPDATE_BYTES:
            return Response(status_code=413)
        try:
            data = json.loads(body)
            update = Update.de_json(data, self.application.bot) if isinstance(data, dict) else None
        except (ValueError, TypeError, KeyError):
            update = None
        if update is None:
            return Response(status_code=400)
        if not self.ready:  # shutdown began while the body was read: let Telegram re-send it
            return Response(status_code=503, headers={"Retry-After": "30"})
        await self.application.update_queue.put(update)
        return Response(status_code=200)

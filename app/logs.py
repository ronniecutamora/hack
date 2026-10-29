"""Process logging with every configured secret masked.

Library loggers can echo credentials: httpx logs request URLs, and a Telegram bot token is part
of every Bot API URL; python-telegram-bot prints a rejected token in its error. httpx is held at
WARNING, and a filter on the root handlers replaces any secret value that still reaches a record
(message, arguments or traceback) with "[redacted]".
"""

import logging
from collections.abc import Iterable

REDACTED = "[redacted]"
# Shorter values are not secrets worth masking, and masking them would garble ordinary text.
MIN_SECRET_LENGTH = 8


class RedactSecrets(logging.Filter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Longest first, so a secret that contains another is masked whole.
        self.secrets = sorted({s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len)[::-1]

    def _mask(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        masked = self._mask(message)
        if masked != message or record.args:
            record.msg, record.args = masked, None
        if record.exc_text:
            record.exc_text = self._mask(record.exc_text)
        if record.stack_info:
            record.stack_info = self._mask(record.stack_info)
        return True


def configure_logging(secrets: Iterable[str], level: int = logging.INFO) -> None:
    """INFO logs to stderr with `secrets` masked on every root handler; safe to call twice."""
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs contain the bot token
    redactor = RedactSecrets(secrets)
    for handler in logging.getLogger().handlers:
        for old in [f for f in handler.filters if isinstance(f, RedactSecrets)]:
            handler.removeFilter(old)
        handler.addFilter(redactor)

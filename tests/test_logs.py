"""Secrets never reach a log line, whatever logger or exception carries them."""

import logging

from app.logs import REDACTED, RedactSecrets, configure_logging

TOKEN = "7000001:AAsecret-bot-token"


def record(msg, *args, exc_info=None):
    return logging.LogRecord("x", logging.ERROR, __file__, 1, msg, args, exc_info)


def test_secrets_are_masked_in_messages_arguments_and_tracebacks():
    redact = RedactSecrets([TOKEN, "short", ""])
    message = record("GET https://api.telegram.org/bot%s/getMe", TOKEN)
    redact.filter(message)
    assert message.getMessage() == f"GET https://api.telegram.org/bot{REDACTED}/getMe"
    try:
        raise ValueError(f"The token `{TOKEN}` was rejected")
    except ValueError:
        import sys

        failed = record("start failed", exc_info=sys.exc_info())
    redact.filter(failed)
    assert TOKEN not in logging.Formatter().format(failed)
    plain = record("nothing %s here", "secret-free")
    redact.filter(plain)
    assert plain.getMessage() == "nothing secret-free here"


def test_configure_logging_installs_one_redactor_per_handler(caplog):
    configure_logging([TOKEN])
    configure_logging([TOKEN])
    root = logging.getLogger()
    for handler in root.handlers:
        assert sum(isinstance(f, RedactSecrets) for f in handler.filters) == 1
    assert logging.getLogger("httpx").level == logging.WARNING

"""Webhook mode: the web app runs the bot, and Telegram's secret header guards the route."""

import logging
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session, select

from app import bot
from app.config import Settings
from app.main import create_app
from app.models import STATUS_ASSESSED, Report
from app.reporter_summary import HEADER, Acknowledgements
from app.telegram_webhook import SECRET_HEADER, WEBHOOK_PREFIX, webhook_path
from tests.fake_telegram import FakeTelegram, text_update

HOST = "ulat.example"
SECRET = "w" * 40
TOKEN = "7000001:AAtest-token-value"


@pytest.fixture
def webhook_settings(settings):
    return settings.model_copy(
        update={
            "telegram_mode": "webhook",
            "telegram_bot_token": SecretStr(TOKEN),
            "telegram_webhook_secret": SecretStr(SECRET),
            "allowed_hosts": [HOST],
            "officer_passcode": SecretStr("correct-horse-battery"),
            "session_secret": SecretStr("s" * 40),
        }
    )


class MarkAssessed:
    """Stands in for the model pipeline: marks the report assessed, as a finished run would."""

    def __init__(self, engine):
        self.engine = engine

    async def assess(self, report_id):
        with Session(self.engine) as session:
            report = session.get(Report, report_id)
            report.status = STATUS_ASSESSED
            session.add(report)
            session.commit()
            session.refresh(report)
            return report


def webhook_app(settings, fake: FakeTelegram):
    application = bot.build_application(settings, request=fake)
    application.bot_data["assessor"] = MarkAssessed(application.bot_data["intake"].engine)
    application.bot_data["acks"] = Acknowledgements(quiet_s=0)
    return create_app(settings, telegram_application=application)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def post_update(client, payload, *, secret=SECRET, path=None):
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    return client.post(path or webhook_path(SECRET), json=payload, headers=headers)


def test_path_is_derived_from_the_secret():
    path = webhook_path(SECRET)
    assert path.startswith(WEBHOOK_PREFIX) and SECRET not in path
    assert webhook_path(SECRET) == path and webhook_path("x" * 40) != path


def test_valid_update_runs_through_the_handlers_and_the_summary_reply(webhook_settings):
    fake = FakeTelegram()
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:
        response = post_update(client, text_update("Baha sa Brgy. Malaban, tuhod"))
        assert response.status_code == 200
        # "Received" at once, then the summary once the (fake) assessment finished: the
        # assessor's background task runs in the web app's loop with the same Application.
        assert wait_for(lambda: len(fake.sent_texts()) == 2), fake.calls
        received, summary = fake.sent_texts()
        assert received == bot.RECEIVED
        assert summary.startswith(HEADER)
        with Session(client.app.state.engine) as session:
            (report,) = session.exec(select(Report)).all()
        assert report.text == "Baha sa Brgy. Malaban, tuhod" and report.channel == "telegram"
    # Shutdown stopped the bot cleanly.
    assert not client.app.state.telegram.ready


@pytest.mark.parametrize("secret", [None, "", "w" * 39, "x" * 40])
def test_wrong_or_missing_secret_is_refused(webhook_settings, secret, caplog):
    fake = FakeTelegram()
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:
        assert post_update(client, text_update("spoofed"), secret=secret).status_code == 403
        with Session(client.app.state.engine) as session:
            assert session.exec(select(Report)).all() == []
    assert fake.sent_texts() == []


def test_wrong_path_and_bad_bodies(webhook_settings):
    fake = FakeTelegram()
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:
        assert (
            post_update(client, text_update("x"), path=WEBHOOK_PREFIX + "0" * 32).status_code == 404
        )
        path = webhook_path(SECRET)
        bad = client.post(path, content=b"not json", headers={SECRET_HEADER: SECRET})
        assert bad.status_code == 400
        assert post_update(client, [1, 2]).status_code == 400


def test_bot_that_cannot_reach_telegram_keeps_the_dashboard_up(webhook_settings, caplog):
    fake = FakeTelegram(get_me_status=502)
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:
        assert client.get("/health").status_code == 200
        response = post_update(client, text_update("later"))
        assert response.status_code == 503  # Telegram keeps it and sends it again
        assert response.headers["Retry-After"] == "30"


def test_a_rejected_token_is_never_logged(webhook_settings, caplog):
    fake = FakeTelegram(get_me_status=401)
    caplog.set_level(logging.INFO)
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:
        assert client.get("/health").status_code == 200
        assert post_update(client, text_update("x")).status_code == 503
    assert "rejected TELEGRAM_BOT_TOKEN" in caplog.text
    assert TOKEN not in caplog.text


def test_polling_mode_has_no_webhook(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert post_update(client, text_update("x")).status_code == 404


def test_webhook_mode_needs_its_secrets():
    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN"):
        Settings(_env_file=None, telegram_mode="webhook")
    with pytest.raises(ValueError, match="32-256 characters"):
        Settings(_env_file=None, telegram_webhook_secret="short")


def polling_settings(settings):
    return settings.model_copy(update={"telegram_bot_token": SecretStr(TOKEN)})


def test_polling_refuses_to_start_while_a_webhook_is_set(settings, monkeypatch, capsys):
    monkeypatch.setattr(bot, "get_settings", lambda: polling_settings(settings))
    started = []
    monkeypatch.setattr(bot, "build_application", lambda *a, **k: started.append(1))
    fake = FakeTelegram(webhook_url=f"https://{HOST}{webhook_path(SECRET)}")
    assert bot.main(request=fake) == 3
    err = capsys.readouterr().err
    assert "app.deploy webhook delete" in err and HOST in err
    assert webhook_path(SECRET) not in err  # the secret path is not printed
    assert started == []
    assert [name for name, _ in fake.calls if name == "deleteWebhook"] == []


async def test_current_webhook_is_empty_when_polling_is_safe():
    fake = FakeTelegram()
    assert await bot.current_webhook(bot.Bot(TOKEN, request=fake, get_updates_request=fake)) == ""

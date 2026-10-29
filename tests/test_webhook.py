"""Webhook mode: the web app runs the bot, and Telegram's secret header guards the route."""

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session, select

from app import bot, telegram_webhook
from app.config import Settings
from app.db import init_db, make_engine
from app.intake import IntakeService
from app.main import create_app
from app.models import STATUS_ASSESSED, Media, Report, utcnow
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

    async def assess(self, report_id, **_kwargs):
        with Session(self.engine) as session:
            report = session.get(Report, report_id)
            report.status = STATUS_ASSESSED
            session.add(report)
            session.commit()
            session.refresh(report)
            return report

    resume = assess


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


# --- Durability of acknowledged reports and a bounded shutdown ---------------------------------


class HangingAssessor:
    """An assessment that never finishes (a slow model): shutdown must not wait for it."""

    def __init__(self):
        self.started = []
        self.cancelled = []

    async def assess(self, report_id, **_kwargs):
        self.started.append(report_id)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(report_id)
            raise


def snapshot_texts(folder):
    """Report texts in each finished snapshot on the (shared) volume, oldest first."""
    found = []
    for path in sorted(folder.glob("app-*.db")):
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            found.append([row[0] for row in connection.execute("SELECT text FROM report")])
        except sqlite3.Error:
            found.append([])
        finally:
            connection.close()
    return found


def durable_settings(webhook_settings, tmp_path, disk):
    return webhook_settings.model_copy(
        update={
            "database_path": tmp_path / disk / "app.db",
            "snapshot_dir": tmp_path / "volume" / "snapshots",
            "snapshot_interval_minutes": 60.0,  # only a request or the shutdown snapshots
        }
    )


def test_shutdown_fits_modal_grace_with_room_for_the_snapshots():
    stages = bot.SHUTDOWN_DRAIN_S + bot.SHUTDOWN_ASSESS_S + 2 * bot.SHUTDOWN_STOP_S
    assert stages + 5 <= bot.MODAL_EXIT_GRACE_S


async def test_flush_ends_the_quiet_period_at_once():
    acks = Acknowledgements(quiet_s=60)
    waiting = asyncio.create_task(acks.quiet())
    await asyncio.sleep(0)
    acks.flush()
    await asyncio.wait_for(waiting, timeout=1)
    await asyncio.wait_for(acks.quiet(), timeout=1)  # and every later wait is skipped too


def test_an_acknowledged_report_is_in_a_snapshot_within_seconds(webhook_settings, tmp_path):
    """A crash (no shutdown snapshot) must not lose a report the bot already acknowledged."""
    settings = durable_settings(webhook_settings, tmp_path, "disk1")
    fake = FakeTelegram()
    application = bot.build_application(settings, request=fake)
    application.bot_data["assessor"] = MarkAssessed(application.bot_data["intake"].engine)
    folder = settings.snapshot_dir
    with TestClient(
        create_app(settings, telegram_application=application), base_url=f"https://{HOST}"
    ) as client:
        assert post_update(client, text_update("Baha sa Brgy. Malaban")).status_code == 200
        assert wait_for(lambda: ["Baha sa Brgy. Malaban"] in snapshot_texts(folder))


def test_a_report_acknowledged_just_before_shutdown_survives_the_restart(
    webhook_settings, tmp_path, monkeypatch
):
    """Telegram got its 200, then the container stops while the assessment hangs: the report is
    snapshotted before the (bounded) wait for the assessment, and the next container has it."""
    monkeypatch.setattr(bot, "SHUTDOWN_ASSESS_S", 1.5)
    first = durable_settings(webhook_settings, tmp_path, "disk1")
    fake = FakeTelegram()
    application = bot.build_application(first, request=fake)
    assessor = HangingAssessor()
    application.bot_data["assessor"] = assessor
    app = create_app(first, telegram_application=application)
    posted = threading.Event()

    def container():
        with TestClient(app, base_url=f"https://{HOST}") as client:
            response = post_update(client, text_update("Bagsak ang bubong sa Casile"))
            assert response.status_code == 200
            posted.set()
        # leaving the block is the stop: the lifespan's shutdown runs now

    started = time.monotonic()
    worker = threading.Thread(target=container, daemon=True)
    worker.start()
    assert posted.wait(5)
    # Durable while the shutdown still waits for the hanging assessment.
    assert wait_for(lambda: ["Bagsak ang bubong sa Casile"] in snapshot_texts(first.snapshot_dir))
    assert worker.is_alive()
    worker.join(timeout=15)
    assert not worker.is_alive(), "shutdown waited for the assessment without a limit"
    assert time.monotonic() - started < 10
    assert assessor.cancelled == assessor.started != []
    assert fake.sent_texts() == [bot.RECEIVED]

    second = first.model_copy(  # the next container, without a bot: only its database matters
        update={"database_path": tmp_path / "disk2" / "app.db", "telegram_mode": "polling"}
    )
    with (
        TestClient(create_app(second), base_url=f"https://{HOST}") as client,
        Session(client.app.state.engine) as session,
    ):
        (report,) = session.exec(select(Report)).all()
    assert report.text == "Bagsak ang bubong sa Casile"


# --- The retention purge does not depend on Telegram -------------------------------------------


async def expired_photo(settings):
    """A reporter's photo past the retention TTL, stored before the app starts."""
    engine = make_engine(settings)
    init_db(engine)
    report = await IntakeService(settings, engine).ingest_photo(1, b"old-photo")
    with Session(engine) as session:
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        media.created_at = utcnow() - timedelta(hours=settings.raw_media_ttl_hours + 1)
        session.add(media)
        session.commit()
        path = settings.data_dir / media.path
    engine.dispose()
    assert path.exists()
    return path


@pytest.mark.parametrize("get_me_status", [401, 502])  # a revoked token, Telegram unreachable
async def test_the_purge_runs_when_the_bot_cannot_start(webhook_settings, get_me_status):
    photo = await expired_photo(webhook_settings)
    fake = FakeTelegram(get_me_status=get_me_status)
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:
        assert not client.app.state.telegram.ready
        assert wait_for(lambda: not photo.exists()), "raw media outlived its TTL"


def test_exactly_one_purge_loop_per_process_across_start_retries(webhook_settings, monkeypatch):
    starts = []

    async def counting_purge(settings, engine):
        starts.append(engine)
        await asyncio.Event().wait()

    monkeypatch.setattr(bot, "purge_loop", counting_purge)
    monkeypatch.setattr(telegram_webhook, "START_RETRY_S", 0.01)  # failed starts in a row
    fake = FakeTelegram(get_me_status=502)
    app = webhook_app(webhook_settings, fake)
    with TestClient(app, base_url=f"https://{HOST}") as client:
        telegram = client.app.state.telegram
        assert wait_for(lambda: len([n for n, _ in fake.calls if n == "getMe"]) >= 3)
        assert len(starts) == 1
        purge = telegram.application.bot_data["purge_task"]
    assert purge.cancelled()  # stopped with the app


def test_polling_starts_the_purge_with_the_bot(settings):
    application = bot.build_application(polling_settings(settings), polling=True)
    assert application.post_init is bot.start_upkeep
    webhook = bot.build_application(polling_settings(settings))
    assert webhook.post_init is None  # the web app starts the upkeep itself


# --- Interrupted reports resume at startup -------------------------------------------------------


async def test_a_report_cut_off_by_a_restart_is_assessed_at_the_next_start(
    webhook_settings, monkeypatch
):
    monkeypatch.setattr(bot, "RESUME_PAUSE_S", 0)
    engine = make_engine(webhook_settings)
    init_db(engine)
    intake = IntakeService(webhook_settings, engine)
    queued = await intake.ingest_text(1, "Baha sa Malaban")
    cut_off = await intake.ingest_text(2, "Baha sa Casile")
    with Session(engine) as session:
        row = session.get(Report, cut_off.id)
        row.status, row.assessing_started_at = "assessing", utcnow()
        session.add(row)
        session.commit()
    engine.dispose()

    fake = FakeTelegram(get_me_status=502)  # resuming needs no Telegram
    with TestClient(webhook_app(webhook_settings, fake), base_url=f"https://{HOST}") as client:

        def statuses():
            with Session(client.app.state.engine) as session:
                return {r.id: r.status for r in session.exec(select(Report)).all()}

        assert wait_for(lambda: statuses() == {queued.id: "assessed", cut_off.id: "assessed"})

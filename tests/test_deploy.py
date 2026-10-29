"""Deploy targets and the deploy CLI: secrets stay in .env and Modal, and staging is isolated."""

import json
import stat
from pathlib import Path

import pytest

from app import deploy
from app.deploy_targets import REQUIRED_KEYS, TARGETS, container_env
from app.telegram_webhook import webhook_path
from tests.fake_telegram import FakeTelegram

STAGING = TARGETS["staging"]
PROD = TARGETS["production"]


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "META_API_KEY=meta-value-123\n"
        "TELEGRAM_BOT_TOKEN=111:prod-token-value\n"
        "TELEGRAM_DEV_BOT_TOKEN=222:dev-token-value\n"
        "PSEUDONYM_SALT=prod-salt-value\n",
        encoding="utf-8",
    )
    return path


def test_targets_are_isolated_and_capped():
    assert STAGING.sources["TELEGRAM_BOT_TOKEN"] == "TELEGRAM_DEV_BOT_TOKEN"
    assert PROD.sources["TELEGRAM_BOT_TOKEN"] == "TELEGRAM_BOT_TOKEN"
    for key in ("app", "label", "secret", "volume"):
        assert getattr(STAGING, key) != getattr(PROD, key)
    assert STAGING.sources["PSEUDONYM_SALT"] != PROD.sources["PSEUDONYM_SALT"]
    for target in TARGETS.values():
        assert set(REQUIRED_KEYS) <= set(target.sources)
        env = container_env(target, "ws")
        assert float(env["DAILY_SPEND_CAP_USD"]) <= 10  # the public bot's 24 h spend cap
        assert env["TELEGRAM_MODE"] == "webhook" and env["ALLOWED_HOSTS"] == target.host("ws")
        assert env["DATABASE_PATH"].startswith("/var/") and env["DATA_DIR"] == "/data"
    assert not STAGING.needs_confirmation and PROD.needs_confirmation


def test_secrets_are_generated_once_into_env_and_never_printed(env_file):
    printed = []
    deploy.ensure_generated(STAGING, env_file, printed.append)
    deploy.ensure_generated(STAGING, env_file, printed.append)
    assert len(printed) == 1 and "STAGING_OFFICER_PASSCODE" in printed[0]
    values = deploy.secret_values(STAGING, env_file)
    assert values["TELEGRAM_BOT_TOKEN"] == "222:dev-token-value"  # never production's bot
    assert values["PSEUDONYM_SALT"] != "prod-salt-value"
    assert len(values["OFFICER_PASSCODE"]) >= 12 and len(values["SESSION_SECRET"]) >= 32
    for value in values.values():
        assert value not in printed[0]
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_missing_required_values_are_named_by_their_env_name(tmp_path):
    with pytest.raises(deploy.DeployError, match=r"TELEGRAM_BOT_TOKEN \(.env TELEGRAM_DEV_BOT"):
        deploy.secret_values(STAGING, tmp_path / ".env")


def test_push_secret_hands_values_to_modal_by_file_not_argv(env_file, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["json"] = json.loads(Path(args[args.index("--from-json") + 1]).read_text())

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(deploy, "run_modal", fake_run)
    printed = []
    deploy.push_secret(STAGING, env_file, printed.append)
    assert seen["args"][:4] == ["secret", "create", "--force", "ulat-staging-keys"]
    assert seen["json"]["TELEGRAM_BOT_TOKEN"] == "222:dev-token-value"
    for value in seen["json"].values():
        assert all(value not in arg for arg in seen["args"])
        assert all(value not in line for line in printed)


async def test_webhook_set_registers_the_derived_path_and_secret():
    fake = FakeTelegram()
    secret = "q" * 40
    printed = []
    bot = deploy.Bot("222:dev-token-value", request=fake, get_updates_request=fake)
    await deploy.webhook("set", STAGING, bot, secret, "ws--ulat-staging.modal.run", printed.append)
    (params,) = [p for name, p in fake.calls if name == "setWebhook"]
    assert params["url"] == f"https://ws--ulat-staging.modal.run{webhook_path(secret)}"
    assert params["secret_token"] == secret
    assert params["allowed_updates"] == ["message"]
    assert params["drop_pending_updates"] is False
    assert params["max_connections"] == 1  # one at a time, in order, as polling does
    output = "\n".join(printed)
    assert secret not in output and webhook_path(secret) not in output
    assert "last error: none" in output


def test_production_changes_need_yes(capsys):
    assert deploy.main(["webhook", "set", "--target", "production"]) == 2
    assert deploy.main(["deploy", "--target", "production"]) == 2
    assert "--yes" in capsys.readouterr().err


def test_restore_refuses_a_name_that_is_not_a_snapshot():
    with pytest.raises(deploy.DeployError):
        deploy.restore(STAGING, "../app.db")

"""The bot's public profile: Telegram limits, and an idempotent CLI against a fake Bot."""

import io
import re
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr
from telegram import BotCommand, BotDescription, BotName, BotShortDescription
from telegram.constants import BotCommandLimit, BotDescriptionLimit, BotNameLimit
from telegram.ext import CommandHandler

from app import bot, bot_profile
from app.bot_profile import PROFILE, apply_profile, profile_jpeg


class FakeBot:
    """Stores what was set and returns it from the getters, like the Bot API."""

    def __init__(self, name="", description="", short_description="", commands=()):
        self.values = {
            "name": name,
            "description": description,
            "short_description": short_description,
            "commands": tuple(BotCommand(c, d) for c, d in commands),
        }
        self.setters = []
        self.photos = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_my_name(self):
        return BotName(self.values["name"])

    async def get_my_description(self):
        return BotDescription(self.values["description"])

    async def get_my_short_description(self):
        return BotShortDescription(self.values["short_description"])

    async def get_my_commands(self):
        return self.values["commands"]

    async def set_my_name(self, name):
        self.setters.append("name")
        self.values["name"] = name

    async def set_my_description(self, description):
        self.setters.append("description")
        self.values["description"] = description

    async def set_my_short_description(self, short_description):
        self.setters.append("short_description")
        self.values["short_description"] = short_description

    async def set_my_commands(self, commands):
        self.setters.append("commands")
        self.values["commands"] = tuple(commands)

    async def set_my_profile_photo(self, photo):
        self.setters.append("photo")
        self.photos.append(photo)


def test_profile_fits_telegram_limits():
    assert 0 < len(PROFILE.name) <= BotNameLimit.MAX_NAME_LENGTH
    assert 0 < len(PROFILE.description) <= BotDescriptionLimit.MAX_DESCRIPTION_LENGTH
    assert 0 < len(PROFILE.short_description) <= BotDescriptionLimit.MAX_SHORT_DESCRIPTION_LENGTH
    assert 0 < len(PROFILE.commands) <= BotCommandLimit.MAX_COMMAND_NUMBER
    for command, description in PROFILE.commands:
        assert re.fullmatch(r"[a-z0-9_]+", command)
        assert BotCommandLimit.MIN_COMMAND <= len(command) <= BotCommandLimit.MAX_COMMAND
        assert 0 < len(description) <= BotCommandLimit.MAX_DESCRIPTION


def test_profile_says_what_to_send_and_that_it_is_not_a_hotline():
    text = PROFILE.description
    for words in ("larawan", "voice note", "location", "photos", "Biñan", "911"):
        assert words in text
    assert bot.NOT_A_HOTLINE in text and bot.NOT_A_HOTLINE in bot.start_text(24, 72)
    assert bot.NO_NAMES_STORED in text and bot.NO_NAMES_STORED in bot.start_text(24, 72)
    assert "911" in PROFILE.short_description


# Ulat is a prototype the city has not adopted: the public copy must not read as an LGU service.
OFFICIAL_CLAIMS = (
    "Napupunta ito",
    "go to the City",
    "reach the City",
    "DRRM Office",
    "CDRRMO",
    "for the LGU damage assessment",
    "damage assessment ng LGU",
)


@pytest.mark.parametrize(
    "text",
    [PROFILE.name, PROFILE.short_description, PROFILE.description, bot.start_text(24, 72)],
    ids=["name", "short", "description", "start"],
)
def test_public_copy_is_honest_that_ulat_is_a_prototype(text):
    assert "prototype" in text.lower()
    for claim in OFFICIAL_CLAIMS:
        assert claim not in text


def test_profile_and_start_say_the_city_does_not_monitor_reports_yet():
    for text in (PROFILE.description, bot.start_text(24, 72)):
        assert "not yet official" in text or "not yet an official city service" in text
        assert "hindi pa opisyal o binabantayan ng LGU" in text
    assert "monitored by the city" in PROFILE.description
    assert bot.PROTOTYPE_NOTICE in bot.start_text(24, 72)
    assert "Biñan" not in PROFILE.name  # an LGU-sounding account name is avoided


def test_every_listed_command_has_a_handler(settings):
    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123456:ABC-test")})
    application = bot.build_application(configured)
    handled = {
        command
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {command for command, _ in PROFILE.commands} <= handled


async def test_dry_run_prints_changes_and_sets_nothing():
    fake = FakeBot(name="UlatPH_bot", commands=[("start", "old")])
    lines = []
    assert await apply_profile(fake, dry_run=True, out=lines.append) == 0
    assert fake.setters == []
    printed = "\n".join(lines)
    for field in ("would set name", "would set description", "would set short description"):
        assert field in printed
    assert "UlatPH_bot" in printed and PROFILE.name in printed


async def test_run_sets_only_what_differs_and_a_second_run_sets_nothing():
    fake = FakeBot(name=PROFILE.name, commands=PROFILE.commands)
    await apply_profile(fake, out=lambda _: None)
    assert fake.setters == ["description", "short_description"]
    fake.setters.clear()
    lines = []
    await apply_profile(fake, out=lines.append)
    assert fake.setters == []
    assert lines == ["name, descriptions and commands are already up to date"]


async def test_missing_photo_is_skipped_with_a_clear_message(tmp_path):
    fake = FakeBot(PROFILE.name, PROFILE.description, PROFILE.short_description, PROFILE.commands)
    lines = []
    missing = tmp_path / "ulat-avatar.png"
    assert await apply_profile(fake, photo=missing, out=lines.append) == 0
    assert f"profile photo {missing} not found; skipped" in lines
    assert fake.setters == []


async def test_photo_is_uploaded_as_a_square_jpeg_only_when_asked(tmp_path):
    png = tmp_path / "avatar.png"
    Image.new("RGBA", (300, 200), (0, 90, 160, 128)).save(png)
    fake = FakeBot(PROFILE.name, PROFILE.description, PROFILE.short_description, PROFILE.commands)
    await apply_profile(fake, photo=png, dry_run=True, out=lambda _: None)
    assert fake.photos == []
    await apply_profile(fake, out=lambda _: None)
    assert fake.photos == []
    await apply_profile(fake, photo=png, out=lambda _: None)
    (photo,) = fake.photos
    assert photo.type == "static"
    assert (photo.photo.filename, photo.photo.mimetype) == ("avatar.jpg", "image/jpeg")
    with Image.open(io.BytesIO(profile_jpeg(png))) as image:
        assert (image.format, image.mode, image.size) == ("JPEG", "RGB", (200, 200))


@pytest.mark.parametrize(
    "argv,photo",
    [([], None), (["--photo"], bot_profile.DEFAULT_PHOTO), (["--photo", "x.png"], Path("x.png"))],
)
def test_cli_reads_the_token_from_settings_and_passes_options(monkeypatch, settings, argv, photo):
    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123:fake")})
    monkeypatch.setattr(bot_profile, "get_settings", lambda: configured)
    seen = {}

    async def fake_apply(fake, *, dry_run, photo):
        seen.update(dry_run=dry_run, photo=photo)
        return 0

    monkeypatch.setattr(bot_profile, "apply_profile", fake_apply)
    tokens = []

    def make_bot(token):
        tokens.append(token)
        return FakeBot()

    assert bot_profile.main([*argv, "--dry-run"], make_bot=make_bot) == 0
    assert tokens == ["123:fake"]
    assert seen == {"dry_run": True, "photo": photo}


def test_cli_without_a_token_explains_and_exits(monkeypatch, settings, capsys):
    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("")})
    monkeypatch.setattr(bot_profile, "get_settings", lambda: configured)
    assert bot_profile.main(["--dry-run"], make_bot=lambda token: pytest.fail("no bot")) == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err

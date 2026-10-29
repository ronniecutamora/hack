"""The Telegram bot's public profile as data, and an idempotent CLI that applies it.

    uv run python -m app.bot_profile --dry-run      # show what would change
    uv run python -m app.bot_profile                # apply name, descriptions and commands
    uv run python -m app.bot_profile --photo        # also upload assets/brand/ulat-avatar.png

The Bot API has getters for the name, both descriptions and the commands, so only values that
differ are set. It has no getter for the profile photo, so the photo is uploaded only when
--photo is given.
"""

import argparse
import asyncio
import io
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from telegram import Bot, BotCommand, InputFile, InputProfilePhotoStatic

from app.bot import MISSING_TOKEN_MESSAGE, NO_NAMES_STORED, _with_retries
from app.config import get_settings

# The chosen avatar (branding only; see the README's third-party assets).
DEFAULT_PHOTO = Path(__file__).resolve().parent.parent / "assets" / "brand" / "ulat-avatar.png"
# Telegram shows the photo in a circle; a static profile photo must be a JPEG.
PHOTO_SIDE_PX = 1024


@dataclass(frozen=True)
class BotProfile:
    name: str
    short_description: str  # profile page and link previews
    description: str  # the empty chat's "What can this bot do?"
    commands: tuple[tuple[str, str], ...]


# Taglish first, then English, like /start. Not an emergency line: rescue goes to 911 (no local
# number is printed without an official source).
PROFILE = BotProfile(
    name="Ulat Biñan · Bagyo at Baha",
    short_description=(
        "Mag-report ng pinsala ng bagyo at baha sa Biñan. / "
        "Report typhoon and flood damage in Biñan. Emergency? Call 911."
    ),
    description=(
        "Mag-report ng pinsala ng bagyo at baha sa Biñan, Laguna: magpadala ng larawan, voice "
        "note, text o location. Napupunta ito sa City DRRM Office.\n"
        "Report typhoon and flood damage in Biñan: send photos, voice notes, text or location. "
        "Reports go to the City DRRM Office.\n\n"
        "Hindi ito emergency hotline. Para sa rescue, tumawag sa 911. / "
        "Not an emergency line. For rescue, call 911.\n\n"
        f"{NO_NAMES_STORED}"
    ),
    commands=(
        ("start", "Paano mag-report at privacy / How to report, and privacy"),
        ("help", "Paano mag-report / How to report"),
    ),
)


def profile_jpeg(path: Path) -> bytes:
    """A square JPEG (centre crop, transparency on white) from any image Pillow reads."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, "white")
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
        side = min(PHOTO_SIDE_PX, *image.size)
        square = ImageOps.fit(image.convert("RGB"), (side, side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        square.save(buffer, "JPEG", quality=90)
        return buffer.getvalue()


@dataclass(frozen=True)
class Change:
    field: str
    current: Any
    wanted: Any
    apply: Callable[[], Any]


async def plan(bot: Any, profile: BotProfile) -> list[Change]:
    """Compare the live profile with `profile`; one Change per field that differs."""
    current_name = (await _with_retries(bot.get_my_name)).name
    current_description = (await _with_retries(bot.get_my_description)).description
    current_short = (await _with_retries(bot.get_my_short_description)).short_description
    current_commands = tuple(
        (c.command, c.description) for c in await _with_retries(bot.get_my_commands)
    )
    commands = [BotCommand(command, text) for command, text in profile.commands]
    candidates = [
        Change("name", current_name, profile.name, lambda: bot.set_my_name(profile.name)),
        Change(
            "description",
            current_description,
            profile.description,
            lambda: bot.set_my_description(profile.description),
        ),
        Change(
            "short description",
            current_short,
            profile.short_description,
            lambda: bot.set_my_short_description(profile.short_description),
        ),
        Change(
            "commands", current_commands, profile.commands, lambda: bot.set_my_commands(commands)
        ),
    ]
    return [c for c in candidates if c.current != c.wanted]


async def apply_profile(
    bot: Any,
    profile: BotProfile = PROFILE,
    *,
    dry_run: bool = False,
    photo: Path | None = None,
    out: Callable[[str], None] = print,
) -> int:
    changes = await plan(bot, profile)
    verb = "would set" if dry_run else "set"
    for change in changes:
        out(f"{verb} {change.field}:\n  now:  {change.current!r}\n  new:  {change.wanted!r}")
        if not dry_run:
            await _with_retries(change.apply)
    if not changes:
        out("name, descriptions and commands are already up to date")
    if photo is not None:
        if not photo.is_file():
            out(f"profile photo {photo} not found; skipped")
        elif dry_run:
            out(f"would upload the profile photo {photo} (as a square JPEG)")
        else:
            data = await asyncio.to_thread(profile_jpeg, photo)
            # A .jpg name gives the upload an image/jpeg type (bare bytes are octet-stream).
            upload = InputProfilePhotoStatic(InputFile(data, filename=f"{photo.stem}.jpg"))
            await _with_retries(lambda: bot.set_my_profile_photo(upload))
            out(f"uploaded the profile photo {photo}")
    return 0


async def _cli(args: argparse.Namespace, make_bot: Callable[[str], Any]) -> int:
    token = get_settings().telegram_bot_token.get_secret_value()
    if not token:
        print(MISSING_TOKEN_MESSAGE, file=sys.stderr)
        return 2
    async with make_bot(token) as bot:
        return await apply_profile(bot, dry_run=args.dry_run, photo=args.photo)


def main(argv: list[str] | None = None, make_bot: Callable[[str], Any] = Bot) -> int:
    parser = argparse.ArgumentParser(description="Apply the bot's public Telegram profile.")
    parser.add_argument("--dry-run", action="store_true", help="print changes, set nothing")
    parser.add_argument(
        "--photo",
        nargs="?",
        type=Path,
        const=DEFAULT_PHOTO,
        help=f"also upload a profile photo (default {DEFAULT_PHOTO})",
    )
    return asyncio.run(_cli(parser.parse_args(argv), make_bot))


if __name__ == "__main__":
    sys.exit(main())

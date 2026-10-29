"""Demo commands: `python -m app.demo images|load|replay` (DATA_DIR must name a demo dir)."""

import argparse
from pathlib import Path

from app.config import get_settings


def main(argv: list[str] | None = None) -> int:
    from app.demo.images import images_command
    from app.demo.loader import add_anchor_argument, load_command
    from app.demo.replay import DEFAULT_SEED, DEFAULT_SPEED, replay_command

    parser = argparse.ArgumentParser(description="Synthetic typhoon demo scenario.")
    which = parser.add_subparsers(dest="command", required=True)
    images = which.add_parser("images", help="generate the scenario's photos")
    images.add_argument("--yes", action="store_true", help="generate without asking")
    load = which.add_parser("load", help="feed the scenario through intake and assessment")
    load.add_argument(
        "--reset",
        action="store_true",
        help="delete the demo data (keeps generated images and the model cache)",
    )
    load.add_argument("--yes", action="store_true", help="delete without asking")
    add_anchor_argument(load)
    replay = which.add_parser(
        "replay", help="send the scenario live, at real time, for the dashboard (var/demo-replay)"
    )
    replay.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help=f"scenario seconds per real second (default {DEFAULT_SPEED:g}: ~3 minutes)",
    )
    replay.add_argument(
        "--reset", action="store_true", help="start over (keeps images and the model cache)"
    )
    replay.add_argument("--yes", action="store_true", help="reset without asking")
    replay.add_argument(
        "--allow-paid",
        action="store_true",
        help="allow uncached (paid) model calls instead of refusing",
    )
    replay.add_argument(
        "--seed-from",
        type=Path,
        default=DEFAULT_SEED,
        help=f"copy missing images and cached replies from this demo dir (default {DEFAULT_SEED})",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "images":
        return images_command(settings, assume_yes=args.yes)
    if args.command == "replay":
        return replay_command(
            settings,
            speed=args.speed,
            reset=args.reset,
            assume_yes=args.yes,
            allow_paid=args.allow_paid,
            seed_from=args.seed_from,
        )
    return load_command(settings, reset=args.reset, assume_yes=args.yes, anchor_now=args.anchor_now)


if __name__ == "__main__":
    raise SystemExit(main())

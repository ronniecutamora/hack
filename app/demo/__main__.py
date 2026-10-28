"""Demo commands: `python -m app.demo images|load` (DATA_DIR must end in "demo")."""

import argparse

from app.config import get_settings


def main(argv: list[str] | None = None) -> int:
    from app.demo.images import images_command
    from app.demo.loader import load_command

    parser = argparse.ArgumentParser(description="Synthetic typhoon demo scenario.")
    which = parser.add_subparsers(dest="command", required=True)
    images = which.add_parser("images", help="generate the scenario's photos")
    images.add_argument("--yes", action="store_true", help="generate without asking")
    load = which.add_parser("load", help="feed the scenario through intake and assessment")
    load.add_argument(
        "--reset", action="store_true", help="delete the demo data (keeps generated images)"
    )
    load.add_argument("--yes", action="store_true", help="delete without asking")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "images":
        return images_command(settings, assume_yes=args.yes)
    return load_command(settings, reset=args.reset, assume_yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())

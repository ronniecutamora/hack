"""Call an admin command in a deployed app's live container: `python -m app.deploy reseed|screen`
runs this under the Modal CLI's own Python (it imports only modal and the standard library).

    python -m deploy.admin_call <modal app> <command> [--dry-run]

It looks up the deployed class and calls its `admin` method (deploy/modal_app.py), which runs in
the one container the app has (max_containers=1). Not `modal run` (an ephemeral copy of the app:
a second container and a second SQLite writer) and not `modal container exec` (a separate
process outside the lease and the snapshot lock). Prints the method's lines; exit 0 when it ran.
"""

import argparse
import sys

import modal

from app.deploy_targets import WEB_CLASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", help="the Modal app name, e.g. ulat-staging")
    parser.add_argument("command")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        web = modal.Cls.from_name(args.app, WEB_CLASS)()
        result = web.admin.remote(args.command, dry_run=args.dry_run)
    except modal.exception.Error as exc:
        print(
            f"error: {type(exc).__name__}: {exc} (is a commit with the admin method deployed?)",
            file=sys.stderr,
        )
        return 1
    for line in result["lines"]:
        print(line)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

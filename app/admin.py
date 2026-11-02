"""Admin commands run inside a deployed app's live container (`python -m app.deploy reseed|screen`).

They run in the container that holds the database lease, in the app's own process (a method of
the Modal class, deploy/modal_app.py), so there is never a second SQLite writer and the
snapshot store's lock orders them with the periodic snapshots. Every change is preceded by a
pinned snapshot (app.snapshots: never pruned) whose name is printed, so `python -m app.deploy
restore --target T <name>` undoes it. Output is counts and ids only: never message text,
places or pseudonyms.

- `reseed`: replace the synthetic demo with a fresh `--anchor-now` load (app.reseed).
- `screen`: the free not-a-report rule over every stored message (app.assessment), for the
  messages that arrived before the screen existed.
"""

import logging
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.assessment import screen_existing
from app.config import Settings
from app.db import make_engine
from app.reseed import ReseedRefused, reseed
from app.seed import run_demo_load
from app.snapshots import SnapshotStore

logger = logging.getLogger(__name__)

COMMANDS = ("reseed", "screen")
# One admin command at a time per container: the class serves many inputs at once.
_running = threading.Lock()


class AdminRefused(Exception):
    """The command did not run, or ran and changed nothing; the reason is safe to print."""


def _pinner(store: SnapshotStore, command: str, pinned: list[str]) -> Callable[[], str]:
    """Pins a snapshot named after the command and records its name in `pinned`."""

    def pin() -> str:
        try:
            name = store.pin(f"before-{command}").name
        except (RuntimeError, FileNotFoundError) as exc:
            raise AdminRefused(f"no snapshot could be taken ({exc}); nothing changed") from exc
        pinned.append(name)
        return name

    return pin


def _listed(ids: list[int]) -> str:
    return ", ".join(f"#{i}" for i in ids) or "-"


def _screen(settings: Settings, pin: Callable[[], str], *, dry_run: bool) -> list[str]:
    engine = make_engine(settings)
    try:
        found = screen_existing(engine, dry_run=True)
        if dry_run or not found:
            return [
                f"would set aside {len(found)} message(s) as not reports: {_listed(found)}",
                "dry run: nothing changed, no snapshot taken"
                if dry_run
                else "nothing to set aside: nothing changed, no snapshot taken",
            ]
        lines = [f"snapshot before the change: {pin()}"]
        dropped = screen_existing(engine)
        return [*lines, f"set aside {len(dropped)} message(s) as not reports: {_listed(dropped)}"]
    finally:
        engine.dispose()


def _reseed(
    settings: Settings,
    pin: Callable[[], str],
    images_dir: Path,
    *,
    dry_run: bool,
    load_demo: Callable[[Path], bool] | None,
) -> list[str]:
    if not images_dir.is_dir():
        raise AdminRefused(
            f"no demo photos at {images_dir}: run python -m app.deploy seed-assets first"
        )
    load = load_demo or (lambda work: run_demo_load(settings, images_dir, work))
    with tempfile.TemporaryDirectory(prefix="ulat-reseed-") as scratch:
        try:
            return reseed(
                settings,
                load,
                scratch=Path(scratch),
                before_change=pin,
                dry_run=dry_run,
            )
        except ReseedRefused as exc:
            raise AdminRefused(str(exc)) from exc


def _kept(pinned: list[str]) -> list[str]:
    return [f"snapshot before the change (kept): {name}" for name in pinned]


def run(
    command: str,
    settings: Settings,
    store: SnapshotStore,
    *,
    dry_run: bool = False,
    images_dir: Path,
    demo_allowed: bool = True,
    load_demo: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    """Run one admin command; returns {"ok": bool, "lines": [str]} for the CLI to print. A
    failure after the snapshot rolls its change back; the snapshot stays."""
    if command not in COMMANDS:
        return {"ok": False, "lines": [f"refused: unknown command {command!r}"]}
    if not _running.acquire(blocking=False):
        return {"ok": False, "lines": ["refused: another admin command is running"]}
    pinned: list[str] = []
    pin = _pinner(store, command, pinned)
    try:
        mode = " (dry run)" if dry_run else ""
        logger.info("admin %s%s started", command, mode)
        if not store.holds_lease():
            raise AdminRefused("this container does not hold the database lease")
        if command == "reseed":
            if not demo_allowed:
                raise AdminRefused("this target has no synthetic demo (seed_demo is off)")
            lines = _reseed(settings, pin, images_dir, dry_run=dry_run, load_demo=load_demo)
        else:
            lines = _screen(settings, pin, dry_run=dry_run)
        if not dry_run:
            store.backup()  # the change is durable now, not at the next tick
        for line in lines:
            logger.info("admin %s: %s", command, line)
        return {"ok": True, "lines": [f"{command}{mode}", *lines]}
    except AdminRefused as exc:
        logger.warning("admin %s refused: %s", command, exc)
        return {"ok": False, "lines": [f"refused: {exc}", *_kept(pinned)]}
    except Exception as exc:
        logger.exception("admin %s failed", command)
        return {
            "ok": False,
            "lines": [
                f"failed: {type(exc).__name__} (see the logs); the database change is one "
                "transaction, so it was rolled back unless it had committed; --dry-run shows "
                "the counts now",
                *_kept(pinned),
            ],
        }
    finally:
        _running.release()

"""Seed a fresh deploy with the synthetic typhoon demo, through the real pipeline.

A deploy whose volume has no snapshot yet starts with an empty database. Instead of copying a
database in, this runs the documented demo load (`python -m app.demo load --anchor-now`) in a
scratch demo directory, so every seeded report goes through intake, the models and grouping on
the deploy itself, with times ending an hour before the seed. The generated scenario photos and
the model-response cache come from the volume (uploaded once with `python -m app.deploy
seed-assets`); cached responses cost nothing, and anything new is cached for the next seed.

The finished database and its media are then installed where the app keeps them; Media rows
hold paths relative to DATA_DIR, so they resolve there unchanged. Only synthetic data is used.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import Settings
from app.snapshots import copy_database

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_TIMEOUT_S = 30 * 60
# Deploy-only settings the demo load must not inherit: it uses its own scratch directory.
_SCRUBBED = ("DATABASE_PATH", "SNAPSHOT_DIR", "TELEGRAM_MODE", "ALLOWED_HOSTS", "OFFICER_PASSCODE")


def run_demo_load(
    settings: Settings,
    images_dir: Path,
    work: Path,
    *,
    quiet: bool = False,
    spend_cap_usd: float | None = None,
) -> bool:
    """Run `python -m app.demo load --anchor-now` with `work` (a directory named "demo") as its
    DATA_DIR, reading the volume's demo photos and sharing the deploy's model cache. A database
    already at work/app.db is loaded into (app.reseed prepares one). Returns whether it
    produced work/app.db."""
    if not images_dir.is_dir():
        logger.error("no demo images at %s: run python -m app.deploy seed-assets", images_dir)
        return False
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (work / "images").symlink_to(images_dir, target_is_directory=True)
    (work / "model_cache").symlink_to(settings.cache_dir, target_is_directory=True)
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env["DATA_DIR"] = str(work)
    if spend_cap_usd is not None:
        env["DAILY_SPEND_CAP_USD"] = str(min(settings.daily_spend_cap_usd, spend_cap_usd))
    if not quiet:
        logger.info("loading the synthetic demo (python -m app.demo load --anchor-now)")
    result = subprocess.run(
        [sys.executable, "-m", "app.demo", "load", "--anchor-now"],
        cwd=REPO_ROOT,
        env=env,
        timeout=SEED_TIMEOUT_S,
        capture_output=True,
        text=True,
    )
    if not quiet:
        for line in (result.stdout + result.stderr).splitlines()[-20:]:
            logger.info("demo load: %s", line)
    if result.returncode != 0 or not (work / "app.db").is_file():
        logger.error("demo load failed (exit %s)", result.returncode)
        return False
    return True


def seed_demo(settings: Settings, images_dir: Path) -> bool:
    """Load the demo into a scratch dir and install it as this deploy's data. The database must
    not exist yet. Returns False (and leaves an empty database to the app) when it fails."""
    if settings.db_path.exists():
        raise FileExistsError(f"{settings.db_path} exists; seeding would overwrite it")
    with tempfile.TemporaryDirectory(prefix="ulat-seed-") as scratch:
        work = Path(scratch) / "demo"  # the demo commands only accept a dir named "demo"
        work.mkdir()
        if not run_demo_load(settings, images_dir, work):
            logger.error("seeding failed; starting empty")
            return False
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        copy_database(work / "app.db", settings.db_path, source_read_only=False)
        if (work / "media").is_dir():
            shutil.copytree(work / "media", settings.media_dir, dirs_exist_ok=True)
    logger.info("seeded the synthetic demo")
    return True

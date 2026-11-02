"""Ulat on Modal: one always-on container serving the officer dashboard and the Telegram webhook.

Deploy through the CLI, which picks the target, checks the tree and deploys the committed HEAD:

    uv run python -m app.deploy deploy --target staging

Topology: exactly one container (min_containers=1, max_containers=1) so SQLite has one writer.
It runs the FastAPI app, which in TELEGRAM_MODE=webhook also runs the bot, its assessor and the
retention purge in the same event loop. The live database is on the container's disk; the
volume holds media, the model cache and database snapshots (app.snapshots).

Container lifecycle:
- enter: take the database lease, restore the newest snapshot, or seed the synthetic demo on a
  fresh volume (app.seed); then the app's lifespan snapshots every SNAPSHOT_INTERVAL_MINUTES.
- exit (redeploy, stop, preemption; Modal allows 30 s): the final snapshot, then release the
  lease. The app's lifespan does the same after draining the bot; whichever runs first wins.
- admin (`python -m app.deploy reseed|screen`): a method of the same class, so it runs in this
  container's process, under its lease (app.admin).
"""

import os
from pathlib import Path

import modal

from app.deploy_targets import (
    DEMO_IMAGES_SUBDIR,
    REQUIRED_KEYS,
    TARGETS,
    VOLUME_MOUNT,
    container_env,
)

ROOT = Path(__file__).resolve().parent.parent
try:
    TARGET = TARGETS[os.environ["ULAT_TARGET"]]
    WORKSPACE = os.environ["ULAT_WORKSPACE"]
except KeyError as exc:
    raise SystemExit(
        "Deploy with: uv run python -m app.deploy deploy --target staging|production"
    ) from exc

IGNORE = ["**/__pycache__", "**/*.pyc"]
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")  # voice notes are converted to WAV before transcription
    .uv_sync(str(ROOT))  # locked dependencies from pyproject.toml and uv.lock (no dev group)
    .env(container_env(TARGET, WORKSPACE))
    .add_local_dir(ROOT / "app", "/root/app", ignore=IGNORE)
    .add_local_dir(ROOT / "data", "/root/data", ignore=IGNORE)
)
volume = modal.Volume.from_name(TARGET.volume, create_if_missing=True, version=2)
app = modal.App(TARGET.app, image=image)


@app.cls(
    secrets=[modal.Secret.from_name(TARGET.secret, required_keys=list(REQUIRED_KEYS))],
    volumes={VOLUME_MOUNT: volume},
    min_containers=1,  # always warm: the webhook and the judges never wait for a cold start
    max_containers=1,  # one SQLite writer
    cpu=0.25,
    memory=512,
    timeout=60 * 60,  # per request; a live-update stream reconnects when it ends
    startup_timeout=20 * 60,  # a first start seeds the demo through the real pipeline
)
@modal.concurrent(max_inputs=100)
class Web:  # = app.deploy_targets.WEB_CLASS
    @modal.enter()
    def start(self) -> None:
        from app.config import get_settings
        from app.logs import configure_logging
        from app.seed import seed_demo
        from app.snapshots import SnapshotStore

        self.settings = get_settings()
        configure_logging(self.settings.secret_values())
        self.store = SnapshotStore.from_settings(self.settings, volume)
        fresh = self.store.open() == "empty"
        images = Path(VOLUME_MOUNT) / DEMO_IMAGES_SUBDIR
        if fresh and TARGET.seed_demo and seed_demo(self.settings, images):
            self.store.backup()  # the seed is the first snapshot

    @modal.asgi_app(label=TARGET.label)
    def web(self):
        from app.main import create_app

        return create_app(self.settings, snapshots=self.store)

    @modal.method()
    def admin(self, command: str, dry_run: bool = False) -> dict:
        """An admin command (app.admin: reseed, screen) in this live container, called by
        `python -m app.deploy reseed|screen` through deploy/admin_call.py. It runs here, in the
        process that holds the lease, beside the web app: max_containers=1 means a call can
        never start a second writer, only wait for this one."""
        from app.admin import run

        return run(
            command,
            self.settings,
            self.store,
            dry_run=dry_run,
            images_dir=Path(VOLUME_MOUNT) / DEMO_IMAGES_SUBDIR,
            demo_allowed=TARGET.seed_demo,
        )

    @modal.exit()
    def stop(self) -> None:
        self.store.close()

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
class Web:
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

    @modal.exit()
    def stop(self) -> None:
        self.store.close()

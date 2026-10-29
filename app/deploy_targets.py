"""Where Ulat is deployed, as data: one entry per Modal app.

Standard library only: deploy/modal_app.py imports this under the Modal CLI's own Python, which
does not have the project's dependencies.

Each target has its own app, URL, secret, volume and Telegram bot, so staging can never write to
production's data or take production's bot. Secret values come from the git-ignored .env:
`sources` maps each key the container reads to the .env name that holds its value for this
target, and the names in `generated` are created (random) by `python -m app.deploy secrets` when
.env lacks them.
"""

from dataclasses import dataclass, field

# Container layout: the volume holds media, the model cache, snapshots and the demo photos; the
# live database is on the container's own disk (see app.snapshots).
VOLUME_MOUNT = "/data"
DATABASE_PATH = "/var/lib/ulat/app.db"
SNAPSHOT_SUBDIR = "snapshots"
DEMO_IMAGES_SUBDIR = "demo-images"
MODEL_CACHE_SUBDIR = "model_cache"  # = DATA_DIR/model_cache, the app's cache dir

SHARED_KEYS = ("META_API_KEY", "BASETEN_API_KEY", "FIREWORKS_API_KEY")
REQUIRED_KEYS = (
    "META_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "PSEUDONYM_SALT",
    "OFFICER_PASSCODE",
    "SESSION_SECRET",
)


PER_TARGET_KEYS = ("TELEGRAM_WEBHOOK_SECRET", "OFFICER_PASSCODE", "SESSION_SECRET")


def _sources(prefix: str, bot_token: str, salt: str) -> dict[str, str]:
    return {
        **{key: key for key in SHARED_KEYS},
        "TELEGRAM_BOT_TOKEN": bot_token,
        "PSEUDONYM_SALT": salt,
        **{key: prefix + key for key in PER_TARGET_KEYS},
    }


@dataclass(frozen=True)
class Target:
    name: str
    app: str  # Modal app name
    label: str  # URL: https://<workspace>--<label>.modal.run
    secret: str  # Modal secret name
    volume: str  # Modal volume name
    sources: dict[str, str]  # container key -> .env name
    generated: frozenset[str]  # .env names the secrets command may create
    daily_spend_cap_usd: float  # model spend cap for the public bot (<= 10)
    seed_demo: bool = True  # seed the synthetic demo when the volume has no snapshot
    needs_confirmation: bool = False  # webhook and deploy commands ask for --yes
    env: dict[str, str] = field(default_factory=dict)  # extra non-secret settings

    def host(self, workspace: str) -> str:
        return f"{workspace}--{self.label}.modal.run"


TARGETS = {
    "staging": Target(
        name="staging",
        app="ulat-staging",
        label="ulat-staging",
        secret="ulat-staging-keys",
        volume="ulat-staging-data",
        # The dev bot (@UlatPH_dev_bot) and its own pseudonym salt: nothing shared with prod.
        sources=_sources("STAGING_", "TELEGRAM_DEV_BOT_TOKEN", "STAGING_PSEUDONYM_SALT"),
        generated=frozenset("STAGING_" + key for key in (*PER_TARGET_KEYS, "PSEUDONYM_SALT")),
        daily_spend_cap_usd=2.0,
    ),
    "production": Target(
        name="production",
        app="ulat",
        label="ulat",
        secret="ulat-keys",
        volume="ulat-data",
        # @UlatPH_bot and the existing salt, so a reporter keeps the pseudonym they had.
        sources=_sources("PRODUCTION_", "TELEGRAM_BOT_TOKEN", "PSEUDONYM_SALT"),
        generated=frozenset("PRODUCTION_" + key for key in PER_TARGET_KEYS),
        daily_spend_cap_usd=10.0,
        needs_confirmation=True,
    ),
}


def container_env(target: Target, workspace: str) -> dict[str, str]:
    """The non-secret settings of a target's container (secrets come from its Modal secret)."""
    return {
        "ULAT_TARGET": target.name,
        "ULAT_WORKSPACE": workspace,
        "TELEGRAM_MODE": "webhook",
        "DATA_DIR": VOLUME_MOUNT,
        "DATABASE_PATH": DATABASE_PATH,
        "SNAPSHOT_DIR": f"{VOLUME_MOUNT}/{SNAPSHOT_SUBDIR}",
        "ALLOWED_HOSTS": target.host(workspace),
        "PROXY_HEADERS": "true",
        "MODEL_CACHE": "true",
        "DAILY_SPEND_CAP_USD": str(target.daily_spend_cap_usd),
        **target.env,
    }

"""Settings loaded from the environment or a git-ignored .env file."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.meta.llm import AgentLLMName

# Telegram allows 1-256 of these characters; Ulat asks for at least 32 (it guards the bot).
WEBHOOK_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    meta_api_key: SecretStr = SecretStr("")
    baseten_api_key: SecretStr = SecretStr("")
    fireworks_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    pseudonym_salt: SecretStr = SecretStr("")
    # Officer login (app.auth). Unset on a localhost-only dev server, required anywhere else.
    officer_passcode: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_ttl_hours: int = Field(default=12, gt=0, le=24 * 30)
    # Failed passcodes per client address in a rolling window, then 429 until the window passes.
    login_failures_per_window: int = Field(default=10, gt=0)
    login_window_minutes: int = Field(default=15, gt=0)
    # Behind a TLS-terminating proxy (Modal): take the client address from X-Forwarded-For (the
    # hop the proxy appended) and mark the session cookie Secure. Off when clients connect directly.
    proxy_headers: bool = False

    # "polling": `python -m app.bot` long-polls (local dev). "webhook": the web app also runs the
    # bot and Telegram posts updates to it (app.telegram_webhook); the webhook is registered
    # separately with `python -m app.deploy webhook set`.
    telegram_mode: Literal["polling", "webhook"] = "polling"
    # Telegram echoes this in X-Telegram-Bot-Api-Secret-Token; it also derives the URL path.
    telegram_webhook_secret: SecretStr = SecretStr("")

    # Agent and pair judge share a backend vocabulary; None inherits AGENT_LLM.
    agent_llm: AgentLLMName = "muse-spark-1.3"

    pair_judge_llm: AgentLLMName | None = None
    incident_window_days: int = Field(default=14, gt=0)
    pair_merge_threshold: float = Field(default=0.85, ge=0, le=1)
    # Pair-judge calls per grouping: every candidate up to this many is judged, then the best
    # accepted match wins. Same-barangay candidates beyond it are rare within the window.
    max_judged_candidates: int = Field(default=4, gt=0)

    reports_per_hour: int = Field(default=20, gt=0)
    reports_per_day: int = Field(default=60, gt=0)
    daily_spend_cap_usd: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    photo_max_age_days: int = Field(default=3, ge=0)
    photo_hash_max_distance: int = Field(default=6, ge=0, le=64)

    data_dir: Path = Path("./var")
    # The live SQLite file; default DATA_DIR/app.db. A deploy keeps it on local disk while
    # DATA_DIR (media, model cache) sits on a network volume (see app.snapshots).
    database_path: Path | None = None
    # Online backups of the database (app.snapshots): off unless SNAPSHOT_DIR is set.
    snapshot_dir: Path | None = None
    snapshot_interval_minutes: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    snapshot_keep: int = Field(default=15, gt=0)  # the newest N, plus one per day below
    snapshot_keep_daily: int = Field(default=7, ge=0)
    model_cache: bool = True
    raw_media_ttl_hours: int = 24
    held_media_max_hours: int = Field(default=72, gt=0)
    # Host names the web app answers to (Host header, any port); anything else gets 400. This
    # stops DNS rebinding, where another site's name resolves to this machine. Comma-separated.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default=["localhost", "127.0.0.1"], min_length=1
    )

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value

    @model_validator(mode="after")
    def validate_retention_periods(self) -> Self:
        if self.held_media_max_hours < self.raw_media_ttl_hours:
            raise ValueError("held_media_max_hours must be >= raw_media_ttl_hours")
        return self

    @model_validator(mode="after")
    def validate_webhook_secret(self) -> Self:
        secret = self.telegram_webhook_secret.get_secret_value()
        if secret and not WEBHOOK_SECRET_PATTERN.fullmatch(secret):
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET must be 32-256 characters of A-Z, a-z, 0-9, _ and -"
            )
        if self.telegram_mode == "webhook":
            missing = [
                name.upper()
                for name in ("telegram_webhook_secret", "telegram_bot_token", "pseudonym_salt")
                if not getattr(self, name).get_secret_value()
            ]
            if missing:
                raise ValueError(f"TELEGRAM_MODE=webhook needs {', '.join(missing)}")
        return self

    def secret_values(self) -> list[str]:
        """Every secret this process holds, for log redaction (app.logs)."""
        values = [
            value.get_secret_value()
            for value in (getattr(self, name) for name in type(self).model_fields)
            if isinstance(value, SecretStr)
        ]
        return [v for v in values if v]

    @property
    def db_path(self) -> Path:
        return self.database_path or self.data_dir / "app.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "model_cache"


@lru_cache
def get_settings() -> Settings:
    return Settings()

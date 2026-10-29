"""Settings loaded from the environment or a git-ignored .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.meta.llm import AgentLLMName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    meta_api_key: SecretStr = SecretStr("")
    baseten_api_key: SecretStr = SecretStr("")
    fireworks_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    pseudonym_salt: SecretStr = SecretStr("")

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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "model_cache"


@lru_cache
def get_settings() -> Settings:
    return Settings()

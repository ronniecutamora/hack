"""Settings loaded from the environment or a git-ignored .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    data_dir: Path = Path("./var")
    model_cache: bool = True
    raw_media_ttl_hours: int = 24

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

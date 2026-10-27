"""Settings loaded from the environment or a git-ignored .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    meta_api_key: SecretStr = SecretStr("")
    baseten_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    pseudonym_salt: SecretStr = SecretStr("")

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

"""Shared fixtures. No test may touch the network: HTTP is mocked with respx."""

import pytest

from app.cache import ResponseCache
from app.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        meta_api_key="test-meta-key",
        baseten_api_key="test-baseten-key",
        pseudonym_salt="test-salt",
        data_dir=tmp_path / "var",
        model_cache=True,
        raw_media_ttl_hours=24,
    )


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(tmp_path / "cache", enabled=True)

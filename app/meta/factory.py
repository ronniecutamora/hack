"""Build model clients from Settings, sharing one response cache."""

from app.cache import ResponseCache
from app.config import Settings
from app.meta.base import Recorder
from app.meta.llm import AgentLLM
from app.meta.sam import SamClient
from app.meta.spark import SparkClient
from app.meta.spark_open import SparkOpenClient
from app.meta.transcribe import TranscribeClient


def make_cache(settings: Settings) -> ResponseCache:
    return ResponseCache(settings.cache_dir, enabled=settings.model_cache)


def _meta_kwargs(settings: Settings, recorder: Recorder | None) -> dict:
    return {
        "api_key": settings.meta_api_key.get_secret_value(),
        "cache": make_cache(settings),
        "recorder": recorder,
    }


def make_transcriber(settings: Settings, recorder: Recorder | None = None) -> TranscribeClient:
    return TranscribeClient(**_meta_kwargs(settings, recorder))


def make_spark(settings: Settings, recorder: Recorder | None = None) -> SparkClient:
    return SparkClient(**_meta_kwargs(settings, recorder))


def make_sam(settings: Settings, recorder: Recorder | None = None) -> SamClient:
    return SamClient(**_meta_kwargs(settings, recorder))


def make_spark_open(settings: Settings, recorder: Recorder | None = None) -> SparkOpenClient:
    return SparkOpenClient(
        api_key=settings.baseten_api_key.get_secret_value(),
        cache=make_cache(settings),
        recorder=recorder,
    )


def make_agent_llm(settings: Settings, recorder: Recorder | None = None) -> AgentLLM:
    """The LLM selected by AGENT_LLM."""
    if settings.agent_llm == "muse-spark-open":
        return make_spark_open(settings, recorder)
    return make_spark(settings, recorder)

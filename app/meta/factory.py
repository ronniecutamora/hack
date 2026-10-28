"""Build model clients from Settings, sharing one response cache."""

from app.cache import ResponseCache
from app.config import Settings
from app.meta.base import Recorder
from app.meta.fireworks import FireworksGlimmerClient, FireworksSparkOpenClient
from app.meta.llm import AgentLLM, AgentLLMName
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


# A new backend is one mapping entry; names are constrained by AgentLLMName in Settings.
LLM_BACKENDS = {
    "muse-spark-1.3": (SparkClient, "meta_api_key"),
    "muse-spark-open": (SparkOpenClient, "baseten_api_key"),
    "muse-spark-open-fireworks": (FireworksSparkOpenClient, "fireworks_api_key"),
    "muse-glimmer": (FireworksGlimmerClient, "fireworks_api_key"),
}


def make_llm(settings: Settings, name: AgentLLMName, recorder: Recorder | None = None) -> AgentLLM:
    client, key = LLM_BACKENDS[name]
    return client(
        api_key=getattr(settings, key).get_secret_value(),
        cache=make_cache(settings),
        recorder=recorder,
    )


def make_agent_llm(settings: Settings, recorder: Recorder | None = None) -> AgentLLM:
    return make_llm(settings, settings.agent_llm, recorder)


def make_pair_judge(settings: Settings, recorder: Recorder | None = None) -> AgentLLM:
    return make_llm(settings, settings.pair_judge_llm or settings.agent_llm, recorder)

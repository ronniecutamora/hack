"""Muse Spark Open on Baseten; shared chat behavior lives in openai_compatible."""

from app.meta.openai_compatible import OpenAICompatibleClient

MODEL = "meta-models/muse-spark-open"
URL = "https://inference.baseten.co/v1/chat/completions"


class SparkOpenClient(OpenAICompatibleClient):
    service = "Baseten"
    model = MODEL
    url = URL
    key_env_var = "BASETEN_API_KEY"
    auth_scheme = "Api-Key"

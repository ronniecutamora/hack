"""Muse Spark Open on Baseten; shared chat behavior lives in openai_compatible."""

from app.meta.openai_compatible import OpenAICompatibleClient

MODEL = "meta-models/muse-spark-open"
URL = "https://inference.baseten.co/v1/chat/completions"
# Baseten list price, USD per 1M tokens (input, output): https://www.baseten.co/library/muse-spark-open/
# Paid from Baseten credits, but logged so spend caps see it.
PRICE_PER_M_TOKENS = (1.25, 4.25)


class SparkOpenClient(OpenAICompatibleClient):
    service = "Baseten"
    model = MODEL
    url = URL
    key_env_var = "BASETEN_API_KEY"
    auth_scheme = "Api-Key"
    price_per_m_tokens = PRICE_PER_M_TOKENS

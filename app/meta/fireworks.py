"""Fireworks Meta models; availability errors are explicit and never trigger a fallback."""

from app.meta.base import ModelApiError, error_from_response
from app.meta.openai_compatible import OpenAICompatibleClient


class FireworksSparkOpenClient(OpenAICompatibleClient):
    service = "Fireworks"
    model = "accounts/fireworks/models/muse-spark-open"
    url = "https://api.fireworks.ai/inference/v1/chat/completions"
    key_env_var = "FIREWORKS_API_KEY"
    # USD per 1M tokens (input, output), Fireworks model page:
    # https://fireworks.ai/models/fireworks/muse-spark-open
    price_per_m_tokens = (1.25, 4.25)

    def response_error(self, response) -> ModelApiError:
        error = error_from_response(self.service, response)
        if response.status_code == 404 or any(
            marker in str(error).lower() for marker in ("not found", "not deployed", "not_found")
        ):
            return ModelApiError(
                self.service,
                f"{self.model} is not found or not deployed on Fireworks; "
                "serverless availability must be verified. No model fallback was used.",
                status=response.status_code,
                code=error.code,
            )
        return error


class FireworksGlimmerClient(FireworksSparkOpenClient):
    model = "accounts/fireworks/models/muse-glimmer-30b"
    price_per_m_tokens = None  # not published by Fireworks; a third-party figure is unverified

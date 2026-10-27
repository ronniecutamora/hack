"""Muse Spark 1.3: chat completions with structured (JSON schema) output, optional image input."""

from typing import Any

from app.meta.base import (
    ModelClient,
    ModelOutputError,
    error_from_response,
    json_chat_request,
    parse_json_text,
)
from app.meta.schema import validate

MODEL = "muse-spark-1.3"
URL = "https://api.meta.ai/v1/chat/completions"
# Estimate only: USD per 1M tokens (input, output). Check dev.meta.ai/docs for current prices.
PRICE_PER_M_TOKENS = (1.25, 4.25)


def estimate_cost(usage: dict[str, Any] | None) -> float:
    usage = usage or {}
    price_in, price_out = PRICE_PER_M_TOKENS
    return (
        usage.get("prompt_tokens", 0) * price_in + usage.get("completion_tokens", 0) * price_out
    ) / 1_000_000


class SparkClient(ModelClient):
    service = "Muse Spark"
    model = MODEL  # the AgentLLM backend name, logged with every call

    async def chat_json(
        self,
        *,
        system: str,
        user_text: str,
        schema: dict[str, Any],
        schema_name: str,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
        model: str = MODEL,
    ) -> dict[str, Any]:
        """Ask for output matching a JSON schema and return it parsed and validated.

        Spark reasons before answering (about 1.5k reasoning tokens per call in testing), so
        calls take 10 seconds or more.
        """
        messages, response_format, key_payload = json_chat_request(
            system, user_text, schema, schema_name, image, image_mime
        )
        body = {"model": model, "messages": messages, "response_format": response_format}
        headers = self.auth_headers()

        async def fetch() -> dict[str, Any]:
            response = await self.http.post(URL, headers=headers, json=body)
            if response.status_code != 200:
                raise error_from_response(self.service, response)
            return response.json()

        reply = await self._call(model, key_payload, fetch, lambda r: estimate_cost(r.get("usage")))
        result = extract_json(reply)
        validate(schema, result)
        return result


def extract_json(reply: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON object out of a chat-completions reply."""
    try:
        choice = reply["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelOutputError(f"unexpected Spark reply shape: {str(reply)[:200]}") from exc
    if not text:
        raise ModelOutputError(
            f"Spark returned empty content (finish_reason={choice.get('finish_reason')})"
        )
    parsed = parse_json_text(text, what="Muse Spark")
    if not isinstance(parsed, dict):
        raise ModelOutputError(f"Muse Spark returned JSON that is not an object: {text[:200]!r}")
    return parsed

"""Muse Spark Open (open weights) served on Baseten through an OpenAI-compatible endpoint."""

from dataclasses import dataclass
from typing import Any

from app.meta.base import ModelClient, ModelOutputError, error_from_response, parse_json_text

MODEL = "meta-models/muse-spark-open-1026"
URL = "https://inference.baseten.co/v1/chat/completions"


@dataclass(frozen=True)
class SparkOpenReply:
    content: str
    reasoning: str
    finish_reason: str | None


class SparkOpenClient(ModelClient):
    service = "Baseten"
    key_env_var = "BASETEN_API_KEY"
    auth_scheme = "Api-Key"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float | None = None,
        model: str = MODEL,
    ) -> SparkOpenReply:
        """Plain chat call.

        This is a reasoning model: reasoning tokens count against max_tokens, so leave
        headroom or the answer in `content` comes back empty (finish_reason == "length").
        """
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        headers = self.auth_headers()

        async def fetch() -> dict[str, Any]:
            response = await self.http.post(URL, headers=headers, json=body)
            if response.status_code != 200:
                raise error_from_response(self.service, response)
            return response.json()

        reply = await self._call(model, body, fetch)  # usage is covered by Baseten credits
        try:
            choice = reply["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelOutputError(f"unexpected Baseten reply shape: {str(reply)[:200]}") from exc
        return SparkOpenReply(
            content=(message.get("content") or "").strip(),
            reasoning=(message.get("reasoning_content") or "").strip(),
            finish_reason=choice.get("finish_reason"),
        )

    async def chat_json(
        self, messages: list[dict[str, Any]], *, max_tokens: int = 4096
    ) -> dict[str, Any]:
        """Chat call whose answer must be a JSON object (ask for it in the prompt)."""
        reply = await self.chat(messages, max_tokens=max_tokens)
        if not reply.content:
            raise ModelOutputError(
                f"Spark Open returned no answer (finish_reason={reply.finish_reason}); "
                "raise max_tokens so reasoning does not use the whole budget"
            )
        parsed = parse_json_text(reply.content, what="Muse Spark Open")
        if not isinstance(parsed, dict):
            raise ModelOutputError(
                f"Muse Spark Open returned non-object JSON: {reply.content[:200]!r}"
            )
        return parsed

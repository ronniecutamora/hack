"""Shared OpenAI-compatible chat, structured output, validation and length retry."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.meta.base import (
    ModelClient,
    ModelOutputError,
    error_from_response,
    json_chat_request,
    parse_json_text,
)
from app.meta.schema import validate

# Reasoning tokens count against max_tokens; a photo judgment used ~1,900 completion tokens.
JSON_MAX_TOKENS = 8192
LENGTH_RETRY_FACTOR = 2


@dataclass(frozen=True)
class ChatReply:
    content: str
    reasoning: str
    finish_reason: str | None


class OpenAICompatibleClient(ModelClient):
    model: str
    url: str

    def response_error(self, response):
        return error_from_response(self.service, response)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float | None = None,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
        key_payload: Any = None,
        check: Callable[[ChatReply], None] | None = None,
    ) -> ChatReply:
        """Plain chat call. `check` rejects unusable replies so they are not cached.

        This is a reasoning model: reasoning tokens count against max_tokens, so leave
        headroom or the answer in `content` comes back empty (finish_reason == "length").
        """
        model = model or self.model
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        if response_format is not None:
            body["response_format"] = response_format
        headers = self.auth_headers()

        async def fetch() -> dict[str, Any]:
            response = await self.http.post(self.url, headers=headers, json=body)
            if response.status_code != 200:
                raise self.response_error(response)
            return response.json()

        raw_check = (lambda raw: check(parse_reply(raw, self.service))) if check else None
        # Provider credits and pricing are tracked externally; no price is assumed here.
        raw = await self._call(
            model, body if key_payload is None else key_payload, fetch, check=raw_check
        )
        return parse_reply(raw, self.service)

    async def chat_json(
        self,
        *,
        system: str,
        user_text: str,
        schema: dict[str, Any],
        schema_name: str,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
        max_tokens: int = JSON_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Same contract as SparkClient.chat_json: schema-constrained, parsed and validated."""
        messages, response_format, key_payload = json_chat_request(
            system, user_text, schema, schema_name, image, image_mime
        )
        budgets = (max_tokens, max_tokens * LENGTH_RETRY_FACTOR)
        for budget in budgets:
            try:
                reply = await self.chat(
                    messages,
                    max_tokens=budget,
                    response_format=response_format,
                    key_payload=key_payload,
                    check=lambda r: json_answer(r, schema),
                )
                return json_answer(reply, schema)
            except TruncatedReplyError:
                # Reasoning can use the whole budget (one Taglish note did at 8,192 tokens on
                # 2026-10-27); one retry with a larger budget beats losing the report.
                if budget == budgets[-1]:
                    raise
        raise AssertionError("unreachable")


class TruncatedReplyError(ModelOutputError):
    """The model spent the whole token budget reasoning and gave no answer."""


def parse_reply(raw: dict[str, Any], service: str = "OpenAI-compatible") -> ChatReply:
    try:
        choice = raw["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelOutputError(f"unexpected {service} reply shape: {str(raw)[:200]}") from exc
    return ChatReply(
        content=(message.get("content") or "").strip(),
        reasoning=(message.get("reasoning_content") or "").strip(),
        finish_reason=choice.get("finish_reason"),
    )


def json_answer(reply: ChatReply, schema: dict[str, Any]) -> dict[str, Any]:
    """The reply's JSON answer, validated; raises ModelOutputError if it is unusable."""
    if reply.finish_reason == "length":
        raise TruncatedReplyError("Reply truncated; raise max_tokens and retry.")
    if not reply.content:
        raise ModelOutputError(
            f"Model returned no answer (finish_reason={reply.finish_reason}); "
            "raise max_tokens so reasoning does not use the whole budget"
        )
    parsed = parse_json_text(reply.content, what="Model")
    if not isinstance(parsed, dict):
        raise ModelOutputError(f"Model returned non-object JSON: {reply.content[:200]!r}")
    validate(schema, parsed)
    return parsed

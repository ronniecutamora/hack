"""Shared plumbing for the model clients: HTTP client, errors, retries, cache and call log."""

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.cache import ResponseCache, sha256_hex

# Reads can be slow (SAM took up to ~76 s and Spark ~32 s in testing), connects should not be.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class MetaError(Exception):
    """Base class for model-client errors."""


class MissingApiKeyError(MetaError):
    def __init__(self, env_var: str) -> None:
        super().__init__(f"{env_var} is not set. Add it to .env or the environment.")


class ModelApiError(MetaError):
    """The API answered with an error status, or the request failed in transit."""

    def __init__(
        self, service: str, message: str, *, status: int | None = None, code: str | None = None
    ) -> None:
        self.service = service
        self.status = status
        self.code = code
        label = f"{service} API error"
        if status is not None:
            label += f" (HTTP {status}{f', {code}' if code else ''})"
        super().__init__(f"{label}: {message}")

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS


class ModelOutputError(MetaError):
    """The API answered 200 but the content was not usable (bad JSON, empty, truncated)."""


class UncachedCallError(MetaError):
    """A cache-only client was asked for a reply it has not cached; answering would cost money."""

    def __init__(self, service: str, model: str) -> None:
        self.service, self.model = service, model
        super().__init__(f"{service}: no cached {model} reply, and the client is cache-only")


def error_from_response(service: str, response: httpx.Response) -> ModelApiError:
    """Build a ModelApiError from an already-read error response."""
    code = message = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error", body.get("detail"))
        if isinstance(err, dict):
            code, message = err.get("code"), err.get("message")
        elif isinstance(err, str):
            message = err
    if not message:
        message = response.text[:300] or response.reason_phrase
    return ModelApiError(service, str(message), status=response.status_code, code=code)


@dataclass(frozen=True)
class CallRecord:
    model: str
    latency_ms: int
    cached: bool
    cost_estimate: float


Recorder = Callable[[CallRecord], None]


def token_cost(usage: dict[str, Any] | None, price_per_m_tokens: tuple[float, float]) -> float:
    """USD for a chat reply's `usage` at (input, output) list prices per 1M tokens.

    Cached-input discounts are ignored, so this is an upper bound.
    """
    usage = usage or {}
    price_in, price_out = price_per_m_tokens
    return (
        usage.get("prompt_tokens", 0) * price_in + usage.get("completion_tokens", 0) * price_out
    ) / 1_000_000


class ModelClient:
    """Base class: owns the httpx client and wraps every call with retry, cache and logging."""

    service = "model"
    key_env_var = "META_API_KEY"
    auth_scheme = "Bearer"

    def __init__(
        self,
        *,
        api_key: str,
        http: httpx.AsyncClient | None = None,
        cache: ResponseCache | None = None,
        recorder: Recorder | None = None,
        max_attempts: int = 3,
        backoff_s: float = 1.0,
        cache_only: bool = False,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._owns_http = http is None
        self.cache = cache
        self.recorder = recorder
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        # cache_only answers from the cache and raises UncachedCallError instead of calling
        # the API, so a dry run can prove a job is free. `uncached` lists the models it refused.
        self.cache_only = cache_only
        self.uncached: list[str] = []

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    def auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise MissingApiKeyError(self.key_env_var)
        return {"Authorization": f"{self.auth_scheme} {self._api_key}"}

    async def _call(
        self,
        model: str,
        key_payload: Any,
        fetch: Callable[[], Awaitable[Any]],
        cost: Callable[[Any], float] = lambda _data: 0.0,
        check: Callable[[Any], None] | None = None,
    ) -> Any:
        """Return a JSON-serializable response, from cache when possible.

        `check` raises ModelOutputError for a reply that is unusable (wrong schema, no answer).
        Such replies are never cached, and one found in the cache counts as a miss, so a bad
        answer is not replayed on every re-run.
        """
        started = time.perf_counter()
        if self.cache is not None:
            hit = self.cache.get(model, key_payload)
            if hit is not None and _passes(check, hit):
                self._record(model, started, cached=True, cost=0.0)
                return hit
        if self.cache_only:
            self.uncached.append(model)
            raise UncachedCallError(self.service, model)
        data = await self._with_retry(fetch)
        self._record(model, started, cached=False, cost=cost(data))
        if check is not None:
            check(data)
        if self.cache is not None:
            self.cache.put(model, key_payload, data)
        return data

    async def _with_retry(self, fetch: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(1, self.max_attempts + 1):
            last = attempt == self.max_attempts
            try:
                return await fetch()
            except ModelApiError as exc:
                if last or not exc.retryable:
                    raise
            except httpx.TransportError as exc:
                if last:
                    raise ModelApiError(
                        self.service, f"network failure after {attempt} attempts: {exc!r}"
                    ) from exc
            await asyncio.sleep(self.backoff_s * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _record(self, model: str, started: float, *, cached: bool, cost: float) -> None:
        if self.recorder is not None:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.recorder(CallRecord(model, latency_ms, cached, cost))


def _passes(check: Callable[[Any], None] | None, data: Any) -> bool:
    if check is None:
        return True
    try:
        check(data)
    except ModelOutputError:
        return False
    return True


def parse_json_text(text: str, *, what: str) -> Any:
    """Parse model output that should be JSON; tolerate a Markdown code fence around it."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        return json.loads(cleaned)
    except ValueError as exc:
        raise ModelOutputError(f"{what} did not return valid JSON: {text[:200]!r}") from exc


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def json_chat_request(
    system: str,
    user_text: str,
    schema: dict[str, Any],
    schema_name: str,
    image: bytes | None,
    image_mime: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Messages, response_format and cache-key payload for a schema-constrained chat call.

    Both Spark backends use the OpenAI-style format: an image is an `image_url` content part
    holding a base64 data URL. The cache key uses the image's hash, not its bytes.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image is not None:
        content.append({"type": "image_url", "image_url": {"url": data_url(image, image_mime)}})
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": schema},
    }
    key_payload = {
        "system": system,
        "user_text": user_text,
        "schema": schema,
        "schema_name": schema_name,
        "image": sha256_hex(image) if image is not None else None,
    }
    return messages, response_format, key_payload

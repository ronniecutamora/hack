"""Shared plumbing for the model clients: HTTP client, errors, retries, cache and call log."""

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.cache import ResponseCache

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
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._owns_http = http is None
        self.cache = cache
        self.recorder = recorder
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s

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
    ) -> Any:
        """Return a JSON-serializable response, from cache when possible."""
        started = time.perf_counter()
        if self.cache is not None:
            hit = self.cache.get(model, key_payload)
            if hit is not None:
                self._record(model, started, cached=True, cost=0.0)
                return hit
        data = await self._with_retry(fetch)
        if self.cache is not None:
            self.cache.put(model, key_payload, data)
        self._record(model, started, cached=False, cost=cost(data))
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

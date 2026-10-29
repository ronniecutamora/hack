"""Spend accounting in the model clients: token prices for every backend and cache-only mode."""

import httpx
import pytest
import respx

from app.meta.base import UncachedCallError, token_cost
from app.meta.fireworks import FireworksGlimmerClient, FireworksSparkOpenClient
from app.meta.spark import SparkClient
from app.meta.spark_open import SparkOpenClient

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
ASK = {"system": "s", "user_text": "u", "schema": SCHEMA, "schema_name": "Ok"}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 2000}


def reply(content='{"ok": true}'):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": USAGE,
    }


def test_token_cost_prices_input_and_output():
    assert token_cost(USAGE, (1.25, 4.25)) == pytest.approx(0.00975)
    assert token_cost(None, (1.25, 4.25)) == 0.0


@pytest.mark.parametrize(
    ("client_class", "expected"),
    [
        (SparkOpenClient, 0.00975),  # Baseten list price, same as Spark 1.3
        (FireworksSparkOpenClient, 0.00975),
        (FireworksGlimmerClient, 0.0),  # price not published: logged as unknown (0)
    ],
)
@respx.mock
async def test_open_weight_calls_log_their_token_cost(client_class, expected):
    respx.post(client_class.url).mock(return_value=httpx.Response(200, json=reply()))
    calls = []
    client = client_class(api_key="k", recorder=calls.append, backoff_s=0)
    assert await client.chat_json(**ASK) == {"ok": True}
    assert calls[0].cost_estimate == pytest.approx(expected)
    assert not calls[0].cached


@respx.mock
async def test_cache_only_refuses_a_miss_without_calling_the_api(cache):
    route = respx.post("https://api.meta.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply())
    )
    calls = []
    client = SparkClient(api_key="k", cache=cache, recorder=calls.append, cache_only=True)
    with pytest.raises(UncachedCallError, match="cache-only"):
        await client.chat_json(**ASK)
    assert not route.called and calls == []
    assert client.uncached == ["muse-spark-1.3"]

    client.cache_only = False
    assert await client.chat_json(**ASK) == {"ok": True}  # pays once, fills the cache
    client.cache_only = True
    assert await client.chat_json(**ASK) == {"ok": True}  # now answered from the cache
    assert route.call_count == 1
    assert [c.cached for c in calls] == [False, True]
    assert client.uncached == ["muse-spark-1.3"]


@respx.mock
async def test_cache_only_counts_misses_on_openai_compatible_backends(cache):
    route = respx.post(SparkOpenClient.url).mock(return_value=httpx.Response(200, json=reply()))
    client = SparkOpenClient(api_key="k", cache=cache, cache_only=True)
    for _ in range(2):
        with pytest.raises(UncachedCallError):
            await client.chat_json(**ASK)
    assert not route.called
    assert client.uncached == [SparkOpenClient.model] * 2

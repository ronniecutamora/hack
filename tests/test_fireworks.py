import json
from typing import get_args

import httpx
import pytest
import respx

from app.meta.base import ModelApiError, ModelOutputError
from app.meta.factory import LLM_BACKENDS, make_agent_llm, make_pair_judge
from app.meta.fireworks import FireworksGlimmerClient, FireworksSparkOpenClient
from app.meta.llm import AgentLLMName
from app.meta.spark_open import SparkOpenClient
from tests.test_spark import OK_SCHEMA, completion

URL = FireworksSparkOpenClient.url
KWARGS = dict(system="s", user_text="JSON please", schema=OK_SCHEMA, schema_name="Ok")


@pytest.mark.parametrize("client_type", [SparkOpenClient, FireworksSparkOpenClient])
@respx.mock
async def test_provider_auth_schema_cache_and_length_retry(client_type, cache):
    client = client_type(api_key="test", cache=cache, backoff_s=0)
    route = respx.post(client.url).mock(
        side_effect=[
            httpx.Response(200, json=completion('{"ok": "wrong"}')),
            httpx.Response(200, json=completion('{"ok":', finish="length")),
            httpx.Response(200, json=completion('{"ok": true}')),
            httpx.Response(200, json=completion('{"ok":', finish="length")),
        ]
    )
    with pytest.raises(ModelOutputError):
        await client.chat_json(**KWARGS)
    assert await client.chat_json(**KWARGS) == {"ok": True}
    assert await client.chat_json(**KWARGS) == {"ok": True}
    # The larger-budget answer has its own cache key, as before the client refactor.
    assert route.call_count == 4
    assert route.calls.last.request.headers["authorization"] == f"{client.auth_scheme} test"
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["max_tokens"] for b in bodies] == [8192, 8192, 16384, 8192]
    assert all(b["model"] == client.model for b in bodies)
    assert bodies[-1]["response_format"]["json_schema"]["schema"] == OK_SCHEMA
    await client.aclose()


@respx.mock
async def test_glimmer_unavailable_names_actual_model_without_fallback():
    route = respx.post(URL).mock(
        return_value=httpx.Response(404, json={"error": {"message": "model not deployed"}})
    )
    client = FireworksGlimmerClient(api_key="test", backoff_s=0)
    with pytest.raises(ModelApiError, match="muse-glimmer-30b.*not found or not deployed"):
        await client.chat_json(**KWARGS)
    assert route.call_count == 1
    await client.aclose()


def test_mapping_is_complete_and_judge_default_and_override(settings):
    assert set(get_args(AgentLLMName)) == set(LLM_BACKENDS)
    for name, (client_type, _) in LLM_BACKENDS.items():
        assert isinstance(
            make_agent_llm(settings.model_copy(update={"agent_llm": name})), client_type
        )
    assert make_pair_judge(settings).model == make_agent_llm(settings).model
    overridden = settings.model_copy(update={"pair_judge_llm": "muse-glimmer"})
    assert isinstance(make_pair_judge(overridden), FireworksGlimmerClient)

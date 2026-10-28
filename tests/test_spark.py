import json

import httpx
import pytest
import respx

from app.meta.base import ModelOutputError, parse_json_text
from app.meta.fireworks import FireworksSparkOpenClient
from app.meta.openai_compatible import TruncatedReplyError
from app.meta.spark import SparkClient, extract_json
from app.meta.spark_open import SparkOpenClient

SCHEMA = {
    "type": "object",
    "properties": {"roof_damage": {"type": "string", "enum": ["none", "partial", "severe"]}},
    "required": ["roof_damage"],
    "additionalProperties": False,
}


OK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def completion(content, finish="stop", usage=None):
    return {
        "choices": [
            {"finish_reason": finish, "message": {"role": "assistant", "content": content}}
        ],
        "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 2000},
    }


def test_extract_json_plain_and_fenced():
    assert extract_json(completion('{"roof_damage":"severe"}')) == {"roof_damage": "severe"}
    fenced = completion('```json\n{"roof_damage": "none"}\n```')
    assert extract_json(fenced) == {"roof_damage": "none"}


def test_extract_json_errors_are_clear():
    with pytest.raises(ModelOutputError, match="valid JSON"):
        extract_json(completion("not json at all"))
    with pytest.raises(ModelOutputError, match="empty content"):
        extract_json(completion("", finish="length"))
    with pytest.raises(ModelOutputError, match="not an object"):
        extract_json(completion("[1, 2]"))
    with pytest.raises(ModelOutputError, match="shape"):
        extract_json({"unexpected": True})


def test_parse_json_text_rejects_garbage():
    with pytest.raises(ModelOutputError):
        parse_json_text("```", what="x")


@respx.mock
async def test_chat_json_request_shape_and_parsing():
    route = respx.post("https://api.meta.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion('{"roof_damage":"partial"}'))
    )
    calls = []
    client = SparkClient(api_key="k", recorder=calls.append, backoff_s=0)

    out = await client.chat_json(
        system="judge roofs",
        user_text="Assess this photo.",
        schema=SCHEMA,
        schema_name="Roof",
        image=b"jpegbytes",
    )

    assert out == {"roof_damage": "partial"}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer k"
    body = json.loads(request.content)
    assert body["model"] == "muse-spark-1.3"
    assert body["messages"][0] == {"role": "system", "content": "judge roofs"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "Assess this photo."}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Roof", "schema": SCHEMA},
    }
    # 1000 in + 2000 out tokens at $1.25 / $4.25 per 1M
    assert calls[0].cost_estimate == pytest.approx(0.00975)


@respx.mock
async def test_spark_cache_key_depends_on_image(cache):
    route = respx.post("https://api.meta.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion('{"roof_damage":"none"}'))
    )
    client = SparkClient(api_key="k", cache=cache, backoff_s=0)
    kwargs = {"system": "s", "user_text": "u", "schema": SCHEMA, "schema_name": "Roof"}

    await client.chat_json(image=b"one", **kwargs)
    await client.chat_json(image=b"one", **kwargs)
    await client.chat_json(image=b"two", **kwargs)

    assert route.call_count == 2


@respx.mock
async def test_spark_open_uses_baseten_auth_and_reads_content_after_reasoning():
    reply = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"ok": true}', "reasoning_content": "thinking..."},
            }
        ]
    }
    route = respx.post("https://inference.baseten.co/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply)
    )
    client = SparkOpenClient(api_key="bk", backoff_s=0)

    out = await client.chat_json(
        system="sys", user_text="Return JSON.", schema=OK_SCHEMA, schema_name="Ok", image=b"img"
    )

    request = route.calls.last.request
    assert request.headers["authorization"] == "Api-Key bk"
    body = json.loads(request.content)
    assert body["model"] == "meta-models/muse-spark-open"
    assert body["max_tokens"] == 8192
    assert body["response_format"]["json_schema"] == {"name": "Ok", "schema": OK_SCHEMA}
    image_part = body["messages"][1]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert out == {"ok": True}


@respx.mock
async def test_spark_open_truncated_reasoning_gives_actionable_error():
    reply = {
        "choices": [
            {"finish_reason": "length", "message": {"content": None, "reasoning_content": "hmm"}}
        ]
    }
    respx.post("https://inference.baseten.co/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply)
    )
    client = SparkOpenClient(api_key="bk", backoff_s=0)
    with pytest.raises(ModelOutputError, match="max_tokens"):
        await client.chat_json(
            system="s", user_text="x", schema=OK_SCHEMA, schema_name="Ok", max_tokens=10
        )


@respx.mock
async def test_both_backends_reject_json_that_breaks_the_schema():
    respx.post("https://api.meta.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion('{"roof_damage":"gone"}'))
    )
    bad_open = {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": "yes"}'}}]}
    respx.post("https://inference.baseten.co/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=bad_open)
    )
    with pytest.raises(ModelOutputError, match="roof_damage"):
        await SparkClient(api_key="k", backoff_s=0).chat_json(
            system="s", user_text="u", schema=SCHEMA, schema_name="J"
        )
    with pytest.raises(ModelOutputError, match=r"\$\.ok: expected boolean"):
        await SparkOpenClient(api_key="bk", backoff_s=0).chat_json(
            system="s", user_text="u", schema=OK_SCHEMA, schema_name="Ok"
        )


def test_both_backends_name_their_model():
    assert SparkClient.model == "muse-spark-1.3"
    assert SparkOpenClient.model == "meta-models/muse-spark-open"


@respx.mock
async def test_spark_open_retries_once_with_a_bigger_budget_when_reasoning_runs_out():
    cut = {"choices": [{"finish_reason": "length", "message": {"content": None}}]}
    done = {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}
    route = respx.post("https://inference.baseten.co/v1/chat/completions").mock(
        side_effect=[httpx.Response(200, json=cut), httpx.Response(200, json=done)]
    )
    client = SparkOpenClient(api_key="bk", backoff_s=0)

    out = await client.chat_json(system="s", user_text="u", schema=OK_SCHEMA, schema_name="Ok")

    assert out == {"ok": True}
    budgets = [json.loads(c.request.content)["max_tokens"] for c in route.calls]
    assert budgets == [8192, 16384]


@respx.mock
async def test_invalid_replies_are_not_cached_so_a_rerun_asks_again(cache):
    bad = completion('{"roof_damage":"gone"}')
    good = completion('{"roof_damage":"severe"}')
    route = respx.post("https://api.meta.ai/v1/chat/completions").mock(
        side_effect=[httpx.Response(200, json=bad), httpx.Response(200, json=good)]
    )
    client = SparkClient(api_key="k", backoff_s=0, cache=cache)
    kwargs = {"system": "s", "user_text": "u", "schema": SCHEMA, "schema_name": "J"}

    with pytest.raises(ModelOutputError):
        await client.chat_json(**kwargs)
    assert await client.chat_json(**kwargs) == {"roof_damage": "severe"}
    assert await client.chat_json(**kwargs) == {"roof_damage": "severe"}  # now cached
    assert route.call_count == 2


@respx.mock
async def test_spark_open_does_not_cache_a_truncated_reply(cache):
    cut = {"choices": [{"finish_reason": "length", "message": {"content": None}}]}
    done = {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}
    route = respx.post("https://inference.baseten.co/v1/chat/completions").mock(
        side_effect=[httpx.Response(200, json=cut)] * 2 + [httpx.Response(200, json=done)]
    )
    client = SparkOpenClient(api_key="bk", backoff_s=0, cache=cache)
    kwargs = {"system": "s", "user_text": "u", "schema": OK_SCHEMA, "schema_name": "Ok"}

    with pytest.raises(ModelOutputError, match="max_tokens"):
        await client.chat_json(**kwargs)  # both budgets truncated
    assert await client.chat_json(**kwargs) == {"ok": True}  # asked again, not replayed
    assert route.call_count == 3


@pytest.mark.parametrize("image", [None, b"synthetic image"])
@respx.mock
async def test_spark_open_hits_pre_refactor_cache_key(cache, image):
    from app.cache import sha256_hex

    # Payload used by SparkOpenClient.chat_json at commit 4bcf167.
    payload = {
        "system": "s",
        "user_text": "u",
        "schema": OK_SCHEMA,
        "schema_name": "Ok",
        "image": sha256_hex(image) if image is not None else None,
        "max_tokens": 8192,
    }
    cache.put(SparkOpenClient.model, payload, completion('{"ok": true}'))
    route = respx.post(SparkOpenClient.url).mock(
        return_value=httpx.Response(200, json=completion('{"ok": false}'))
    )
    records = []
    client = SparkOpenClient(api_key="bk", cache=cache, recorder=records.append)
    try:
        assert await client.chat_json(
            system="s", user_text="u", schema=OK_SCHEMA, schema_name="Ok", image=image
        ) == {"ok": True}
        assert route.call_count == 0
        assert len(records) == 1 and records[0].cached
    finally:
        await client.aclose()


@pytest.mark.parametrize("client_type", [SparkOpenClient, FireworksSparkOpenClient])
@respx.mock
async def test_complete_length_reply_is_accepted_and_cached(client_type, cache):
    route = respx.post(client_type.url).mock(
        return_value=httpx.Response(200, json=completion('{"ok": true}', finish="length"))
    )
    client = client_type(api_key="test", cache=cache, backoff_s=0)
    kwargs = dict(system="s", user_text="u", schema=OK_SCHEMA, schema_name="Ok")
    try:
        assert await client.chat_json(**kwargs) == {"ok": True}
        assert await client.chat_json(**kwargs) == {"ok": True}
        assert route.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("client_type", [SparkOpenClient, FireworksSparkOpenClient])
@pytest.mark.parametrize("content", [None, '{"ok":', '{"ok": "wrong"}', "[]"])
@respx.mock
async def test_invalid_length_reply_retries_and_is_not_cached(client_type, content, cache):
    route = respx.post(client_type.url).mock(
        return_value=httpx.Response(200, json=completion(content, finish="length"))
    )
    client = client_type(api_key="test", cache=cache, backoff_s=0)
    try:
        with pytest.raises(TruncatedReplyError, match="max_tokens"):
            await client.chat_json(system="s", user_text="u", schema=OK_SCHEMA, schema_name="Ok")
        assert [json.loads(c.request.content)["max_tokens"] for c in route.calls] == [8192, 16384]
        assert list(cache.root.rglob("*.json")) == []
    finally:
        await client.aclose()

import json

import httpx
import pytest
import respx

from app.meta.base import ModelOutputError, parse_json_text
from app.meta.spark import SparkClient, extract_json
from app.meta.spark_open import SparkOpenClient

SCHEMA = {
    "type": "object",
    "properties": {"roof_damage": {"type": "string", "enum": ["none", "partial", "severe"]}},
    "required": ["roof_damage"],
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

    out = await client.chat_json([{"role": "user", "content": "Return JSON."}], max_tokens=8000)

    request = route.calls.last.request
    assert request.headers["authorization"] == "Api-Key bk"
    body = json.loads(request.content)
    assert body["model"] == "meta-models/muse-spark-open"
    assert body["max_tokens"] == 8000
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
        await client.chat_json([{"role": "user", "content": "x"}], max_tokens=10)

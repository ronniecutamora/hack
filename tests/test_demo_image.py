import base64
import json

import httpx
import pytest
import respx

from app.meta.base import ModelOutputError
from app.meta.image import ImageClient, extract_image

TINY_JPEG = base64.b64encode(b"\xff\xd8fakejpeg").decode()


def reply(b64=TINY_JPEG):
    return {"created": 1784584435, "data": [{"b64_json": b64}], "output_format": "jpeg"}


def test_extract_image_plain_and_errors():
    assert extract_image(reply()) == b"\xff\xd8fakejpeg"
    with pytest.raises(ModelOutputError, match="shape"):
        extract_image({"unexpected": True})
    with pytest.raises(ModelOutputError, match="shape"):
        extract_image({"data": []})
    with pytest.raises(ModelOutputError, match="empty"):
        extract_image({"data": [{"b64_json": ""}]})
    with pytest.raises(ModelOutputError, match="base64"):
        extract_image(reply("!!!not-base64!!!" ))


@respx.mock
async def test_generate_request_shape_and_cost():
    route = respx.post("https://api.meta.ai/v1/images/generations").mock(
        return_value=httpx.Response(200, json=reply())
    )
    calls = []
    client = ImageClient(api_key="k", recorder=calls.append, backoff_s=0)

    out = await client.generate("a flooded street, no people")

    assert out == b"\xff\xd8fakejpeg"
    request = route.calls.last.request
    assert request.headers["authorization"].startswith("Bearer ")
    body = json.loads(request.content)
    assert body == {
        "model": "muse-image-1.0",
        "prompt": "a flooded street, no people",
        "size": "1536x1024",
        "response_format": "b64_json",
        "output_format": "jpeg",
    }
    assert calls[0].model == "muse-image-1.0"
    assert calls[0].cost_estimate == pytest.approx(0.01)


@respx.mock
async def test_generate_results_are_cached_by_prompt(cache):
    route = respx.post("https://api.meta.ai/v1/images/generations").mock(
        return_value=httpx.Response(200, json=reply())
    )
    client = ImageClient(api_key="k", cache=cache, backoff_s=0)

    await client.generate("same prompt")
    await client.generate("same prompt")
    await client.generate("another prompt")

    assert route.call_count == 2


@respx.mock
async def test_invalid_replies_are_not_cached(cache):
    bad = {"data": []}
    route = respx.post("https://api.meta.ai/v1/images/generations").mock(
        side_effect=[httpx.Response(200, json=bad), httpx.Response(200, json=reply())]
    )
    client = ImageClient(api_key="k", cache=cache, backoff_s=0)

    with pytest.raises(ModelOutputError):
        await client.generate("flaky prompt")
    assert await client.generate("flaky prompt") == b"\xff\xd8fakejpeg"
    assert await client.generate("flaky prompt") == b"\xff\xd8fakejpeg"  # now cached
    assert route.call_count == 2


@respx.mock
async def test_http_errors_raise():
    respx.post("https://api.meta.ai/v1/images/generations").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(Exception, match="bad key"):
        await ImageClient(api_key="k", backoff_s=0).generate("a flooded street")

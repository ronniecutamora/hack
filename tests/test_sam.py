import json

import httpx
import pytest
import respx

from app.meta.base import ModelApiError, ModelOutputError
from app.meta.sam import SamClient, parse_stream

# Synthetic stream in the SAM wire format (frame header, then "<id><|box|><|mask|>" records).
# Image is 1000x500. Mask payloads are valid one-bit rasters: 4x2 with 6 of 8 pixels set, and
# 2x2 with 2 of 4 set.
STREAM = (
    "<0f>0<|box;x1=100;y1=200;x2=299;y2=299;w=1000;h=500;c=0.9|>"
    "<|mask;x=0;y=0;c=0.9;data=2,4,!!!!!&vpfC7|>"
    ",1<|box;x1=0;y1=0;x2=99;y2=99;w=1000;h=500;c=0.55|>"
    "<|mask;x=0;y=0;c=0.55;data=2,2,!!!!!&T:u9`|>"
)


def sse(text: str, chunk: int = 40) -> bytes:
    events = [
        {"type": "response.output_text.delta", "delta": text[i : i + chunk]}
        for i in range(0, len(text), chunk)
    ]
    events.append({"type": "response.completed"})
    return ("".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()


def sse_response(text: str) -> httpx.Response:
    return httpx.Response(200, content=sse(text), headers={"content-type": "text/event-stream"})


def test_parse_stream_boxes_confidence_and_area():
    result = parse_stream(STREAM, "roof")

    assert result.image_size == (1000, 500)
    assert result.diagnostics == 0
    assert [round(d.confidence, 2) for d in result.detections] == [0.9, 0.55]
    assert result.detections[0].box == (100, 200, 300, 300)
    # 0.75 of a 200x100 box on a 1000x500 image, and 0.5 of a 100x100 box
    assert result.detections[0].area_fraction == pytest.approx(0.03)
    assert result.detections[1].area_fraction == pytest.approx(0.01)
    assert result.count(0.5) == 2
    assert result.count(0.7) == 1
    assert result.area_fraction(0.7) == pytest.approx(0.03)
    assert result.area_fraction(0.5) == pytest.approx(0.04)


def test_parse_empty_stream_means_nothing_found():
    result = parse_stream("")
    assert result.detections == []
    assert result.count() == 0
    assert result.area_fraction() == 0.0


@respx.mock
async def test_segment_request_shape_and_result():
    route = respx.post("https://api.meta.ai/v1/responses").mock(return_value=sse_response(STREAM))
    client = SamClient(api_key="k", backoff_s=0)

    result = await client.segment(b"jpegbytes", "roof")

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "sam-3.1"
    assert body["stream"] is True
    assert body["metadata"] == {"include_confidence": "true"}
    content = body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "roof"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert route.calls.last.request.headers["accept"] == "text/event-stream"
    assert result.count(0.5) == 2


@respx.mock
async def test_url_not_fetchable_falls_back_to_base64():
    error = httpx.Response(
        400,
        json={"error": {"code": "media_url_not_fetchable", "message": "failed to download media"}},
    )
    route = respx.post("https://api.meta.ai/v1/responses").mock(
        side_effect=[error, sse_response(STREAM)]
    )
    client = SamClient(api_key="k", backoff_s=0)

    result = await client.segment(b"jpegbytes", "roof", image_url="https://example.com/a.jpg")

    refs = [
        json.loads(c.request.content)["input"][0]["content"][1]["image_url"] for c in route.calls
    ]
    assert refs[0] == "https://example.com/a.jpg"
    assert refs[1].startswith("data:image/jpeg;base64,")
    assert result.count() == 2


@respx.mock
async def test_other_http_errors_are_not_masked():
    respx.post("https://api.meta.ai/v1/responses").mock(
        return_value=httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})
    )
    with pytest.raises(ModelApiError) as info:
        await SamClient(api_key="k", backoff_s=0).segment(b"x", "roof", image_url="https://x/y.jpg")
    assert info.value.status == 401


@respx.mock
async def test_failed_stream_event_raises():
    failed = b'data: {"type": "response.failed", "error": {"message": "boom"}}\n\n'
    respx.post("https://api.meta.ai/v1/responses").mock(
        return_value=httpx.Response(200, content=failed)
    )
    with pytest.raises(ModelOutputError, match="boom"):
        await SamClient(api_key="k", backoff_s=0).segment(b"x", "roof")


@respx.mock
async def test_sam_responses_are_cached(cache):
    route = respx.post("https://api.meta.ai/v1/responses").mock(return_value=sse_response(STREAM))
    calls = []
    client = SamClient(api_key="k", cache=cache, recorder=calls.append, backoff_s=0)

    await client.segment(b"img", "roof")
    again = await client.segment(b"img", "roof")
    await client.segment(b"img", "floodwater")  # different concept: new request

    assert route.call_count == 2
    assert again.count() == 2
    assert [c.cached for c in calls] == [False, True, False]
    assert calls[0].cost_estimate == pytest.approx(0.0025)
    assert calls[1].cost_estimate == 0.0

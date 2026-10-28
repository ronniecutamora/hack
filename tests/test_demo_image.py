import base64
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from app.demo.images import (
    LABEL_TEXT,
    build_prompt,
    image_filename,
    jobs_for,
    stamp_label,
)
from app.demo.scenario import ScenarioReport
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
        extract_image(reply("!!!not-base64!!!"))


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


# prompt building, caching by prompt hash, and the honesty label


def gray_jpeg(size=(640, 480)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (128, 128, 128)).save(buffer, "JPEG")
    return buffer.getvalue()


def test_built_prompt_forbids_people_faces_text_logos_and_plates():
    prompt = build_prompt("A flooded street")
    assert "A flooded street" in prompt
    for forbidden in ("people", "faces", "readable text", "logos", "license plates"):
        assert forbidden in prompt.lower()


def test_image_filenames_are_stable_hashes():
    assert image_filename("a") == image_filename("a")
    assert image_filename("a") != image_filename("b")
    assert image_filename("a").endswith(".jpg")


def test_jobs_cover_only_reports_with_photos(tmp_path):
    reports = [
        ScenarioReport("a", "demo-01", 0, 8, "caption", photo_prompt="scene one"),
        ScenarioReport("b", "demo-02", 0, 9, "plain text"),
    ]
    (job,) = jobs_for(reports, tmp_path)
    assert job.report.key == "a"
    assert job.prompt.startswith("scene one")
    assert job.path.parent == tmp_path


def test_stamp_keeps_size_and_marks_the_bottom_left_corner():
    stamped = stamp_label(gray_jpeg())
    image = Image.open(io.BytesIO(stamped)).convert("RGB")
    assert image.size == (640, 480)
    dark = [
        image.getpixel((x, y))
        for x in range(4, 200, 7)
        for y in range(440, 476, 4)
        if sum(image.getpixel((x, y))) < 90
    ]
    assert dark, "the corner region must carry the dark label box"
    plain = Image.open(io.BytesIO(gray_jpeg())).convert("RGB")
    assert all(sum(plain.getpixel((x, y))) >= 90 for x in range(4, 200, 7) for y in (450, 465))


def test_stamp_label_text_names_the_source():
    assert "Muse Image" in LABEL_TEXT and "synthetic" in LABEL_TEXT


async def test_ensure_images_generates_only_missing(tmp_path):
    from app.demo.images import ensure_images

    reports = [
        ScenarioReport("a", "demo-01", 0, 8, "caption", photo_prompt="scene one"),
        ScenarioReport("b", "demo-02", 0, 9, "caption", photo_prompt="scene two"),
    ]
    jobs = jobs_for(reports, tmp_path)
    jobs[0].path.write_bytes(gray_jpeg())  # already cached

    class FakeClient:
        def __init__(self):
            self.prompts = []

        async def generate(self, prompt):
            self.prompts.append(prompt)
            return gray_jpeg()

        async def aclose(self):
            pass

    made = [FakeClient()]
    written = await ensure_images(jobs, lambda: made[0])

    assert written == [jobs[1].path]
    assert made[0].prompts == [jobs[1].prompt]
    stamped = Image.open(io.BytesIO(jobs[1].path.read_bytes()))
    assert stamped.size == (640, 480)

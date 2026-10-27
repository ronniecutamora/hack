import json

import httpx
import pytest
import respx

from app.meta.base import MissingApiKeyError, ModelApiError
from app.meta.transcribe import TranscribeClient

API_REPLY = {
    "sessionId": "s1",
    "transcript": "Bumaha na po sa amin, flooded na ang kalsada.",
    "audioDurationMs": 4200,
    "turns": [
        {
            "turnId": 0,
            "startMs": 0,
            "endMs": 4200,
            "transcript": "Bumaha na po sa amin, flooded na ang kalsada.",
            "speaker": "A",
        }
    ],
}


def make_client(cache=None, recorder=None, key="k"):
    return TranscribeClient(api_key=key, cache=cache, recorder=recorder, backoff_s=0)


@respx.mock
async def test_multipart_request_shape():
    route = respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(200, json=API_REPLY)
    )
    result = await make_client().transcribe(
        b"RIFFfakewav", language_bias=["Tagalog", "English"], keywords=["Biñan"], session_id="abc"
    )

    request = route.calls.last.request
    assert request.url.params["sessionId"] == "abc"
    assert request.headers["authorization"] == "Bearer k"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="request"' in body
    assert b"Content-Type: application/json" in body
    assert b'name="audio"' in body
    assert b"RIFFfakewav" in body
    request_json = json.loads(
        body.split(b'name="request"')[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n--")[0]
    )
    assert request_json == {
        "mode": "DIARIZATION",
        "model": "muse-voice-transcribe-1.0",
        "audioEncoding": "WAV",
        "languageBias": ["Tagalog", "English"],
        "keywords": ["Biñan"],
    }
    assert result.text.startswith("Bumaha")
    assert result.audio_duration_ms == 4200
    assert result.turns[0].speaker == "A"


@respx.mock
async def test_error_is_clear_and_not_retried_for_client_errors():
    route = respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "invalid_audio", "message": "bad wav header"}}
        )
    )
    with pytest.raises(ModelApiError) as info:
        await make_client().transcribe(b"x")
    assert info.value.status == 400
    assert info.value.code == "invalid_audio"
    assert "bad wav header" in str(info.value)
    assert route.call_count == 1


@respx.mock
async def test_retries_on_503_then_succeeds():
    route = respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json=API_REPLY)]
    )
    result = await make_client().transcribe(b"x")
    assert route.call_count == 2
    assert result.text


async def test_missing_key_raises_before_any_request():
    with pytest.raises(MissingApiKeyError, match="META_API_KEY"):
        await make_client(key="").transcribe(b"x")


@respx.mock
async def test_cache_hit_skips_network_and_is_logged(cache):
    route = respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(200, json=API_REPLY)
    )
    calls = []
    client = make_client(cache=cache, recorder=calls.append)

    first = await client.transcribe(b"same audio")
    second = await client.transcribe(b"same audio")

    assert route.call_count == 1
    assert first == second
    assert [c.cached for c in calls] == [False, True]
    assert all(c.model == "muse-voice-transcribe-1.0" for c in calls)

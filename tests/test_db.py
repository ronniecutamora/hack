import httpx
import respx
from sqlmodel import Session, select

from app.db import init_db, make_engine, make_recorder
from app.meta.transcribe import TranscribeClient
from app.models import ModelCall


@respx.mock
async def test_model_calls_are_logged_including_cache_hits(settings, cache):
    engine = make_engine(settings)
    init_db(engine)
    respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(200, json={"transcript": "hi", "audioDurationMs": 10})
    )
    client = TranscribeClient(api_key="k", cache=cache, recorder=make_recorder(engine))

    await client.transcribe(b"audio")
    await client.transcribe(b"audio")

    with Session(engine) as session:
        calls = session.exec(select(ModelCall).order_by(ModelCall.id)).all()
    assert [c.cached for c in calls] == [False, True]
    assert {c.model for c in calls} == {"muse-voice-transcribe-1.0"}
    assert all(c.latency_ms >= 0 for c in calls)

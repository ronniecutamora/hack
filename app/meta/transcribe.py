"""Muse Voice Transcribe: file transcription over REST (multipart WAV upload)."""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.cache import sha256_hex
from app.meta.base import ModelClient, error_from_response

MODEL = "muse-voice-transcribe-1.0"
URL = "https://api.meta.ai/v1/asr/transcribe"
MAX_AUDIO_BYTES = 32 * 1024 * 1024  # documented limit per request
MAX_AUDIO_SECONDS = 10 * 60  # documented limit per request


@dataclass(frozen=True)
class Turn:
    turn_id: int | None
    start_ms: int
    end_ms: int
    transcript: str
    speaker: str | None


@dataclass(frozen=True)
class Transcript:
    text: str
    audio_duration_ms: int
    turns: list[Turn] = field(default_factory=list)

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> "Transcript":
        turns = [
            Turn(
                turn_id=t.get("turnId"),
                start_ms=int(t.get("startMs", 0)),
                end_ms=int(t.get("endMs", 0)),
                transcript=t.get("transcript", ""),
                speaker=t.get("speaker"),
            )
            for t in body.get("turns") or []
        ]
        return cls(
            text=(body.get("transcript") or "").strip(),
            audio_duration_ms=int(body.get("audioDurationMs", 0)),
            turns=turns,
        )


class TranscribeClient(ModelClient):
    service = "Voice Transcribe"

    async def transcribe(
        self,
        wav: bytes,
        *,
        language_bias: list[str] | None = None,
        keywords: list[str] | None = None,
        session_id: str | None = None,
    ) -> Transcript:
        """Transcribe mono 16-bit PCM WAV audio (16 or 24 kHz, at most 10 minutes).

        language_bias is a list of language names, e.g. ["Tagalog", "English"]; it nudges the
        model but does not force a language. keywords biases toward names and terms such as
        barangay names. Both go in the JSON `request` part, as the docs describe.
        """
        if len(wav) > MAX_AUDIO_BYTES:
            raise ValueError(f"audio is {len(wav)} bytes; the limit is {MAX_AUDIO_BYTES}")
        request: dict[str, Any] = {"mode": "DIARIZATION", "model": MODEL, "audioEncoding": "WAV"}
        if language_bias:
            request["languageBias"] = language_bias
        if keywords:
            request["keywords"] = keywords
        headers = self.auth_headers()
        sid = session_id or str(uuid.uuid4())

        async def fetch() -> dict[str, Any]:
            response = await self.http.post(
                URL,
                params={"sessionId": sid},
                headers=headers,
                files={
                    "request": (None, json.dumps(request), "application/json"),
                    "audio": ("audio.wav", wav, "audio/wav"),
                },
            )
            if response.status_code != 200:
                raise error_from_response(self.service, response)
            return response.json()

        body = await self._call(MODEL, {"request": request, "audio": sha256_hex(wav)}, fetch)
        return Transcript.from_api(body)

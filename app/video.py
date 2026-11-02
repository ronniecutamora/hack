"""Bounded ffmpeg extraction for short reporter media; raw video stays temporary."""

import asyncio
import io
import json
import math
import shutil
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

MEDIA_TIMEOUT_S = 45
SILENCE_PEAK = 16  # 16-bit PCM: treat near-digital silence as no audible track


class MediaError(ValueError):
    """The supplied media could not safely be read."""


class MediaTooLong(MediaError):
    """The media exceeds the configured duration."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool
    has_video: bool


async def _run(*args: str) -> bytes:
    executable = shutil.which(args[0])
    if executable is None:
        raise MediaError(f"{args[0]} is not installed")
    process = await asyncio.create_subprocess_exec(
        executable, *args[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=MEDIA_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError) as exc:
        process.kill()
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise MediaError(f"{args[0]} timed out") from exc
    if process.returncode:
        # Do not log ffmpeg's diagnostics: they may include a user's filename or metadata.
        raise MediaError(f"{args[0]} could not read the media")
    return stdout


async def probe(source: Path, *, max_seconds: int, video: bool) -> MediaInfo:
    raw = await _run(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_entries", "format=duration:stream=codec_type,duration",
        "-of", "json", str(source),
    )  # fmt: skip
    try:
        payload = json.loads(raw)
        streams = payload.get("streams", [])
        durations = []
        for value in [
            payload.get("format", {}).get("duration"),
            *(s.get("duration") for s in streams),
        ]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                durations.append(number)
        duration = max(durations)
        kinds = {s.get("codec_type") for s in streams}
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise MediaError("media duration could not be read") from exc
    if duration > max_seconds:
        raise MediaTooLong("media exceeds the duration limit")
    info = MediaInfo(duration, "audio" in kinds, "video" in kinds)
    if (video and not info.has_video) or (not video and not info.has_audio):
        raise MediaError("media has no required stream")
    return info


async def extract_video(
    source: Path, out_dir: Path, info: MediaInfo, count: int
) -> tuple[list[tuple[float, bytes]], bytes | None]:
    """Extract evenly spaced JPEGs and, when present, mono audio; all files are temporary."""
    frames = []
    for index in range(count):
        at = info.duration * (index + 0.5) / count
        target = out_dir / f"frame-{index}.jpg"
        await _run(
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
            "-ss", f"{at:.3f}",
            "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-threads", "1",
            "-vf", "scale=w='min(1280,iw)':h=-2", "-map_metadata", "-1", str(target),
        )  # fmt: skip
        if not target.is_file() or not target.stat().st_size:
            raise MediaError("a video frame could not be extracted")
        frames.append((at, target.read_bytes()))
    audio = None
    if info.has_audio:
        audio = await extract_audio(source, out_dir)
        if not await asyncio.to_thread(audible, audio):
            audio = None
    return frames, audio


def audible(wav: bytes) -> bool:
    """Skip digital/near-digital silence; ambient sound still goes to transcription."""
    with wave.open(io.BytesIO(wav), "rb") as source:
        while chunk := source.readframes(32_000):
            samples = array("h")
            samples.frombytes(chunk)
            if any(abs(sample) > SILENCE_PEAK for sample in samples):
                return True
    return False


async def extract_audio(source: Path, out_dir: Path) -> bytes:
    target = out_dir / "audio.wav"
    await _run(
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
        "-i", str(source), "-map", "0:a:0",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-map_metadata", "-1",
        str(target),
    )  # fmt: skip
    return target.read_bytes()

"""Audio conversion for Voice Transcribe, using the ffmpeg binary.

Telegram voice notes are OGG/Opus. The transcribe API wants mono 16-bit PCM WAV at 16 kHz and
at most 10 minutes per request, so long audio is split into chunks of at most 9.5 minutes.
"""

import asyncio
import shutil
from pathlib import Path

CHUNK_SECONDS = 570  # 9.5 minutes, under the 10-minute request limit


class AudioConversionError(RuntimeError):
    pass


async def to_wav_chunks(
    source: Path, out_dir: Path, *, chunk_seconds: int = CHUNK_SECONDS
) -> list[Path]:
    """Convert any audio file to 16 kHz mono PCM WAV chunks in out_dir, in playback order."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioConversionError("ffmpeg is not installed (macOS: brew install ffmpeg)")
    out_dir.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin", "-v", "error", "-y",
        "-i", str(source),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-map_metadata", "-1",
        "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
        str(out_dir / "chunk_%03d.wav"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    _, stderr = await process.communicate()
    chunks = sorted(out_dir.glob("chunk_*.wav"))
    if process.returncode != 0 or not chunks:
        detail = stderr.decode(errors="replace").strip()[-300:]
        raise AudioConversionError(f"ffmpeg could not convert {source.name}: {detail}")
    return chunks

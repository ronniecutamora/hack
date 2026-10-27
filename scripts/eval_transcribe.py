"""Measure Voice Transcribe quality (WER) on your own labeled recordings.

Usage:
    uv run python -m scripts.eval_transcribe /path/to/folder [--language Tagalog ...]

The folder holds audio files (wav, ogg, m4a, mp3, ...) and a reference transcript with the
same name and a .txt extension (clip1.ogg + clip1.txt). It must live OUTSIDE this repository:
recordings of real people are never committed. Responses are cached, so re-runs are free.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.evaluation import (
    corpus_word_error_rate,
    normalize_text,
    outside_repo,
    word_error_rate,
)
from app.gazetteer import transcription_keywords
from app.intake import transcribe_file
from app.meta.base import MetaError
from app.meta.factory import make_transcriber
from app.transcript_cleanup import clean_transcript

AUDIO_SUFFIXES = {".wav", ".ogg", ".oga", ".opus", ".m4a", ".mp3", ".flac", ".aac", ".webm"}


def find_pairs(folder: Path) -> list[tuple[Path, Path]]:
    pairs = []
    for audio in sorted(folder.iterdir()):
        reference = audio.with_suffix(".txt")
        if audio.suffix.lower() in AUDIO_SUFFIXES and reference.is_file():
            pairs.append((audio, reference))
    return pairs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="folder of audio files with same-name .txt")
    parser.add_argument(
        "--language",
        action="append",
        dest="languages",
        help="language bias, repeatable (default: Tagalog and English)",
    )
    parser.add_argument(
        "--keywords",
        help='comma-separated terms to bias toward, or "barangays" for the gazetteer names',
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    try:
        folder = outside_repo(args.folder)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pairs = find_pairs(folder)
    if not pairs:
        print(f"error: no audio files with a same-name .txt in {folder}", file=sys.stderr)
        return 2

    languages = args.languages or ["Tagalog", "English"]
    if args.keywords == "barangays":
        keywords = transcription_keywords()
    elif args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        keywords = None
    transcriber = make_transcriber(get_settings())
    scored: list[tuple[str, str]] = []
    print(f"{'file':40} {'WER':>6} {'clean':>6}  words")
    try:
        for audio, reference_file in pairs:
            reference = reference_file.read_text(encoding="utf-8").strip()
            if not normalize_text(reference):
                print(f"{audio.name:40} skipped (empty reference)")
                continue
            try:
                hypothesis = await transcribe_file(
                    transcriber, audio, language_bias=languages, keywords=keywords
                )
            except MetaError as exc:
                print(f"{audio.name:40} failed: {exc}")
                continue
            scored.append((reference, hypothesis))
            words = len(normalize_text(reference).split())
            raw_wer = word_error_rate(reference, hypothesis)
            clean_wer = word_error_rate(reference, clean_transcript(hypothesis))
            print(f"{audio.name:40} {raw_wer:6.1%} {clean_wer:6.1%}  {words}")
    finally:
        await transcriber.aclose()

    if not scored:
        print("error: nothing was scored", file=sys.stderr)
        return 1
    total_words = sum(len(normalize_text(ref).split()) for ref, _ in scored)
    overall = corpus_word_error_rate(scored)
    cleaned = corpus_word_error_rate([(ref, clean_transcript(hyp)) for ref, hyp in scored])
    print(f"{'OVERALL':40} {overall:6.1%} {cleaned:6.1%}  {total_words}  ({len(scored)} files)")
    print("clean = after app.transcript_cleanup (repeated-word artifacts removed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

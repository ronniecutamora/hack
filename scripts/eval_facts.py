"""Score fact extraction from voice reports (transcribe -> clean -> agent LLM) against labels.

Usage:
    uv run python -m scripts.eval_facts /path/to/folder [--llm muse-spark-open]
        [--keywords barangays|"Name One,Name Two"] [--only 3,4,5] [--yes]

The folder (OUTSIDE this repository) holds the audio (N.m4a, N.ogg, ...) and facts.json:
{"N": {"field": expected value, ...}} with fields from app.facts.FACTS_SCHEMA. Only the
listed fields are scored: counts and numbers must match (numbers within 0.05), enums and
booleans exactly, lists as sets, and names after normalisation (accents, case, "Sto.").
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.evaluation import outside_repo
from app.facts import extract_facts
from app.gazetteer import normalize, transcription_keywords
from app.intake import LANGUAGE_BIAS, transcribe_file
from app.meta.base import MetaError
from app.meta.factory import make_agent_llm, make_transcriber
from app.transcript_cleanup import clean_transcript

AUDIO_SUFFIXES = (".m4a", ".ogg", ".oga", ".wav", ".mp3", ".opus", ".webm")
TEXT_FIELDS = ("barangay_mentioned", "place_detail")
SPARK_COST_PER_NOTE = 0.01


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--llm", choices=["muse-spark-1.3", "muse-spark-open"])
    parser.add_argument("--keywords", help='"barangays" or comma-separated transcription keywords')
    parser.add_argument("--only", help="comma-separated note ids, e.g. 3,4,5")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    return parser.parse_args(argv)


def matches(field: str, expected: Any, got: Any) -> bool:
    if field in TEXT_FIELDS:
        return (
            expected is None
            and got is None
            or (expected is not None and got is not None and normalize(expected) in normalize(got))
        )
    if isinstance(expected, list):
        return isinstance(got, list) and set(expected) == set(got)
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return expected == got
    return isinstance(got, (int, float)) and abs(float(got) - float(expected)) <= 0.05


def find_audio(folder: Path, note: str) -> Path | None:
    return next(
        (folder / f"{note}{s}" for s in AUDIO_SUFFIXES if (folder / f"{note}{s}").is_file()), None
    )


async def run(args: argparse.Namespace) -> int:
    try:
        folder = outside_repo(args.folder)
        labels: dict[str, dict[str, Any]] = json.loads((folder / "facts.json").read_text("utf-8"))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        labels = {k: v for k, v in labels.items() if k in wanted}
    if args.keywords == "barangays":
        keywords = transcription_keywords()
    else:
        keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()] or None
    settings = get_settings()
    if args.llm:
        settings = settings.model_copy(update={"agent_llm": args.llm})
    llm = make_agent_llm(settings)
    transcriber = make_transcriber(settings)
    on_baseten = settings.agent_llm == "muse-spark-open"
    print(
        f"{len(labels)} notes, extraction by {llm.model}: at most "
        f"${0 if on_baseten else len(labels) * SPARK_COST_PER_NOTE:.2f} of Meta credits plus "
        "transcription (cached runs are free)"
    )
    if not args.yes and input("continue? [y/N] ").strip().lower() != "y":
        return 1

    per_field: defaultdict[str, list[bool]] = defaultdict(list)
    try:
        for note, expected in labels.items():
            audio = find_audio(folder, note)
            if audio is None:
                print(f"{note}: no audio file")
                continue
            try:
                raw = await transcribe_file(
                    transcriber, audio, language_bias=LANGUAGE_BIAS, keywords=keywords
                )
                facts = await extract_facts(llm, clean_transcript(raw))
            except MetaError as exc:
                print(f"{note}: failed: {exc}")
                continue
            misses = []
            for field, value in expected.items():
                ok = matches(field, value, facts.get(field))
                per_field[field].append(ok)
                if not ok:
                    misses.append(f"{field}: expected {value!r}, got {facts.get(field)!r}")
            hits = len(expected) - len(misses)
            print(f"note {note}: {hits}/{len(expected)}" + "".join(f"\n    {m}" for m in misses))
    finally:
        await llm.aclose()
        await transcriber.aclose()

    total = [ok for oks in per_field.values() for ok in oks]
    if not total:
        return 1
    print(f"\n{llm.model}: {sum(total)}/{len(total)} facts right ({sum(total) / len(total):.0%})")
    for field, oks in sorted(per_field.items()):
        print(f"  {field:28} {sum(oks)}/{len(oks)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

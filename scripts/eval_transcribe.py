"""Measure Voice Transcribe on your own labelled recordings: corpus WER and place names heard.

Usage:
    uv run python -m scripts.eval_transcribe /path/to/folder [--only 3-10] [--yes]
        [--record data/eval/results.json --dataset ID] [--spend-cap USD] [--cached-only]
    uv run python -m scripts.eval_transcribe /path/to/folder --language Tagalog --keywords barangays

The folder holds audio files (wav, ogg, m4a, mp3, ...) and a reference transcript with the
same name and a .txt extension (clip1.ogg + clip1.txt), plus optionally facts.json, the labels
scripts.eval_facts reads ({"clip1": {"barangay_mentioned": ..., "place_detail": ...}}), for
place-name accuracy. It must live OUTSIDE this repository: recordings of real people are never
committed, and only aggregate metrics are recorded.

Each note goes through the bot's own path (app.intake.transcribe_file: ffmpeg to 16 kHz mono
WAV, then muse-voice-transcribe-1.0) once per setting in SETTINGS, so every setting is scored
on the same notes. `--language` / `--keywords` run one custom setting instead (not recordable).

Metrics per setting: corpus WER (all word errors over all reference words, not a mean of
per-note rates), raw and after app.transcript_cleanup, and the labelled place names that occur
in the transcript with their spelling. Key-fact accuracy is not measured here: the references
are plain text, and speakers say numbers as words ("seventy", "dalawa't kalahati"), so a fact
can only be read back by the extraction step; scripts.eval_facts measures that end to end.

After confirmation, a free cache-only dry run prices the uncached calls by their audio length;
the estimate is checked against the spend cap (scripts/spend.py), and a run stops paying at the
cap. `--cached-only` scores what the cache holds without paying. Notes left unscored are
recorded as `errors` (n counts the scored ones).
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import eval_results
from app.audio import AudioConversionError
from app.config import get_settings
from app.evaluation import (
    corpus_word_error_rate,
    names_heard,
    normalize_text,
    outside_repo,
    word_error_rate,
)
from app.gazetteer import transcription_keywords
from app.intake import LANGUAGE_BIAS, Transcriber, transcribe_file
from app.meta.base import MetaError, UncachedCallError
from app.meta.factory import make_transcriber
from app.meta.transcribe import MODEL, PRICE_PER_AUDIO_HOUR, Transcript
from app.transcript_cleanup import clean_transcript
from scripts import spend
from scripts.eval_facts import TEXT_FIELDS as PLACE_FIELDS

AUDIO_SUFFIXES = {".wav", ".ogg", ".oga", ".opus", ".m4a", ".mp3", ".flac", ".aac", ".webm"}
SCRIPT = "eval_transcribe"
TASK = "transcribe"
WAV_BYTES_PER_SECOND = 16_000 * 2  # app.audio's output: 16 kHz mono 16-bit PCM
FALLBACK_SECONDS = 60  # cost guess per call when the client cannot dry-run (test fakes)


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    language_bias: list[str] | None
    keywords: str  # "none", "barangays", "barangays+places", or a custom comma list

    def keyword_list(self, places: list[str]) -> list[str] | None:
        if self.keywords == "none":
            return None
        if self.keywords in ("barangays", "barangays+places"):
            names = transcription_keywords()
            extra = places if self.keywords == "barangays+places" else []
            return names + [p for p in extra if p not in names]
        return [k.strip() for k in self.keywords.split(",") if k.strip()] or None


SETTINGS = (
    Setting("no_hint", "No language hint", None, "none"),
    Setting("language_hint", "Language hint (Tagalog, English)", LANGUAGE_BIAS, "none"),
    # What the bot sends (app.intake.IntakeService._transcribe; a test keeps them equal).
    Setting("barangays", "Hint + Biñan's barangay names", LANGUAGE_BIAS, "barangays"),
    # The notes name invented places that stand in for real ones; on this setting they are on
    # the list, as a real barangay (or a purok the CDRRMO adds) would be.
    Setting(
        "barangays_and_places",
        "Hint + barangay names + the notes' own place names",
        LANGUAGE_BIAS,
        "barangays+places",
    ),
)
PRODUCTION = "barangays"


def find_pairs(folder: Path) -> list[tuple[Path, Path]]:
    pairs = []
    for audio in sorted(folder.iterdir(), key=lambda p: _note_order(p.stem)):
        reference = audio.with_suffix(".txt")
        if audio.suffix.lower() in AUDIO_SUFFIXES and reference.is_file():
            pairs.append((audio, reference))
    return pairs


def _note_order(stem: str) -> tuple[int, int, str]:
    return (0, int(stem), "") if stem.isdigit() else (1, 0, stem)


def parse_only(text: str) -> set[str]:
    """Note ids from "3-10" or "3,4,7" (ranges need numeric ids)."""
    wanted: set[str] = set()
    for part in (p.strip() for p in text.split(",")):
        first, dash, last = part.partition("-")
        if dash and first.isdigit() and last.isdigit():
            wanted.update(str(n) for n in range(int(first), int(last) + 1))
        elif part:
            wanted.add(part)
    return wanted


def place_labels(folder: Path) -> dict[str, list[str]]:
    """Labelled place names per note id, from the folder's facts.json (empty when absent)."""
    path = folder / "facts.json"
    if not path.is_file():
        return {}
    labels = json.loads(path.read_text(encoding="utf-8"))
    return {
        note: [facts[f] for f in PLACE_FIELDS if isinstance(facts.get(f), str) and facts[f]]
        for note, facts in labels.items()
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="folder of audio files with same-name .txt")
    parser.add_argument("--only", help='note ids to score, e.g. "3-10" or "3,4,7"')
    parser.add_argument(
        "--language",
        action="append",
        dest="languages",
        help="run one custom setting: language bias, repeatable",
    )
    parser.add_argument(
        "--keywords",
        help='run one custom setting: "barangays" for the gazetteer names, or comma-separated',
    )
    parser.add_argument("--yes", action="store_true", help="skip the paid-call confirmation")
    parser.add_argument("--record", type=Path, metavar="RESULTS", help="upsert into this file")
    parser.add_argument("--dataset", help="dataset id for --record (described in that file)")
    parser.add_argument(
        "--cached-only", action="store_true", help="score only what is cached; never pay"
    )
    spend.add_cap_argument(parser)
    return parser.parse_args(argv)


def settings_for(args: argparse.Namespace) -> tuple[Setting, ...]:
    if args.languages is None and args.keywords is None:
        return SETTINGS
    if args.record:
        raise ValueError("--record scores the standard settings; drop --language/--keywords")
    keywords = args.keywords or "none"
    return (Setting("custom", "Custom", args.languages or LANGUAGE_BIAS, keywords),)


class _Metered:
    """Passes calls through and adds up the audio seconds of the ones the cache cannot answer."""

    def __init__(self, inner: Transcriber) -> None:
        self.inner, self.uncached_seconds = inner, 0.0

    async def transcribe(self, wav: bytes, **kwargs: Any) -> Transcript:
        try:
            return await self.inner.transcribe(wav, **kwargs)
        except UncachedCallError:
            self.uncached_seconds += len(wav) / WAV_BYTES_PER_SECOND
            raise


@dataclass
class Scored:
    """One setting's transcripts of the scored notes, in note order."""

    setting: Setting
    keyword_count: int
    pairs: list[tuple[str, str]]  # (reference, hypothesis)
    heard: int = 0
    places: int = 0


async def evaluate(
    notes: list[tuple[str, str, Path]],
    settings: tuple[Setting, ...],
    labels: dict[str, list[str]],
    transcriber: Transcriber,
    stop: Callable[[], bool] = lambda: False,
    echo: Callable[[str], None] = print,
) -> tuple[list[Scored], int, int]:
    """Transcribe every (id, reference, audio) note under every setting; print per-note WERs.

    A note is scored only if every setting transcribed it, so the settings stay comparable.
    Returns the per-setting results, the notes that failed and the notes skipped (spend cap
    reached, or not cached in a cache-only run).
    """
    all_places = [p for note, _, _ in notes for p in labels.get(note, [])]
    scored = [Scored(s, len(s.keyword_list(all_places) or []), []) for s in settings]
    failed = skipped = 0
    echo(f"{'note':12} " + " ".join(f"{s.name[:20]:>20}" for s in settings) + "  words")
    for note, reference, audio in notes:
        if stop():
            skipped += 1
            echo(f"{note:12} skipped: the spend cap was reached")
            continue
        # Every setting is tried even after one fails, so a dry run prices all the misses.
        hypotheses: list[str] = []
        problems: list[Exception] = []
        for s in settings:
            try:
                hypotheses.append(
                    await transcribe_file(
                        transcriber,
                        audio,
                        language_bias=s.language_bias,
                        keywords=s.keyword_list(all_places),
                    )
                )
            except (MetaError, AudioConversionError) as exc:
                problems.append(exc)
        real = [p for p in problems if not isinstance(p, UncachedCallError)]
        if real:
            failed += 1
            echo(f"{note:12} failed: {real[0]}")
            continue
        if problems:
            skipped += 1
            echo(f"{note:12} skipped: not cached")
            continue
        places = labels.get(note, [])
        for result, hypothesis in zip(scored, hypotheses, strict=True):
            result.pairs.append((reference, hypothesis))
            result.heard += names_heard(places, hypothesis)
            result.places += len(places)
        rates = " ".join(f"{word_error_rate(reference, h):20.1%}" for h in hypotheses)
        heard = (
            f"  places {'/'.join(str(names_heard(places, h)) for h in hypotheses)} of {len(places)}"
            if places
            else ""
        )
        echo(f"{note:12} {rates}  {len(normalize_text(reference).split())}{heard}")
    return scored, failed, skipped


def metrics_of(scored: list[Scored], production: str) -> dict[str, Any]:
    references = [reference for reference, _ in scored[0].pairs]
    settings = {}
    for result in scored:
        s = result.setting
        settings[s.name] = {
            "label": s.label,
            "language_bias": s.language_bias,
            "keywords": s.keywords,
            "keyword_count": result.keyword_count,
            "wer": round(corpus_word_error_rate(result.pairs), 4),
            "wer_clean": round(
                corpus_word_error_rate([(r, clean_transcript(h)) for r, h in result.pairs]), 4
            ),
            "place_names_heard": result.heard if result.places else None,
            "place_names": result.places or None,
        }
    return {
        "reference_words": sum(len(normalize_text(r).split()) for r in references),
        "production": production,
        "settings": settings,
    }


def print_summary(metrics: dict[str, Any], n: int) -> None:
    print(f"\n{n} notes, {metrics['reference_words']} reference words")
    print(f"{'setting':22} {'WER':>6} {'clean':>6}  places  keywords")
    for name, s in metrics["settings"].items():
        places = f"{s['place_names_heard']}/{s['place_names']}" if s["place_names"] else "-"
        mark = "  (the bot)" if name == metrics["production"] else ""
        print(
            f"{name:22} {s['wer']:6.1%} {s['wer_clean']:6.1%}  {places:>6}  "
            f"{s['keyword_count']}{mark}"
        )
    print("clean = after app.transcript_cleanup (repeated-word artifacts removed)")


async def estimate_cost(
    notes: list[tuple[str, str, Path]],
    settings: tuple[Setting, ...],
    labels: dict[str, list[str]],
    transcriber: Transcriber,
) -> tuple[float, str]:
    """USD for the calls the cache cannot answer, priced by their audio length (free dry run)."""
    calls = len(notes) * len(settings)
    if not spend.probe_ready(transcriber):
        seconds = calls * FALLBACK_SECONDS
        return seconds / 3600 * PRICE_PER_AUDIO_HOUR, f"{calls} call(s), not dry-run"
    metered = _Metered(transcriber)
    with spend.probe(transcriber):
        await evaluate(notes, settings, labels, metered, echo=lambda _line: None)
    seconds = metered.uncached_seconds
    return seconds / 3600 * PRICE_PER_AUDIO_HOUR, (
        f"{spend.misses(transcriber)} uncached call(s), {seconds:.0f} s of audio"
    )


def dataset_for(args: argparse.Namespace, notes: int) -> str | None:
    if not args.record:
        return None
    if not args.dataset:
        raise ValueError("--record needs --dataset (the id that describes these recordings)")
    raw = eval_results.read_raw(args.record)
    dataset = raw["datasets"].get(args.dataset)
    if dataset is None:
        raise ValueError(f"dataset {args.dataset!r} is not described in {args.record}")
    if dataset["n"] != notes:
        raise ValueError(f"dataset {args.dataset!r} has {dataset['n']} notes; {notes} selected")
    return args.dataset


async def run(args: argparse.Namespace) -> int:
    try:
        folder = outside_repo(args.folder)
        settings = settings_for(args)
        labels = place_labels(folder)
        pairs = find_pairs(folder)
        if args.only:
            wanted = parse_only(args.only)
            pairs = [(a, r) for a, r in pairs if a.stem in wanted]
        if not pairs:
            raise ValueError(f"no audio files with a same-name .txt in {folder}")
        dataset = dataset_for(args, len(pairs))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    notes = []
    for audio, reference_file in pairs:
        reference = reference_file.read_text(encoding="utf-8").strip()
        if normalize_text(reference):
            notes.append((audio.stem, reference, audio))
        else:
            print(f"{audio.name}: skipped (empty reference)")
    if not notes:
        print("error: nothing to score", file=sys.stderr)
        return 1
    if not any(labels.get(note) for note, _, _ in notes):  # no place labels: same as "barangays"
        settings = tuple(s for s in settings if s.keywords != "barangays+places")
    app_settings = get_settings()
    print(
        f"{len(notes)} notes x {len(settings)} setting(s) on Voice Transcribe; cached calls are "
        f"free, the rest cost about ${PRICE_PER_AUDIO_HOUR / 60:.3f} per minute of audio."
    )
    if not args.yes and input("Spend credits on uncached calls? [y/N] ").strip().lower() != "y":
        return 1
    transcriber = make_transcriber(app_settings)
    ledger = spend.SpendLedger(app_settings.data_dir, args.spend_cap, script=SCRIPT)
    spend.attach(ledger, transcriber)
    try:
        if args.cached_only:
            with spend.probe(transcriber):
                scored, failed, skipped = await evaluate(notes, settings, labels, transcriber)
        else:
            estimate, detail = await estimate_cost(notes, settings, labels, transcriber)
            if not ledger.approve(estimate, detail):
                return 1
            scored, failed, skipped = await evaluate(
                notes, settings, labels, transcriber, ledger.over_cap
            )
    finally:
        await transcriber.aclose()
        ledger.close()
    print(f"logged spend for this run: ${ledger.run_spend:.4f}")
    n = len(scored[0].pairs)
    if not n:
        print("error: nothing was scored", file=sys.stderr)
        return 1
    names = [s.name for s in settings]
    metrics = metrics_of(scored, PRODUCTION if PRODUCTION in names else names[0])
    print_summary(metrics, n)
    if failed:
        print(f"{failed} note(s) failed" + ("; not recorded" if dataset else ""))
        return 1
    if dataset is not None:
        record(args.record, dataset, transcriber, metrics, n, skipped, app_settings.data_dir)
    return 0


def record(
    path: Path,
    dataset: str,
    transcriber: Any,
    metrics: dict[str, Any],
    n: int,
    skipped: int,
    data_dir: Path,
) -> None:
    key = (TASK, dataset, MODEL)
    recorded = next((e.spend_usd for e in eval_results.load(path).entries if e.key == key), 0.0)
    entry = eval_results.upsert(
        path,
        {
            "task": TASK,
            "dataset": dataset,
            "model": MODEL,
            "model_id": MODEL,
            "backend": eval_results.backend_of(transcriber),
            "sam": None,
            "date": eval_results.manila_today(),
            "commit": eval_results.git_commit(),
            "n": n,
            "errors": skipped,
            # Every logged run of this script, minus what an earlier record holds.
            "spend_usd": max(spend.script_spend(data_dir, SCRIPT) - recorded, 0),
            "metrics": metrics,
        },
    )
    print(f"recorded {TASK}/{dataset}/{entry['model']} in {path}")


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

"""Measure how often Spark and SAM 3.1 disagree on a labeled photo set, and who is right then.

Usage:
    uv run python -m scripts.eval_crosscheck /path/to/folder --results out.json
        --model muse-spark-1.3 [--record data/eval/results.json --dataset ID]

Free: no model is called. `--results` is the per-photo output of
`scripts.eval_vision <folder> --sam --out out.json` (Spark's judgment and SAM's measurement for
each photo, from the cached calls); the folder (OUTSIDE this repository) holds its labels.csv.
Every rule in app.crosscheck.CHECKS is applied to every photo, the rules that ask the officer
and the ones that are only measured alike, and each disagreement is scored against the hand
label: floodwater against the `floodwater` column, roof damage against `roof_damage` (partial
or severe). --record writes a "crosscheck" entry into the results file (app/eval_results.py),
with the model id and backend of the photo_judge entry the outputs came from.
"""

import argparse
import csv
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import crosscheck, eval_results
from app.evaluation import outside_repo
from app.meta.sam import MODEL as SAM_MODEL
from app.photo import FLOOD_LOW_THRESHOLD, FLOOD_MIN_AREA, FLOOD_THRESHOLD, ROOF_THRESHOLD

THRESHOLDS = {
    "roof": ROOF_THRESHOLD,
    "floodwater": FLOOD_THRESHOLD,
    "floodwater_low": FLOOD_LOW_THRESHOLD,
    "floodwater_min_area": FLOOD_MIN_AREA,
}
# What the hand label says about each check's concept: is the thing there?
LABELS: dict[str, Callable[[dict[str, str]], bool]] = {
    "floodwater": lambda row: row["floodwater"] == "1",
    "roof_damage": lambda row: row["roof_damage"] in ("partial", "severe"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="the labeled set (labels.csv)")
    parser.add_argument("--results", type=Path, required=True, help="eval_vision --out JSON")
    parser.add_argument("--model", required=True, choices=["muse-spark-1.3", "muse-spark-open"])
    parser.add_argument("--record", type=Path, metavar="RESULTS", help="upsert into this file")
    parser.add_argument("--dataset", help="dataset id in the results file (with --record)")
    return parser.parse_args(argv)


def score(photos: list[tuple[dict[str, str], dict[str, Any]]]) -> dict[str, Any]:
    """The metrics the results file stores: per rule, how often it fired and who was right."""
    checks = {
        c.code: {"asks": c.asks, "fired": 0, "spark_right": 0, "sam_right": 0}
        for c in crosscheck.CHECKS
    }
    asked = disagreed = 0
    for row, result in photos:
        fired = crosscheck.disagreements(
            result["judgment"], result["measurement"], asked_only=False
        )
        asked += any(c.asks for c in fired)
        disagreed += bool(fired)
        for check in fired:
            counts = checks[check.code]
            counts["fired"] += 1
            # The two disagree on a yes/no, so exactly one of them matches the label.
            spark_right = LABELS[check.concept](row) == check.spark_sees
            counts["spark_right" if spark_right else "sam_right"] += 1
    return {"photos": len(photos), "disagreed": disagreed, "asked": asked, "checks": checks}


def load(folder: Path, results: Path) -> list[tuple[dict[str, str], dict[str, Any]]]:
    """Each scored photo's label row and its judgment and measurement (failed photos left out)."""
    with (folder / "labels.csv").open(encoding="utf-8") as f:
        labels = {row["file"]: row for row in csv.DictReader(f)}
    photos = []
    for result in json.loads(results.read_text(encoding="utf-8")):
        if "error" in result:
            continue
        if "measurement" not in result:
            raise ValueError(f"{result['file']}: no SAM measurement; run eval_vision with --sam")
        if result["measurement"].get("thresholds") != THRESHOLDS:
            raise ValueError(
                f"{result['file']}: measured at other thresholds than app.photo's; "
                "re-run eval_vision --sam --out (the calls are cached)"
            )
        if result["file"] not in labels:
            raise ValueError(f"{result['file']}: not in {folder / 'labels.csv'}")
        photos.append((labels[result["file"]], result))
    return photos


def print_metrics(metrics: dict[str, Any]) -> None:
    print(
        f"photos {metrics['photos']}, disagreed on {metrics['disagreed']}, "
        f"asked the officer about {metrics['asked']}"
    )
    for check in crosscheck.CHECKS:
        m = metrics["checks"][check.code]
        mode = "asks    " if check.asks else "measured"
        print(
            f"{mode} {check.code:24} fired {m['fired']:3}  "
            f"Spark right {m['spark_right']:3}  SAM right {m['sam_right']:3}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        folder = outside_repo(args.folder)
        photos = load(folder, args.results)
        source = None
        if args.record:
            if not args.dataset:
                raise ValueError("--record needs --dataset (an id the results file describes)")
            source = eval_results.load(args.record).find("photo_judge", args.dataset, args.model)
            if source is None or source.sam is None:
                raise ValueError(f"no photo_judge/{args.dataset}/{args.model} entry with SAM")
            if source.n != len(photos):
                raise ValueError(f"{len(photos)} photos here, {source.n} in the photo_judge entry")
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not photos:
        print("error: no scored photos in the results", file=sys.stderr)
        return 2
    metrics = score(photos)
    print_metrics(metrics)
    if source is not None:
        stored = eval_results.upsert(
            args.record,
            {
                "task": "crosscheck",
                "dataset": args.dataset,
                "model": args.model,
                "model_id": source.model_id,
                "backend": source.backend,
                "sam": SAM_MODEL,
                "date": eval_results.manila_today(),
                "commit": eval_results.git_commit(),
                "n": len(photos),
                "errors": source.errors,
                "spend_usd": 0.0,  # no model call: the judgments and measurements are reused
                "metrics": metrics,
            },
        )
        print(f"recorded crosscheck/{args.dataset}/{stored['model']} in {args.record}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

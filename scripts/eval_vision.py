"""Score the photo judge (and optionally SAM measurements) against hand-labelled photos.

Usage:
    uv run python -m scripts.eval_vision /path/to/folder [--llm muse-spark-open] [--sam]
        [--limit N] [--yes] [--out results.json] [--record data/eval/results.json --dataset ID]
        [--spend-cap USD]

The folder (OUTSIDE this repository) holds labels.csv and an images/ folder. labels.csv has a
`file` column and, per photo, 0/1 columns roof, fallen_tree, floodwater, debris, tarpaulin and
a roof_damage column (none | partial | severe | unknown); other columns are ignored. Responses
are cached, so re-runs are free. A free cache-only dry run finds the uncached calls first; their
estimated cost is printed and checked against the spend cap (see scripts/spend.py) before
anything is paid for. --record writes the metrics into the results file (app/eval_results.py)
under a dataset id that file already describes.
"""

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app import eval_results
from app.config import get_settings
from app.evaluation import binary_scores, cohen_kappa, outside_repo
from app.meta.factory import make_agent_llm, make_sam
from app.meta.sam import MODEL as SAM_MODEL
from app.meta.sam import PRICE_PER_REQUEST as SAM_PRICE_PER_REQUEST
from app.photo import SAM_CONCEPTS, judge, measure
from scripts import spend

FLAGS = ("floodwater", "debris", "fallen_tree", "tarpaulin")
SPARK_COST_PER_PHOTO = 0.012  # measured 2026-10-27 (about 1k prompt + 2.5k output tokens)
SAM_COST_PER_PHOTO = SAM_PRICE_PER_REQUEST * len(SAM_CONCEPTS)
CONCURRENCY = 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--llm", choices=["muse-spark-1.3", "muse-spark-open"])
    parser.add_argument("--sam", action="store_true", help="also score SAM roof/floodwater")
    parser.add_argument("--limit", type=int, help="only the first N photos")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--out", type=Path, help="write per-photo results as JSON here")
    parser.add_argument("--record", type=Path, metavar="RESULTS", help="upsert into this file")
    parser.add_argument("--dataset", help="dataset id in the results file (with --record)")
    spend.add_cap_argument(parser)
    return parser.parse_args(argv)


def load_labels(folder: Path, limit: int | None) -> list[dict[str, str]]:
    with (folder / "labels.csv").open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (folder / "images" / r["file"]).is_file()]
    return rows[:limit] if limit else rows


def record_problem(args: argparse.Namespace) -> str | None:
    if not args.record:
        return None
    if args.limit:
        return "--record needs the whole dataset; drop --limit"
    if not args.dataset:
        return "--record needs --dataset (an id the results file describes)"
    if args.dataset not in eval_results.dataset_ids(args.record):
        return f"dataset {args.dataset!r} is not described in {args.record}"
    return None


async def run(args: argparse.Namespace) -> int:
    try:
        folder = outside_repo(args.folder)
        rows = load_labels(folder, args.limit)
        problem = record_problem(args)
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if not rows:
        print("error: no labelled images found", file=sys.stderr)
        return 2
    settings = get_settings()
    if args.llm:
        settings = settings.model_copy(update={"agent_llm": args.llm})
    llm = make_agent_llm(settings)
    sam = make_sam(settings) if args.sam else None
    ledger = spend.SpendLedger(settings.data_dir, args.spend_cap, script="eval_vision")
    clients = [c for c in (llm, sam) if c is not None]
    spend.attach(ledger, *clients)
    images = {row["file"]: (folder / "images" / row["file"]).read_bytes() for row in rows}
    try:
        estimate, detail = await estimate_cost(llm, sam, list(images.values()))
        print(f"{len(rows)} photos, judge {llm.model}{' + SAM' if sam else ''}: {detail}")
        if not ledger.approve(estimate, detail):
            return 1
        if estimate and not args.yes and input("continue? [y/N] ").strip().lower() != "y":
            return 1
        results = await judge_all(llm, sam, rows, images, ledger)
    finally:
        for client in clients:
            await client.aclose()
        ledger.close()

    scored = [(row, res) for row, res in zip(rows, results, strict=True) if "error" not in res]
    for res in results:
        if "error" in res:
            print(f"{res['file']}: failed: {res['error']}")
    if args.out:
        args.out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    if not scored:
        return 1
    print(f"\nscored {len(scored)} of {len(rows)} photos with {llm.model}")
    metrics = score(scored, sam=sam is not None)
    print_metrics(metrics, len(scored))
    print(f"logged spend for this run: ${ledger.run_spend:.4f}")
    if args.record:
        stored = eval_results.upsert(
            args.record,
            {
                "task": "photo_judge",
                "dataset": args.dataset,
                "model": settings.agent_llm,
                "model_id": llm.model,
                "backend": eval_results.backend_of(llm),
                "sam": SAM_MODEL if sam else None,
                "date": eval_results.manila_today(),
                "commit": eval_results.git_commit(),
                "n": len(scored),
                "errors": len(rows) - len(scored),
                "spend_usd": ledger.run_spend,
                "metrics": metrics,
            },
        )
        print(f"recorded photo_judge/{args.dataset}/{stored['model']} in {args.record}")
    return 0


async def estimate_cost(llm, sam, images: list[bytes]) -> tuple[float, str]:
    """USD for the calls not cached yet, from a free cache-only dry run (worst case for fakes)."""
    if not spend.probe_ready(llm, *([sam] if sam else [])):
        judge_misses, sam_misses = len(images), len(images) * len(SAM_CONCEPTS) * bool(sam)
    else:
        with spend.probe(llm, sam):
            calls = [judge(llm, image) for image in images]
            if sam is not None:
                calls += [sam.segment(image, c) for image in images for c in SAM_CONCEPTS]
            await asyncio.gather(*calls, return_exceptions=True)
        judge_misses, sam_misses = spend.misses(llm), spend.misses(sam)
    cost = judge_misses * SPARK_COST_PER_PHOTO + sam_misses * SAM_PRICE_PER_REQUEST
    return cost, f"{judge_misses} uncached judge call(s), {sam_misses} uncached SAM call(s)"


async def judge_all(llm, sam, rows, images, ledger: spend.SpendLedger) -> list[dict[str, Any]]:
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict[str, str]) -> dict[str, Any]:
        image = images[row["file"]]
        async with limit:
            if ledger.over_cap():
                return {"file": row["file"], "error": "skipped: the spend cap is reached"}
            calls = [judge(llm, image)]
            if sam is not None:
                calls += [sam.segment(image, "roof"), sam.segment(image, "floodwater")]
            replies = await asyncio.gather(*calls, return_exceptions=True)
            failed = next((r for r in replies if isinstance(r, BaseException)), None)
            if failed is not None:
                return {"file": row["file"], "error": str(failed)}
            result: dict[str, Any] = {"file": row["file"], "judgment": replies[0]}
            if sam is not None:
                result["measurement"] = measure(replies[1], replies[2]).as_dict()
            return result

    return list(await asyncio.gather(*(one(r) for r in rows)))


SAM_CHECKS = (
    ("roof", "roof", lambda m: m["roof_count"] > 0),
    ("floodwater", "floodwater", lambda m: m["floodwater_present"]),
)


def score(scored: list[tuple[dict[str, str], dict[str, Any]]], *, sam: bool) -> dict[str, Any]:
    """The metrics dict the results file stores (see app/eval_results.py)."""
    labels = [row["roof_damage"] for row, _ in scored]
    predictions = [res["judgment"]["roof_damage"] for _, res in scored]
    confusion: dict[str, dict[str, int]] = {}
    for (label, predicted), count in sorted(Counter(zip(labels, predictions, strict=True)).items()):
        confusion.setdefault(label, {})[predicted] = count
    metrics: dict[str, Any] = {
        "roof_damage": {
            "kappa": round(cohen_kappa(labels, predictions), 4),
            "agreement": sum(a == b for a, b in zip(labels, predictions, strict=True)),
            "confusion": confusion,
        },
        "flags": {
            flag: eval_results.scores_dict(
                binary_scores(
                    [row[flag] == "1" for row, _ in scored],
                    [bool(res["judgment"][flag]) for _, res in scored],
                )
            )
            for flag in FLAGS
        },
        "sam": None,
    }
    if sam:
        metrics["sam"] = {
            concept: eval_results.scores_dict(
                binary_scores(
                    [row[label] == "1" for row, _ in scored],
                    [predicted(res["measurement"]) for _, res in scored],
                )
            )
            for concept, label, predicted in SAM_CHECKS
        }
    return metrics


def print_metrics(metrics: dict[str, Any], n: int) -> None:
    roof = metrics["roof_damage"]
    print(f"roof_damage        kappa {roof['kappa']:.2f}  agreement {roof['agreement']}/{n}")
    for flag in FLAGS:
        s = metrics["flags"][flag]
        print(
            f"{flag:18} F1 {s['f1']:.2f}  P {s['precision']:.2f}  R {s['recall']:.2f}  "
            f"(tp {s['tp']} fp {s['fp']} fn {s['fn']} tn {s['tn']})"
        )
    for concept, s in (metrics["sam"] or {}).items():
        print(
            f"{'SAM ' + concept:18} F1 {s['f1']:.2f}  P {s['precision']:.2f}  R {s['recall']:.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

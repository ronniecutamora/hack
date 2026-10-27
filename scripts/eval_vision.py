"""Score the photo judge (and optionally SAM measurements) against hand-labelled photos.

Usage:
    uv run python -m scripts.eval_vision /path/to/folder [--llm muse-spark-open] [--sam]
        [--limit N] [--yes]

The folder (OUTSIDE this repository) holds labels.csv and an images/ folder. labels.csv has a
`file` column and, per photo, 0/1 columns roof, fallen_tree, floodwater, debris, tarpaulin and
a roof_damage column (none | partial | severe | unknown). Responses are cached, so re-runs are
free; the cost of uncached calls is printed before anything runs.
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.evaluation import binary_scores, cohen_kappa, outside_repo
from app.meta.base import MetaError
from app.meta.factory import make_agent_llm, make_sam
from app.photo import judge, measure

FLAGS = ("floodwater", "debris", "fallen_tree", "tarpaulin")
SPARK_COST_PER_PHOTO = 0.012  # measured 2026-10-27 (about 1k prompt + 2.5k output tokens)
SAM_COST_PER_PHOTO = 0.005  # two concepts at $0.0025
CONCURRENCY = 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--llm", choices=["muse-spark-1.3", "muse-spark-open"])
    parser.add_argument("--sam", action="store_true", help="also score SAM roof/floodwater")
    parser.add_argument("--limit", type=int, help="only the first N photos")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--out", type=Path, help="write per-photo results as JSON here")
    return parser.parse_args(argv)


def load_labels(folder: Path, limit: int | None) -> list[dict[str, str]]:
    with (folder / "labels.csv").open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (folder / "images" / r["file"]).is_file()]
    return rows[:limit] if limit else rows


async def run(args: argparse.Namespace) -> int:
    try:
        folder = outside_repo(args.folder)
        rows = load_labels(folder, args.limit)
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("error: no labelled images found", file=sys.stderr)
        return 2
    settings = get_settings()
    if args.llm:
        settings = settings.model_copy(update={"agent_llm": args.llm})
    llm = make_agent_llm(settings)
    on_baseten = settings.agent_llm == "muse-spark-open"
    meta_cost = len(rows) * (
        (0 if on_baseten else SPARK_COST_PER_PHOTO) + SAM_COST_PER_PHOTO * args.sam
    )
    print(
        f"{len(rows)} photos, judge {llm.model}{' + SAM' if args.sam else ''}: at most "
        f"${meta_cost:.2f} of Meta credits{' (+ Baseten credits)' if on_baseten else ''}, "
        "less for cached photos"
    )
    if not args.yes and input("continue? [y/N] ").strip().lower() != "y":
        return 1

    sam = make_sam(settings) if args.sam else None
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict[str, str]) -> dict:
        image = (folder / "images" / row["file"]).read_bytes()
        async with limit:
            try:
                judgment = await judge(llm, image)
                result = {"file": row["file"], "judgment": judgment}
                if sam is not None:
                    roof, flood = await asyncio.gather(
                        sam.segment(image, "roof"), sam.segment(image, "floodwater")
                    )
                    result["measurement"] = measure(roof, flood).as_dict()
                return result
            except MetaError as exc:
                return {"file": row["file"], "error": str(exc)}

    try:
        results = await asyncio.gather(*(one(r) for r in rows))
    finally:
        await llm.aclose()
        if sam is not None:
            await sam.aclose()

    scored = [(row, res) for row, res in zip(rows, results, strict=True) if "error" not in res]
    for res in results:
        if "error" in res:
            print(f"{res['file']}: failed: {res['error']}")
    if not scored:
        return 1
    print(f"\nscored {len(scored)} of {len(rows)} photos with {llm.model}")
    kappa = cohen_kappa(
        [row["roof_damage"] for row, _ in scored],
        [res["judgment"]["roof_damage"] for _, res in scored],
    )
    agree = sum(row["roof_damage"] == res["judgment"]["roof_damage"] for row, res in scored)
    print(f"roof_damage        kappa {kappa:.2f}  agreement {agree}/{len(scored)}")
    for flag in FLAGS:
        s = binary_scores(
            [row[flag] == "1" for row, _ in scored],
            [bool(res["judgment"][flag]) for _, res in scored],
        )
        print(
            f"{flag:18} F1 {s.f1:.2f}  P {s.precision:.2f}  R {s.recall:.2f}  "
            f"(tp {s.tp} fp {s.fp} fn {s.fn} tn {s.tn})"
        )
    if args.sam:
        for name, label, predicted in (
            ("SAM roof", "roof", lambda m: m["roof_count"] > 0),
            ("SAM floodwater", "floodwater", lambda m: m["floodwater_present"]),
        ):
            s = binary_scores(
                [row[label] == "1" for row, _ in scored],
                [predicted(res["measurement"]) for _, res in scored],
            )
            print(f"{name:18} F1 {s.f1:.2f}  P {s.precision:.2f}  R {s.recall:.2f}")
    if args.out:
        args.out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

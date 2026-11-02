"""Kill test for rice-field photos: the photo judge's crop_field and SAM 3.1's field and water.

Usage:
    uv run python -m scripts.eval_crop /path/to/folder [--concepts "rice field,rice plants"]
        [--yes] [--out results.json] [--record data/eval/results.json --dataset ID]
        [--spend-cap USD]

The folder (OUTSIDE this repository) holds labels.csv and an images/ folder. labels.csv has a
`file` column, `crop_field` (none | no_visible_damage | flooded | lodged | unknown) and
`water_pct`, the labeler's estimate of the share of the field in the photo that open water
covers (0-100). Each photo gets the production photo judge (app.photo.judge) and one SAM
request per concept: "floodwater" and each field concept (one concept per request, plain
nouns). Scored: crop_field agreement and kappa, "damaged" (flooded or lodged) precision and
recall, each field concept's detection rate, and, per field concept, how far SAM's water share
(app.photo.water_on_field) lands from the label. Responses are cached; the uncached calls are
priced and checked against the spend cap first (scripts/spend.py).
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
from app.meta.sam import SamResult
from app.photo import CROP_FIELD, judge
from scripts import spend
from scripts.eval_vision import CONCURRENCY, SPARK_COST_PER_PHOTO

WATER = "floodwater"
DAMAGED = ("flooded", "lodged")
CLOSE_POINTS = 20  # a water share within this many points of the label counts as close


FIELD_THRESHOLD = 0.5  # the field and its water: masks at SAM's confidence floor count
GRID = 160  # cells across the photo's longer side when comparing two concepts' masks


def coverage(result: SamResult, threshold: float, size: tuple[int, int]) -> set[int]:
    """The cells (row * width + column) of a `size` grid laid over the photo that the concept's
    masks at or above the threshold cover; overlapping instances count each cell once."""
    if not result.image_size:
        return set()
    width, height = size
    sx, sy = width / result.image_size[0], height / result.image_size[1]
    cells: set[int] = set()
    for detection in result.above(threshold):
        mask = detection.mask
        if mask is None or not mask.width or not mask.height:
            continue
        left, top, right, bottom = (
            v * s for v, s in zip(mask.bounds, (sx, sy, sx, sy), strict=True)
        )
        span_x, span_y = max(right - left, 1e-9), max(bottom - top, 1e-9)
        for y in range(max(0, int(top)), min(height, int(bottom) + 1)):
            my = int((y + 0.5 - top) / span_y * mask.height)
            if not 0 <= my < mask.height:
                continue
            for x in range(max(0, int(left)), min(width, int(right) + 1)):
                mx = int((x + 0.5 - left) / span_x * mask.width)
                if 0 <= mx < mask.width and mask.raster[my * mask.width + mx]:
                    cells.add(y * width + x)
    return cells


def grid_size(result: SamResult) -> tuple[int, int]:
    w, h = result.image_size or (GRID, GRID)
    scale = GRID / max(w, h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def water_on_field(field: SamResult, water: SamResult, threshold: float) -> float | None:
    """The share of the field in the photo (the field's masks plus the water's) that the water
    covers: what "water over about N% of the field in the photo" means. None when SAM outlines
    no field, since water alone says nothing about a field. Water the field masks also cover
    counts as water."""
    size = grid_size(field)
    field_cells = coverage(field, threshold, size)
    if not field_cells:
        return None
    water_cells = coverage(water, threshold, size)
    return len(water_cells) / len(field_cells | water_cells)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--concepts", default="rice field", help="comma-separated field nouns")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--out", type=Path, help="write per-photo results as JSON here")
    parser.add_argument("--record", type=Path, metavar="RESULTS", help="upsert into this file")
    parser.add_argument("--dataset", help="dataset id in the results file (with --record)")
    spend.add_cap_argument(parser)
    return parser.parse_args(argv)


def load_labels(folder: Path) -> list[dict[str, str]]:
    with (folder / "labels.csv").open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (folder / "images" / r["file"]).is_file()]
    bad = [r["file"] for r in rows if r["crop_field"] not in CROP_FIELD]
    if bad:
        raise ValueError(f"unknown crop_field label in {', '.join(bad)}")
    return rows


async def run(args: argparse.Namespace) -> int:
    concepts = [c.strip() for c in args.concepts.split(",") if c.strip()]
    try:
        folder = outside_repo(args.folder)
        rows = load_labels(folder)
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.record and (
        not args.dataset or args.dataset not in eval_results.dataset_ids(args.record)
    ):
        print("error: --record needs --dataset, an id the results file describes", file=sys.stderr)
        return 2
    settings = get_settings()
    llm, sam = make_agent_llm(settings), make_sam(settings)
    ledger = spend.SpendLedger(settings.data_dir, args.spend_cap, script="eval_crop")
    spend.attach(ledger, llm, sam)
    images = {r["file"]: (folder / "images" / r["file"]).read_bytes() for r in rows}
    asked = [WATER, *concepts]
    try:
        if spend.probe_ready(llm, sam):
            with spend.probe(llm, sam):
                calls = [judge(llm, image) for image in images.values()]
                calls += [sam.segment(i, c) for i in images.values() for c in asked]
                await asyncio.gather(*calls, return_exceptions=True)
            judge_misses, sam_misses = spend.misses(llm), spend.misses(sam)
        else:
            judge_misses, sam_misses = len(images), len(images) * len(asked)
        estimate = judge_misses * SPARK_COST_PER_PHOTO + sam_misses * SAM_PRICE_PER_REQUEST
        detail = f"{judge_misses} uncached judge call(s), {sam_misses} uncached SAM call(s)"
        print(f"{len(rows)} photos, judge {llm.model} + SAM {', '.join(asked)}: {detail}")
        if not ledger.approve(estimate, detail):
            return 1
        if estimate and not args.yes and input("continue? [y/N] ").strip().lower() != "y":
            return 1
        limit = asyncio.Semaphore(CONCURRENCY)

        async def one(row: dict[str, str]) -> dict[str, Any]:
            image = images[row["file"]]
            async with limit:
                if ledger.over_cap():
                    return {"file": row["file"], "error": "skipped: the spend cap is reached"}
                replies = await asyncio.gather(
                    judge(llm, image), *(sam.segment(image, c) for c in asked),
                    return_exceptions=True,
                )  # fmt: skip
            failed = next((r for r in replies if isinstance(r, BaseException)), None)
            if failed is not None:
                return {"file": row["file"], "error": str(failed)}
            water, fields = replies[1], dict(zip(concepts, replies[2:], strict=True))
            return {
                "file": row["file"],
                "judgment": replies[0],
                "water_found": bool(water.above(FIELD_THRESHOLD)),
                "fields": {
                    c: {
                        "found": bool(r.above(FIELD_THRESHOLD)),
                        "water_share": water_on_field(r, water, FIELD_THRESHOLD),
                    }
                    for c, r in fields.items()
                },
            }

        results = list(await asyncio.gather(*(one(r) for r in rows)))
    finally:
        await llm.aclose()
        await sam.aclose()
        ledger.close()
    for res in results:
        if "error" in res:
            print(f"{res['file']}: failed: {res['error']}")
    scored = [(row, res) for row, res in zip(rows, results, strict=True) if "error" not in res]
    if args.out:
        args.out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    if not scored:
        return 1
    metrics = score(scored, concepts)
    print(json.dumps(metrics, indent=1))
    print(f"logged spend for this run: ${ledger.run_spend:.4f}")
    if args.record:
        stored = eval_results.upsert(
            args.record,
            {
                "task": "crop_photo",
                "dataset": args.dataset,
                "model": settings.agent_llm,
                "model_id": llm.model,
                "backend": eval_results.backend_of(llm),
                "sam": SAM_MODEL,
                "date": eval_results.manila_today(),
                "commit": eval_results.git_commit(),
                "n": len(scored),
                "errors": len(rows) - len(scored),
                "spend_usd": ledger.run_spend,
                "metrics": metrics,
            },
        )
        print(f"recorded crop_photo/{args.dataset}/{stored['model']} in {args.record}")
    return 0


def score(scored: list[tuple[dict[str, str], dict[str, Any]]], concepts: list[str]) -> dict:
    """The metrics dict the results file stores for crop_photo (see app/eval_results.py)."""
    labels = [row["crop_field"] for row, _ in scored]
    predictions = [res["judgment"]["crop_field"] for _, res in scored]
    confusion: dict[str, dict[str, int]] = {}
    for (label, predicted), n in sorted(Counter(zip(labels, predictions, strict=True)).items()):
        confusion.setdefault(label, {})[predicted] = n
    sam: dict[str, Any] = {}
    for concept in concepts:
        errors = [
            abs(res["fields"][concept]["water_share"] * 100 - float(row["water_pct"]))
            for row, res in scored
            if res["fields"][concept]["water_share"] is not None
        ]
        sam[concept] = {
            "found": sum(res["fields"][concept]["found"] for _, res in scored),
            "measured": len(errors),
            "water_share_mae": round(sum(errors) / len(errors), 1) if errors else None,
            "water_share_close": sum(e <= CLOSE_POINTS for e in errors),
            "close_points": CLOSE_POINTS,
        }
    return {
        "crop_field": {
            "kappa": round(cohen_kappa(labels, predictions), 4),
            "agreement": sum(a == b for a, b in zip(labels, predictions, strict=True)),
            "confusion": confusion,
        },
        "damaged": eval_results.scores_dict(
            binary_scores([x in DAMAGED for x in labels], [x in DAMAGED for x in predictions])
        ),
        "sam": sam,
    }


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

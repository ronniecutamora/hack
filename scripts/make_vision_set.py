"""Generate the synthetic photo eval set from the scene table (Muse Image, $0.01 per image).

    uv run python -m scripts.make_vision_set /path/outside/repo/vision-synthetic [--yes]
        [--spend-cap USD]

Each scene in data/eval/vision_scenes.json has its labels fixed by design (Biñan-style
lakeshore streets, GI-sheet roofs, fish pens, and hard negatives such as wet roads or tarps on
intact roofs). The prompt is the scene plus the demo's style and no-people guardrails
(app/demo/images.py), and every image is stamped as AI-generated. The folder gets images/ and a
labels.csv that scripts.eval_vision reads.

Idempotent: an image is generated only when its file (scene id + prompt hash) is missing, so a
re-run is free and a changed scene gets a new image. Rows already in labels.csv are kept as
they are, because a person reviews every image and corrects any label the image does not
support, noting why in `label_notes`; a label set that only echoes the prompt would be circular.
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

from app.cache import sha256_hex
from app.config import get_settings
from app.demo.images import build_prompt, stamp_label
from app.evaluation import REPO_ROOT, outside_repo
from app.meta.factory import make_cache
from app.meta.image import PRICE_PER_IMAGE, ImageClient
from scripts import spend

SCENES_PATH = REPO_ROOT / "data" / "eval" / "vision_scenes.json"
LABELS = ("roof", "fallen_tree", "floodwater", "debris", "tarpaulin", "roof_damage", "fish_cage")
COLUMNS = ("file", *LABELS, "category", "label_notes")
ROOF_DAMAGE = ("none", "partial", "severe", "unknown")


def load_scenes(path: Path = SCENES_PATH) -> list[dict[str, Any]]:
    scenes = json.loads(path.read_text(encoding="utf-8"))
    ids = [s["id"] for s in scenes]
    if len(set(ids)) != len(ids):
        raise ValueError("scene ids must be unique")
    for scene in scenes:
        labels = scene["labels"]
        if set(labels) != set(LABELS):
            raise ValueError(f"{scene['id']}: labels must be exactly {', '.join(LABELS)}")
        if labels["roof_damage"] not in ROOF_DAMAGE:
            raise ValueError(f"{scene['id']}: roof_damage must be one of {ROOF_DAMAGE}")
        if any(labels[k] not in (0, 1) for k in LABELS if k != "roof_damage"):
            raise ValueError(f"{scene['id']}: flags must be 0 or 1")
        if not labels["roof"] and labels["roof_damage"] != "unknown":
            raise ValueError(f"{scene['id']}: no roof in view means roof_damage unknown")
    return scenes


def image_name(scene: dict[str, Any]) -> str:
    """Scene id plus a hash of the full prompt: a changed scene never reuses an old image."""
    return f"{scene['id']}-{sha256_hex(build_prompt(scene['scene']).encode())[:8]}.jpg"


def merged_rows(scenes: list[dict[str, Any]], folder: Path) -> list[dict[str, str]]:
    """labels.csv rows for every scene whose image exists; reviewed rows are kept verbatim."""
    existing: dict[str, dict[str, str]] = {}
    path = folder / "labels.csv"
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            existing = {row["file"]: row for row in csv.DictReader(f)}
    rows = []
    for scene in scenes:
        name = image_name(scene)
        if not (folder / "images" / name).is_file():
            continue
        designed = {"file": name, **{k: str(v) for k, v in scene["labels"].items()}}
        rows.append(
            existing.get(name) or {**designed, "category": scene["category"], "label_notes": ""}
        )
    return rows


def write_labels(folder: Path, rows: list[dict[str, str]]) -> None:
    tmp = folder / "labels.csv.tmp"
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(folder / "labels.csv")


def resolve_folder(folder: Path) -> Path:
    folder = folder.expanduser().resolve()
    if folder == REPO_ROOT or REPO_ROOT in folder.parents:
        raise ValueError("keep eval data outside the repository; pass a folder elsewhere")
    (folder / "images").mkdir(parents=True, exist_ok=True)
    return outside_repo(folder)


async def generate(
    scenes: list[dict[str, Any]], folder: Path, client: ImageClient, ledger: spend.SpendLedger
) -> int:
    made = 0
    for scene in scenes:
        path = folder / "images" / image_name(scene)
        if path.is_file():
            continue
        if ledger.over_cap():
            print("stopped: the spend cap is reached")
            break
        image = await client.generate(build_prompt(scene["scene"]))
        path.write_bytes(stamp_label(image))
        made += 1
        print(f"  {path.name}")
    return made


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="output folder, outside this repository")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    spend.add_cap_argument(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        folder = resolve_folder(args.folder)
        scenes = load_scenes()
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    settings = get_settings()
    missing = [s for s in scenes if not (folder / "images" / image_name(s)).is_file()]
    print(
        f"{len(scenes)} scenes: {len(scenes) - len(missing)} images exist, {len(missing)} to make"
    )
    if missing:
        ledger = spend.SpendLedger(settings.data_dir, args.spend_cap, script="make_vision_set")
        try:
            estimate = len(missing) * PRICE_PER_IMAGE
            if not ledger.approve(estimate, f"{len(missing)} image(s) at ${PRICE_PER_IMAGE:.2f}"):
                return 1
            if not args.yes and input("generate? [y/N] ").strip().lower() != "y":
                return 1
            client = ImageClient(
                api_key=settings.meta_api_key.get_secret_value(),
                cache=make_cache(settings),
                recorder=ledger.record,
            )

            async def make() -> int:
                try:
                    return await generate(scenes, folder, client, ledger)
                finally:
                    await client.aclose()

            made = asyncio.run(make())
            print(f"generated {made} image(s); logged spend ${ledger.run_spend:.2f}")
        finally:
            ledger.close()
    rows = merged_rows(scenes, folder)
    write_labels(folder, rows)
    reviewed = sum(bool(r.get("label_notes")) for r in rows)
    print(f"{folder / 'labels.csv'}: {len(rows)} rows, {reviewed} with review notes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

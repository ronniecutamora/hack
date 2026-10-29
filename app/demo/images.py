"""Synthetic photos for the demo scenario, generated once and cached by prompt hash.

Every image is stamped "AI-generated (Muse Image) · synthetic demo" in a corner so
no one mistakes it for a real report photo. Images live under
``<DATA_DIR>/images/<prompt-hash>.jpg``: re-runs are free, and the loader refuses
to run while any image is missing.
"""

import asyncio
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.cache import sha256_hex
from app.config import Settings, get_settings
from app.demo import SCENARIO_PATH, DemoDirError, require_demo_dir
from app.demo.scenario import ScenarioReport, load_scenario, validate
from app.meta.factory import make_cache
from app.meta.image import PRICE_PER_IMAGE, ImageClient

LABEL_TEXT = "AI-generated (Muse Image) · synthetic demo"
# Biñan is flat lowland on the Laguna de Bay shore: a first test image (2026-10-28) showed
# mountains, which no Biñan street has.
STYLE = (
    "Photorealistic phone-camera photo, overcast daylight. Flat, low-lying lakeshore town on a"
    " wide lake; no mountains or hills anywhere in view."
)
SAFETY = "No people, no faces, no readable text, no signs, no logos, no license plates."


def build_prompt(scene: str) -> str:
    """The full generation prompt: the scenario scene plus style and honesty guardrails."""
    return f"{scene.strip()} {STYLE} {SAFETY}"


def image_filename(prompt: str) -> str:
    """Cache file name for a prompt (hash, so re-runs never pay twice)."""
    return sha256_hex(prompt.encode())[:16] + ".jpg"


@dataclass(frozen=True)
class ImageJob:
    report: ScenarioReport
    prompt: str
    path: Path


def jobs_for(reports: list[ScenarioReport], images_dir: Path) -> list[ImageJob]:
    """One job per report that has a photo scene."""
    jobs = []
    for report in reports:
        if report.photo_prompt is None:
            continue
        prompt = build_prompt(report.photo_prompt)
        jobs.append(ImageJob(report, prompt, images_dir / image_filename(prompt)))
    return jobs


def photo_paths(reports: list[ScenarioReport], images_dir: Path) -> dict[str, Path]:
    """The photo file each report sends: its own job's, or the one it resends (photo_from)."""
    own = {job.report.key: job.path for job in jobs_for(reports, images_dir)}
    return {
        r.key: own[r.photo_from] if r.photo_from in own else own[r.key]
        for r in reports
        if r.key in own or r.photo_from in own
    }


def stamp_label(data: bytes) -> bytes:
    """Stamp the synthetic-content label into the bottom-left corner; return JPEG bytes."""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(12, width // 42))
    pad = max(4, width // 128)
    left, top, right, bottom = draw.textbbox((0, 0), LABEL_TEXT, font=font)
    text_w, text_h = right - left, bottom - top
    box = (pad, height - text_h - 3 * pad, text_w + 3 * pad, height - pad)
    draw.rectangle(box, fill=(0, 0, 0))
    draw.text(
        (2 * pad - left, height - text_h - 2 * pad - top),
        LABEL_TEXT,
        font=font,
        fill=(255, 255, 255),
    )
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


async def ensure_images(jobs: list[ImageJob], make_client: Callable[[], ImageClient]) -> list[Path]:
    """Generate every uncached job image (stamped) and return the new files."""
    todo = [job for job in jobs if not job.path.is_file()]
    if not todo:
        return []
    client = make_client()
    try:
        written = []
        for job in todo:
            job.path.parent.mkdir(parents=True, exist_ok=True)
            job.path.write_bytes(stamp_label(await client.generate(job.prompt)))
            written.append(job.path)
        return written
    finally:
        await client.aclose()


def images_command(settings: Settings, *, assume_yes: bool = False) -> int:
    """Print the uncached image count and cost, confirm, then generate. See module docstring."""
    try:
        data_dir = require_demo_dir(settings, "images")
    except DemoDirError as exc:
        print(exc)
        return 2
    reports = load_scenario(SCENARIO_PATH)
    problems = validate(reports)
    if problems:
        print("Scenario is invalid:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    jobs = jobs_for(reports, data_dir / "images")
    missing = [job for job in jobs if not job.path.is_file()]
    cost = len(missing) * PRICE_PER_IMAGE
    print(
        f"{len(jobs)} photo(s) in the scenario: "
        f"{len(jobs) - len(missing)} cached, {len(missing)} to generate "
        f"(~${cost:.2f} at ${PRICE_PER_IMAGE:.2f}/image)."
    )
    if not missing:
        print("All images are cached; nothing to generate.")
        return 0
    if not assume_yes:
        answer = input(f"Generate {len(missing)} image(s) for ~${cost:.2f}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled; no images generated.")
            return 0
    written = asyncio.run(
        ensure_images(
            jobs,
            lambda: ImageClient(
                api_key=settings.meta_api_key.get_secret_value(),
                cache=make_cache(settings),
            ),
        )
    )
    print(f"Generated {len(written)} image(s) under {data_dir / 'images'}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the demo scenario's photos.")
    parser.add_argument("--yes", action="store_true", help="generate without asking")
    return images_command(get_settings(), assume_yes=parser.parse_args(argv).yes)


if __name__ == "__main__":
    raise SystemExit(main())

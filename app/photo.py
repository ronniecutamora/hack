"""Photo assessment: the agent LLM judges damage, SAM 3.1 measures it, masks become evidence.

Model jobs follow the 2026-10-27 kill test on 25 labelled typhoon and flood photos:
- The LLM judges damage state (roof damage kappa 0.78 vs hand labels; floodwater and debris
  F1 0.92). The schema and definitions below are the ones that were scored.
- SAM measures with plain object nouns only. "damaged roof" scored F1 0.36, so conditions are
  never sent to SAM. Its confidence has a floor of 0.50, so 0.5 and 0.7 are the only useful
  thresholds: roofs are counted at 0.5 (F1 1.00); floodwater counts at 0.7 (F1 1.00), or at
  0.5 when the masks cover at least 10% of the photo (puddles were the false positives).
- Neither model finds fallen trees reliably, so a "fallen tree" judgment needs confirmation.

Measurements are evidence for the officer, not a loss figure.
"""

import asyncio
import io
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from app.meta.llm import AgentLLM
from app.meta.sam import SamClient, SamResult

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "roof_damage": {"type": "string", "enum": ["none", "partial", "severe", "unknown"]},
        "fallen_tree": {"type": "boolean"},
        "floodwater": {"type": "boolean"},
        "debris": {"type": "boolean"},
        "tarpaulin": {"type": "boolean"},
        "house_count_estimate": {"type": "integer", "minimum": 0},
        "notes": {"type": "string"},
    },
    "required": [
        "roof_damage",
        "fallen_tree",
        "floodwater",
        "debris",
        "tarpaulin",
        "house_count_estimate",
        "notes",
    ],
    "additionalProperties": False,
}
JUDGE_SYSTEM = (
    "You assess typhoon damage in a single ground-level photo from the Philippines "
    "for a local government damage report. Look only at what is visible. Return JSON matching "
    "the schema.\n"
    "roof_damage: none = roofs visible and no visible roof damage; partial = some roof "
    "sheeting or sections missing, bent or displaced but the roof is still mostly there; "
    "severe = roof largely gone, collapsed, or the building is destroyed; unknown = no roof "
    "visible or cannot judge. Judge the most prominent damaged roof.\n"
    "fallen_tree: true if a tree or large palm trunk is down, uprooted or leaning down over the "
    "street or a building.\n"
    "floodwater: true if standing or flowing floodwater covers a substantial area (not just "
    "small puddles).\n"
    "debris: true if wreckage, rubble or scattered storm debris is visible.\n"
    "tarpaulin: true if a tarpaulin, plastic sheet or tent canvas is visible.\n"
    "house_count_estimate: number of houses (residential buildings, intact or ruined) visible; "
    "0 if none.\n"
    "notes: one short sentence."
)
JUDGE_PROMPT = "Assess this photo."

ROOF_THRESHOLD = 0.5
FLOOD_THRESHOLD = 0.7
FLOOD_LOW_THRESHOLD = 0.5
FLOOD_MIN_AREA = 0.10
SAM_CONCEPTS = ("roof", "floodwater")
OVERLAY_MAX_SIDE = 1280
MASK_ALPHA = 110  # 0-255: masks tint the photo but keep it readable
_COLOURS = {"roof": (255, 140, 0), "floodwater": (30, 144, 255)}


@dataclass(frozen=True)
class Measurement:
    roof_count: int
    floodwater_present: bool
    floodwater_fraction: float  # share of the photo covered by floodwater masks (conf >= 0.5)

    def as_dict(self) -> dict[str, Any]:
        return {
            "roof_count": self.roof_count,
            "floodwater_present": self.floodwater_present,
            "floodwater_fraction": round(self.floodwater_fraction, 4),
            "thresholds": {
                "roof": ROOF_THRESHOLD,
                "floodwater": FLOOD_THRESHOLD,
                "floodwater_low": FLOOD_LOW_THRESHOLD,
                "floodwater_min_area": FLOOD_MIN_AREA,
            },
        }


def measure(roof: SamResult, floodwater: SamResult) -> Measurement:
    low_area = floodwater.area_fraction(FLOOD_LOW_THRESHOLD)
    present = bool(floodwater.above(FLOOD_THRESHOLD)) or low_area >= FLOOD_MIN_AREA
    return Measurement(roof.count(ROOF_THRESHOLD), present, low_area)


@dataclass(frozen=True)
class PhotoResult:
    judgment: dict[str, Any]
    measurement: Measurement
    overlay: bytes | None  # JPEG with masks drawn, or None if the photo could not be read


async def judge(llm: AgentLLM, image: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
    return await llm.chat_json(
        system=JUDGE_SYSTEM,
        user_text=JUDGE_PROMPT,
        schema=JUDGE_SCHEMA,
        schema_name="DamageJudgement",
        image=image,
        image_mime=mime,
    )


async def assess_photo(
    llm: AgentLLM, sam: SamClient, image: bytes, mime: str = "image/jpeg"
) -> PhotoResult:
    """Judge and measure one photo; the three model calls run concurrently."""
    judgment, roof, floodwater = await asyncio.gather(
        judge(llm, image, mime),
        sam.segment(image, "roof", image_mime=mime),
        sam.segment(image, "floodwater", image_mime=mime),
    )
    overlay = render_overlay(
        image, {"roof": (roof, ROOF_THRESHOLD), "floodwater": (floodwater, FLOOD_LOW_THRESHOLD)}
    )
    return PhotoResult(judgment, measure(roof, floodwater), overlay)


def render_overlay(image: bytes, layers: dict[str, tuple[SamResult, float]]) -> bytes | None:
    """Draw each concept's masks (filled) and boxes (outlined) over the photo, as a JPEG."""
    try:
        base = Image.open(io.BytesIO(image))
        base.load()
    except (UnidentifiedImageError, OSError):
        return None
    sizes = {r.image_size for r, _ in layers.values() if r.image_size}
    if sizes and base.size not in sizes:
        rotated = ImageOps.exif_transpose(base)  # SAM may have seen the EXIF-rotated photo
        if rotated.size in sizes:
            base = rotated
    base = base.convert("RGB")
    paint = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(paint)
    for concept, (result, threshold) in layers.items():
        colour = _COLOURS.get(concept, (255, 0, 255))
        sx, sy = _scale(result, base.size)
        for detection in result.above(threshold):
            mask = detection.mask
            if mask is not None and mask.width and mask.height:
                left, top, right, bottom = mask.bounds
                box = (round(left * sx), round(top * sy), round(right * sx), round(bottom * sy))
                size = (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
                alpha = bytes(MASK_ALPHA if v else 0 for v in mask.raster)
                grid = Image.frombytes("L", (mask.width, mask.height), alpha)
                fill = Image.new("RGBA", size, (*colour, 255))  # the grid sets the alpha
                # paste() clips at the photo's edges; the grid is the paste mask
                paint.paste(fill, (box[0], box[1]), grid.resize(size, Image.Resampling.NEAREST))
            left, top, right, bottom = detection.box
            draw.rectangle(
                (left * sx, top * sy, right * sx, bottom * sy), outline=(*colour, 255), width=3
            )
    out = Image.alpha_composite(base.convert("RGBA"), paint).convert("RGB")
    out.thumbnail((OVERLAY_MAX_SIDE, OVERLAY_MAX_SIDE))
    buffer = io.BytesIO()
    out.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def _scale(result: SamResult, size: tuple[int, int]) -> tuple[float, float]:
    if not result.image_size:
        return 1.0, 1.0
    return size[0] / result.image_size[0], size[1] / result.image_size[1]

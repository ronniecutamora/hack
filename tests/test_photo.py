import io

import pytest
from PIL import Image

from app.meta.sam import Detection, SamResult, parse_stream
from app.photo import (
    JUDGE_SCHEMA,
    assess_photo,
    measure,
    render_overlay,
)
from tests.test_sam import STREAM  # 1000x500 frame: one 0.9 and one 0.55 detection with masks

JUDGMENT = {
    "roof_damage": "partial",
    "fallen_tree": False,
    "floodwater": True,
    "debris": True,
    "tarpaulin": False,
    "house_count_estimate": 3,
    "notes": "Flooded street with damaged roofs.",
}


def jpeg(size=(1000, 500), colour=(128, 128, 128)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


def result(concept, *confidences_and_areas):
    return SamResult(
        concept,
        [Detection(c, (0, 0, 10, 10), a) for c, a in confidences_and_areas],
        (1000, 500),
    )


def test_roofs_are_counted_at_05():
    m = measure(result("roof", (0.5, 0.1), (0.9, 0.1), (0.49, 0.1)), result("floodwater"))
    assert m.roof_count == 2


@pytest.mark.parametrize(
    ("detections", "present", "fraction"),
    [
        ([(0.75, 0.02)], True, 0.02),  # confident detection
        ([(0.55, 0.04), (0.6, 0.07)], True, 0.11),  # low confidence but a large area
        ([(0.55, 0.03)], False, 0.03),  # a puddle: low confidence, small area
        ([], False, 0.0),
    ],
)
def test_floodwater_needs_confidence_or_area(detections, present, fraction):
    m = measure(result("roof"), result("floodwater", *detections))
    assert m.floodwater_present is present
    assert m.floodwater_fraction == pytest.approx(fraction)
    assert m.as_dict()["thresholds"]["floodwater"] == 0.7


def test_overlay_paints_masks_in_concept_colours():
    sam = parse_stream(STREAM, "floodwater")
    overlay = render_overlay(jpeg(), {"floodwater": (sam, 0.5)})
    image = Image.open(io.BytesIO(overlay)).convert("RGB")
    assert image.size == (1000, 500)
    inside = image.getpixel((150, 210))  # inside the 0.9 detection's mask
    outside = image.getpixel((800, 400))
    assert inside[2] > inside[0] + 40  # tinted blue
    assert abs(outside[0] - outside[2]) < 10  # untouched grey


def test_overlay_is_shrunk_and_unreadable_photos_give_none():
    sam = parse_stream(STREAM, "roof")
    big = render_overlay(jpeg((3000, 1500)), {"roof": (sam, 0.5)})
    assert max(Image.open(io.BytesIO(big)).size) == 1280
    assert render_overlay(b"not a photo", {"roof": (sam, 0.5)}) is None


class FakeLLM:
    model = "fake-llm"

    def __init__(self):
        self.calls = []

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        return JUDGMENT


class FakeSam:
    def __init__(self):
        self.concepts = []

    async def segment(self, image, concept, *, image_mime="image/jpeg", image_url=None):
        self.concepts.append(concept)
        return parse_stream(STREAM, concept)


async def test_assess_photo_judges_measures_and_overlays():
    llm, sam = FakeLLM(), FakeSam()
    photo = jpeg()

    out = await assess_photo(llm, sam, photo)

    assert out.judgment == JUDGMENT
    (call,) = llm.calls
    assert call["schema"] is JUDGE_SCHEMA and call["image"] == photo
    assert sorted(sam.concepts) == ["floodwater", "roof"]  # plain nouns, never "damaged roof"
    assert out.measurement.roof_count == 2
    assert out.measurement.floodwater_present  # the 0.9 detection
    assert out.overlay.startswith(b"\xff\xd8")

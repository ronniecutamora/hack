"""SAM 3.1: text-prompted segmentation of one concept per request (Responses API, streamed).

The stream is the SAM token grammar carried in `response.output_text.delta` events; it is
decoded with Meta's official `meta-sam-parser` package.
"""

import json
import re
from dataclasses import dataclass, field

import httpx
from meta_sam_parser import CompletedOutcome, decode_mask_to_raster, image_segmentation_format

from app.cache import sha256_hex
from app.meta.base import (
    ModelApiError,
    ModelClient,
    ModelOutputError,
    data_url,
    error_from_response,
)

MODEL = "sam-3.1"
URL = "https://api.meta.ai/v1/responses"
PRICE_PER_REQUEST = 0.0025  # $2.50 per 1,000 images
_SIZE_RE = re.compile(r";w=(\d+);h=(\d+)")


@dataclass(frozen=True)
class Mask:
    """A detection's mask: a width x height grid of 0/1 bytes stretched over `bounds`."""

    width: int
    height: int
    bounds: tuple[float, float, float, float]  # left, top, right, bottom in image pixels
    raster: bytes


@dataclass(frozen=True)
class Detection:
    confidence: float | None
    box: tuple[float, float, float, float]  # left, top, right, bottom in image pixels
    area_fraction: float  # share of the whole image covered by the mask (0 if no mask)
    mask: Mask | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class SamResult:
    concept: str
    detections: list[Detection] = field(default_factory=list)
    image_size: tuple[int, int] | None = None  # (width, height) as reported by the stream
    diagnostics: int = 0  # recoverable parser problems; 0 is normal

    def above(self, threshold: float) -> list[Detection]:
        """Detections with confidence >= threshold (SAM confidence has a floor near 0.5)."""
        return [
            d for d in self.detections if d.confidence is not None and d.confidence >= threshold
        ]

    def count(self, threshold: float = 0.5) -> int:
        return len(self.above(threshold))

    def area_fraction(self, threshold: float = 0.5) -> float:
        """Sum of mask area fractions above the threshold. Instances may overlap, so cap at 1."""
        return min(1.0, sum(d.area_fraction for d in self.above(threshold)))


def parse_stream(text: str, concept: str = "") -> SamResult:
    """Decode a concatenated SAM output-text stream into detections."""
    size_match = _SIZE_RE.search(text)
    image_size = (int(size_match[1]), int(size_match[2])) if size_match else None
    parser = image_segmentation_format().create_parser()
    parser.push(text, emit=False)
    result = parser.finish(CompletedOutcome()).result

    boxes = {r.object_id: r for r in result.records if r.kind == "box"}
    masks = {r.object_id: r for r in result.records if r.kind == "mask"}
    detections: list[Detection] = []
    for object_id, box in boxes.items():
        mask = masks.get(object_id)
        confidence = box.confidence
        area_fraction = 0.0
        decoded: Mask | None = None
        if mask is not None:
            if confidence is None:
                confidence = mask.confidence
            raster = decode_mask_to_raster(mask.mask)
            bounds = (mask.bounds.left, mask.bounds.top, mask.bounds.right, mask.bounds.bottom)
            decoded = Mask(mask.mask.width, mask.mask.height, bounds, raster)
            if image_size:
                covered = sum(raster) / max(1, mask.mask.width * mask.mask.height)
                box_area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                area_fraction = covered * box_area / (image_size[0] * image_size[1])
        detections.append(
            Detection(
                confidence, (box.left, box.top, box.right, box.bottom), area_fraction, decoded
            )
        )
    return SamResult(concept, detections, image_size, len(result.diagnostics))


class SamClient(ModelClient):
    service = "SAM"

    async def segment(
        self,
        image: bytes,
        concept: str,
        *,
        image_mime: str = "image/jpeg",
        image_url: str | None = None,
    ) -> SamResult:
        """Segment one concept ("roof", "floodwater": a short noun phrase, not a condition).

        The image is sent as a base64 data URL by default. If `image_url` is given it is tried
        first, and on a `media_url_not_fetchable` error the request is retried with base64.
        """
        headers = {
            **self.auth_headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        async def fetch() -> dict[str, str]:
            if image_url is not None:
                try:
                    return {"text": await self._stream(headers, concept, image_url)}
                except ModelApiError as exc:
                    if exc.code != "media_url_not_fetchable":
                        raise
            return {"text": await self._stream(headers, concept, data_url(image, image_mime))}

        key_payload = {"concept": concept, "image": sha256_hex(image)}
        reply = await self._call(MODEL, key_payload, fetch, lambda _r: PRICE_PER_REQUEST)
        return parse_stream(reply["text"], concept)

    async def _stream(self, headers: dict[str, str], concept: str, image_ref: str) -> str:
        body = {
            "model": MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": concept},
                        {"type": "input_image", "image_url": image_ref},
                    ],
                }
            ],
            "stream": True,
            "metadata": {"include_confidence": "true"},
        }
        deltas: list[str] = []
        try:
            async with self.http.stream("POST", URL, headers=headers, json=body) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise error_from_response(self.service, response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        deltas.append(event.get("delta", ""))
                    elif kind in ("response.failed", "error"):
                        raise ModelOutputError(f"SAM stream failed: {payload[:300]}")
        except httpx.StreamError as exc:
            raise ModelApiError(self.service, f"stream interrupted: {exc!r}") from exc
        return "".join(deltas)

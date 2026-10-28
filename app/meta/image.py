"""Muse Image: text-to-image generation for synthetic demo photos.

Docs: <https://dev.meta.ai/docs/image-generation>
API reference: <https://dev.meta.ai/docs/api-reference/images/create-image>

One-off generation uses the OpenAI-compatible single-shot endpoint
``POST /v1/images/generations`` with ``{"model", "prompt", "response_format",
"output_format"}``. The reply carries the image as base64 in
``data[0].b64_json``. Billing is a flat $0.01 per generated image, so the cost
of a call is ``0.01 * len(data)``.
"""

import base64

from app.meta.base import (
    ModelClient,
    ModelOutputError,
    error_from_response,
)

MODEL = "muse-image-1.0"
URL = "https://api.meta.ai/v1/images/generations"
PRICE_PER_IMAGE = 0.01  # flat USD per generated image
# Aspect-ratio hint ("WxH"); the generator picks the exact pixel size.
SIZE = "1536x1024"


class ImageClient(ModelClient):
    service = "Muse Image"
    model = MODEL  # logged with every call

    async def generate(
        self,
        prompt: str,
        *,
        size: str = SIZE,
        output_format: str = "jpeg",
        model: str = MODEL,
    ) -> bytes:
        """Generate one image and return its bytes (JPEG by default)."""
        body = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
            "output_format": output_format,
        }
        headers = {**self.auth_headers(), "Content-Type": "application/json"}

        async def fetch() -> dict:
            response = await self.http.post(URL, headers=headers, json=body)
            if response.status_code != 200:
                raise error_from_response(self.service, response)
            return response.json()

        def check(reply: dict) -> None:
            extract_image(reply)  # raises ModelOutputError: never cache an unusable reply

        key_payload = {"prompt": prompt, "size": size, "output_format": output_format}
        reply = await self._call(
            model, key_payload, fetch, lambda r: PRICE_PER_IMAGE * len(r.get("data", [])), check
        )
        return extract_image(reply)


def extract_image(reply: dict) -> bytes:
    """Pull the image bytes out of an images/generations reply."""
    try:
        item = reply["data"][0]
        b64 = item["b64_json"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ModelOutputError(f"unexpected Muse Image reply shape: {str(reply)[:200]}") from exc
    if not b64:
        raise ModelOutputError("Muse Image returned an empty image")
    try:
        return base64.b64decode(b64)
    except ValueError as exc:
        raise ModelOutputError("Muse Image returned undecodable base64") from exc

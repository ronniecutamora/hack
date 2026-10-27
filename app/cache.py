"""Content-hash disk cache for model responses.

Key = SHA-256 of the model id plus the canonical JSON of the request payload. Callers put a
hash of large binary inputs (images, audio) in the payload instead of the bytes themselves.
Values must be JSON-serializable.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResponseCache:
    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled

    @staticmethod
    def key(model: str, payload: Any) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return sha256_hex(f"{model}\0{canonical}".encode())

    def _path(self, model: str, payload: Any) -> Path:
        digest = self.key(model, payload)
        return self.root / model.replace("/", "_") / digest[:2] / f"{digest}.json"

    def get(self, model: str, payload: Any) -> Any | None:
        if not self.enabled:
            return None
        try:
            return json.loads(self._path(model, payload).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # missing or corrupt entry counts as a miss

    def put(self, model: str, payload: Any, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(model, payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(path)

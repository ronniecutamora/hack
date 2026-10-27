"""Pseudonymization of reporter identities (RA 10173: collect the minimum)."""

import hashlib
import hmac


class MissingSaltError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PSEUDONYM_SALT is not set. Generate one with: openssl rand -hex 32")


def pseudonymize(salt: str, user_id: int | str) -> str:
    """HMAC-SHA256(salt, user id) as hex. The raw id is never stored or logged."""
    if not salt:
        raise MissingSaltError
    return hmac.new(salt.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()

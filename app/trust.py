"""Deterministic review evidence and cost guards; none of these signals is a verdict.

uv run python -m app.trust block <pseudonym-prefix>
uv run python -m app.trust unblock <pseudonym-prefix>
"""

import argparse
import contextlib
import hashlib
import io
import re
import sys
from datetime import datetime, timedelta

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.config import Settings, get_settings
from app.db import init_db, make_engine
from app.gazetteer import find_in_text
from app.geo import barangay_at
from app.models import Media, ModelCall, PhotoAssessment, Report, Reporter, utcnow

HOLD_BLOCKED = "reporter blocked"
HOLD_SPEND = "spend cap"
# Empty string means hashing was attempted but no usable image/file was available.
HASH_UNAVAILABLE = ""


def signal_key(signal: dict) -> str:
    """A stable id for one trust signal of a report (its code and detail), so an officer's
    review of it survives a re-assessment that finds the same evidence again; new evidence
    (another photo, another related report) is a new signal and needs its own review."""
    raw = f"{signal.get('code')}|{signal.get('detail')}".encode()
    return hashlib.blake2b(raw, digest_size=6).hexdigest()


def flagging(signal: dict) -> bool:
    """High and medium signals flag a report until an officer reviews them as genuine."""
    return signal.get("severity") in ("high", "medium")


def model_spend(session: Session, now: datetime | None = None) -> float:
    """Recorded estimated USD in the rolling 24 h window (not a UTC calendar day)."""
    now = now or utcnow()
    return float(
        session.exec(
            select(func.coalesce(func.sum(ModelCall.cost_estimate), 0)).where(
                ModelCall.at > now - timedelta(days=1), ModelCall.at <= now
            )
        ).one()
    )


def hold_reason(session: Session, report: Report, settings: Settings) -> str | None:
    reporter = session.get(Reporter, report.reporter_id)
    if reporter and reporter.blocked:
        return HOLD_BLOCKED
    if model_spend(session) >= settings.daily_spend_cap_usd:
        return HOLD_SPEND
    return None


def photo_hash(data: bytes) -> str | None:
    """64-bit dHash: EXIF-transposed, grayscale, 9x8. Invalid photos stay reportable."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            pixels = list(
                ImageOps.exif_transpose(image)
                .convert("L")
                .resize((9, 8), Image.Resampling.LANCZOS)
                .get_flattened_data()
            )
        bits = 0
        for row in range(8):
            for col in range(8):
                offset = row * 9 + col
                bits = (bits << 1) | (pixels[offset] > pixels[offset + 1])
        return f"{bits:016x}"
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        return None


def photo_taken_at(data: bytes) -> datetime | None:
    """DateTimeOriginal is Manila local time, converted to this database's naive UTC."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            # Cameras normally put this tag in the Exif sub-IFD; tolerate top-level writers.
            value = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x9003)
        if isinstance(value, bytes):
            value = value.decode("ascii")
        return datetime.strptime(value.rstrip("\x00"), "%Y:%m:%d %H:%M:%S") - timedelta(hours=8)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        UnicodeError,
        SyntaxError,
        OverflowError,
    ):
        return None


def _hash_value(value: str | None) -> int | None:
    return int(value, 16) if value and re.fullmatch(r"[0-9a-fA-F]{16}", value) else None


def pending_photo_hashes(engine: Engine, settings: Settings) -> dict[int, str]:
    """Read legacy files before the save transaction; NULL alone means not yet attempted."""
    with Session(engine) as session:
        pending = session.exec(
            select(Media.id, Media.path, Media.purged_at).where(
                Media.kind == "photo", col(Media.phash).is_(None)
            )
        ).all()
    hashes = {}
    for media_id, path, purged_at in pending:
        hashed = None
        if purged_at is None:
            # Missing files and retention races must not cause repeated reads.
            with contextlib.suppress(OSError):
                hashed = photo_hash((settings.data_dir / path).read_bytes())
        hashes[media_id] = hashed or HASH_UNAVAILABLE
    return hashes


def compute_signals(
    session: Session, report: Report, settings: Settings, *, transcript: str | None = None
) -> list[dict]:
    """Compare derived evidence only: indexed exact matches and projected usable hashes.

    No raw file reads or decoding belong in this write transaction. Candidates are ordered
    by receipt time/id, with cross-reporter reuse preferred over same-reporter resends.
    """
    signals: list[dict] = []
    own_photos = session.exec(
        select(Media).where(Media.report_id == report.id, Media.kind == "photo")
    ).all()
    photos = (
        session.exec(
            select(Media.id, Media.report_id, Media.phash).where(
                Media.kind == "photo",
                Media.report_id != report.id,
                col(Media.phash).is_not(None),
                Media.phash != HASH_UNAVAILABLE,
            )
        ).all()
        if any(_hash_value(m.phash) is not None for m in own_photos)
        else []
    )
    for media in own_photos:
        hashed = _hash_value(media.phash)
        matches = {
            candidate.report_id: (False, (hashed ^ other_hash).bit_count())
            for candidate in photos
            if hashed is not None
            and (other_hash := _hash_value(candidate.phash)) is not None
            and (hashed ^ other_hash).bit_count() <= settings.photo_hash_max_distance
        }
        # SHA-256 lookup uses ix_media_sha256, even for photos without a usable dHash.
        for candidate in session.exec(
            select(Media.id, Media.report_id, Media.phash).where(
                Media.sha256 == media.sha256,
                Media.kind == "photo",
                Media.report_id != report.id,
            )
        ).all():
            matches[candidate.report_id] = (True, None)
        if not matches:
            continue
        candidates = session.exec(
            select(Report.id, Report.reporter_id)
            .where(col(Report.id).in_(matches))
            .order_by(Report.received_at, Report.id)
        ).all()
        if not candidates:
            continue
        candidate = next(
            (c for c in candidates if c.reporter_id != report.reporter_id), candidates[0]
        )
        exact, distance = matches[candidate.id]
        reused = candidate.reporter_id != report.reporter_id
        signals.append(
            {
                "code": "photo_reused" if reused else "photo_resent",
                "severity": "high" if reused else "info",
                "detail": f"Photo {media.id}: "
                + ("identical bytes" if exact else f"similar image (dHash distance {distance})")
                + f" in report #{candidate.id}; "
                + ("another reporter" if reused else "same reporter")
                + ". Officer verification required.",
                "related_report_id": candidate.id,
            }
        )

    # Keep capture times/GPS when a re-assessment has no raw photo left to read.
    for photo in session.exec(
        select(PhotoAssessment).where(PhotoAssessment.report_id == report.id)
    ).all():
        if photo.taken_at is not None:
            age = report.received_at - photo.taken_at
            if age > timedelta(days=settings.photo_max_age_days) or age < -timedelta(days=1):
                signals.append(
                    {
                        "code": "photo_old",
                        "severity": "medium",
                        "detail": f"Photo {photo.media_id}: EXIF capture time "
                        f"{photo.taken_at.isoformat()} UTC is outside the allowed age window "
                        "(EXIF can be edited).",
                    }
                )
    points = []
    if report.lat is not None and report.lon is not None:
        points.append(("pin", report.lat, report.lon))
    for photo in session.exec(
        select(PhotoAssessment).where(PhotoAssessment.report_id == report.id)
    ).all():
        if photo.gps_lat is not None and photo.gps_lon is not None:
            points.append((f"photo {photo.media_id} GPS", photo.gps_lat, photo.gps_lon))
    named = find_in_text("\n".join(v for v in (report.text, transcript) if v))
    for source, lat, lon in points:
        barangay = barangay_at(lat, lon)
        if barangay is None:
            signals.append(
                {
                    "code": "pin_outside_binan",
                    "severity": "medium",
                    "detail": f"{source.capitalize()} is outside all Biñan barangay polygons.",
                }
            )
        elif named.barangay is not None and named.barangay != barangay:
            signals.append(
                {
                    "code": "pin_text_mismatch",
                    "severity": "medium",
                    "detail": f"{source.capitalize()} is in {barangay.name}; "
                    f"text or transcript names {named.barangay.name}.",
                }
            )
    return signals


def set_blocked(session: Session, prefix: str, blocked: bool) -> Reporter:
    if not re.fullmatch(r"[a-fA-F0-9]{8,64}", prefix):
        raise ValueError("Use at least 8 hexadecimal characters of a pseudonym (maximum 64).")
    matches = session.exec(
        select(Reporter).where(Reporter.pseudonym.startswith(prefix.lower()))
    ).all()
    if len(matches) != 1:
        raise ValueError("Pseudonym prefix must match exactly one reporter.")
    reporter = matches[0]
    reporter.blocked = blocked
    session.add(reporter)
    session.commit()
    return reporter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold or release a pseudonymous reporter's reports."
    )
    parser.add_argument("action", choices=("block", "unblock"))
    parser.add_argument("prefix")
    args = parser.parse_args(argv)
    engine = make_engine(get_settings())
    init_db(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            # set_blocked validates the prefix and unique match before changing the row.
            with session.no_autoflush:
                previous = session.exec(
                    select(Reporter.blocked).where(
                        Reporter.pseudonym.startswith(args.prefix.lower())
                    )
                ).first()
            reporter = set_blocked(session, args.prefix, args.action == "block")
        old, new = ("yes" if previous else "no"), ("yes" if reporter.blocked else "no")
        print(f"Reporter {reporter.pseudonym[:8]}: blocked: {old} -> {new}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

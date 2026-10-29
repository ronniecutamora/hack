"""Retention: delete raw photos and voice notes after RAW_MEDIA_TTL_HOURS.

Files under DATA_DIR/media with no Media row (left behind when a write failed after the file
was saved) are deleted on the same schedule, so the privacy promise holds for them too.
Spend-cap holds keep raw files for at most HELD_MEDIA_MAX_HOURS from media creation;
blocked holds use the normal TTL. Assessments are skipped only during a one-hour grace period.
Only the raw files go. Reports, transcripts, captions and later assessments are kept, and
Media rows stay (with purged_at set) so the record shows what existed and when it was removed.

The synthetic demo scenario's media (channel "demo") is never purged: its photos are generated
by Muse Image and stamped as such, its reporters are invented, so it holds no personal data,
and a deployed demo must still show its evidence the day after it was seeded. Every real
channel is purged; the demo channel is set only by the demo loader, never by a reporter.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, case, or_
from sqlmodel import Session, col, select

from app.config import Settings, get_settings
from app.db import init_db, make_engine
from app.models import (
    CHANNEL_DEMO,
    STATUS_ASSESSING,
    STATUS_HELD,
    Media,
    Report,
    Reporter,
    utcnow,
)
from app.trust import HOLD_SPEND

logger = logging.getLogger(__name__)
PURGE_INTERVAL_S = 3600
ASSESSING_GRACE_HOURS = 1


def purge_expired(settings: Settings, engine: Engine, now: datetime | None = None) -> int:
    """Delete raw media older than the TTL. Returns the number of files purged."""
    now = now or utcnow()
    cutoff = now - timedelta(hours=settings.raw_media_ttl_hours)
    held_cutoff = now - timedelta(hours=settings.held_media_max_hours)
    assessing_cutoff = now - timedelta(hours=ASSESSING_GRACE_HOURS)
    purged = 0
    with Session(engine) as session:
        expired = session.exec(
            select(Media)
            .join(Report, Report.id == Media.report_id)
            .join(Reporter, Reporter.id == Report.reporter_id)
            .where(
                col(Media.purged_at).is_(None),
                Report.channel != CHANNEL_DEMO,
                or_(
                    Report.status != STATUS_ASSESSING,
                    col(Report.assessing_started_at).is_(None),
                    Report.assessing_started_at <= assessing_cutoff,
                ),
                Media.created_at
                < case(
                    (
                        (Report.status == STATUS_HELD)
                        & (Report.hold_reason == HOLD_SPEND)
                        & (Reporter.blocked == False),  # noqa: E712
                        held_cutoff,
                    ),
                    else_=cutoff,
                ),
            )
        ).all()
        for media in expired:
            file = settings.data_dir / media.path
            file.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                file.parent.rmdir()  # drop the per-report folder once empty
            media.purged_at = now
            session.add(media)
            purged += 1
        session.commit()
    return purged + purge_orphans(settings, engine, cutoff)


def purge_orphans(settings: Settings, engine: Engine, cutoff: datetime) -> int:
    """Delete files under media/ that no Media row points to and are older than the cutoff."""
    media_dir = settings.media_dir
    if not media_dir.is_dir():
        return 0
    with Session(engine) as session:
        known = {settings.data_dir / path for path in session.exec(select(Media.path)).all()}
    purged = 0
    for file in media_dir.glob("*/*"):
        if not file.is_file() or file in known:
            continue
        modified = datetime.fromtimestamp(file.stat().st_mtime, UTC).replace(tzinfo=None)
        if modified < cutoff:  # naive UTC, like utcnow()
            file.unlink()
            with contextlib.suppress(OSError):
                file.parent.rmdir()
            logger.info("purged orphaned raw media file %s", file.name)
            purged += 1
    return purged


async def purge_loop(settings: Settings, engine: Engine) -> None:
    """Background task: purge once an hour until cancelled."""
    while True:
        try:
            count = purge_expired(settings, engine)
            if count:
                logger.info("purged %d raw media files", count)
        except Exception:
            logger.exception("retention purge failed")
        await asyncio.sleep(PURGE_INTERVAL_S)


if __name__ == "__main__":  # uv run python -m app.retention
    _settings = get_settings()
    _engine = make_engine(_settings)
    init_db(_engine)
    print(f"purged {purge_expired(_settings, _engine)} raw media files")

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.db import init_db, make_engine
from app.intake import IntakeService
from app.models import CHANNEL_DEMO, Media, Report, utcnow
from app.retention import purge_expired


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    return engine


async def test_purge_deletes_only_expired_raw_files_and_keeps_derived_text(settings, engine):
    intake = IntakeService(settings, engine)
    old = await intake.ingest_photo(1, b"old-photo", caption="Bubong na wasak")
    new = await intake.ingest_photo(2, b"new-photo")
    with Session(engine) as session:
        old_media = session.exec(select(Media).where(Media.report_id == old.id)).one()
        old_media.created_at = utcnow() - timedelta(hours=25)
        session.add(old_media)
        session.commit()
        old_file = settings.data_dir / old_media.path
        new_file = (
            settings.data_dir
            / session.exec(select(Media).where(Media.report_id == new.id)).one().path
        )
    assert old_file.exists() and new_file.exists()

    assert purge_expired(settings, engine) == 1

    assert not old_file.exists()
    assert not old_file.parent.exists()  # empty per-report folder removed
    assert new_file.exists()
    with Session(engine) as session:
        purged = session.exec(select(Media).where(Media.report_id == old.id)).one()
        assert purged.purged_at is not None
        assert purged.sha256  # the row is kept as a record
        assert session.get(Report, old.id).text == "Bubong na wasak"  # derived text survives
        kept = session.exec(select(Media).where(Media.report_id == new.id)).one()
        assert kept.purged_at is None

    assert purge_expired(settings, engine) == 0  # idempotent


async def test_purge_tolerates_already_missing_file(settings, engine):
    report = await IntakeService(settings, engine).ingest_photo(1, b"x")
    with Session(engine) as session:
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        (settings.data_dir / media.path).unlink()

    later = utcnow() + timedelta(hours=settings.raw_media_ttl_hours + 1)
    assert purge_expired(settings, engine, now=later) == 1


@pytest.mark.parametrize(
    "assessing_minutes,media_hours,purged",
    [(59, 25, 0), (60, 25, 1), (61, 25, 1), (None, 25, 1), (61, 23, 0)],
)
async def test_assessing_retention_grace_is_bounded(
    settings, engine, assessing_minutes, media_hours, purged
):
    report = await IntakeService(settings, engine).ingest_photo(1, b"synthetic")
    now = utcnow()
    with Session(engine) as session:
        current = session.get(Report, report.id)
        current.status = "assessing"
        current.received_at = now - timedelta(hours=100)
        current.assessing_started_at = (
            now - timedelta(minutes=assessing_minutes) if assessing_minutes is not None else None
        )
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        media.created_at = now - timedelta(hours=media_hours)
        path = settings.data_dir / media.path
        session.add_all([current, media])
        session.commit()

    assert purge_expired(settings, engine, now=now) == purged
    assert path.exists() == (not purged)
    with Session(engine) as session:
        media = session.exec(select(Media).where(Media.report_id == report.id)).one()
        assert media.purged_at == (now if purged else None)
        assert session.get(Report, report.id).status == "assessing"


async def test_purge_removes_orphaned_files_after_the_ttl(settings, engine):
    # A file saved before a failed DB write has no Media row; it must still be deleted.
    await IntakeService(settings, engine).ingest_photo(1, b"tracked")
    orphan_dir = settings.data_dir / "media" / "99"
    orphan_dir.mkdir(parents=True)
    old_orphan, fresh_orphan = orphan_dir / "old.ogg", orphan_dir / "fresh.ogg"
    old_orphan.write_bytes(b"voice")
    fresh_orphan.write_bytes(b"voice")
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(old_orphan, (old, old))

    assert purge_expired(settings, engine) == 1

    assert not old_orphan.exists()
    assert fresh_orphan.exists()  # still inside the TTL
    assert len(list((settings.data_dir / "media").glob("*/*"))) == 2  # tracked + fresh


async def test_synthetic_demo_media_is_kept_and_every_real_channel_is_purged(settings, engine):
    # A deployed demo must still show its generated photos the day after seeding; the demo
    # channel holds no personal data. Real channels keep the privacy promise.
    intake = IntakeService(settings, engine)
    demo = await intake.ingest_photo(1, b"generated-photo", channel=CHANNEL_DEMO)
    real = await intake.ingest_photo(2, b"resident-photo", channel="telegram")
    other = await intake.ingest_photo(3, b"another-channel-photo", channel="sms")
    with Session(engine) as session:
        for media in session.exec(select(Media)).all():
            media.created_at = utcnow() - timedelta(hours=settings.raw_media_ttl_hours + 1)
            session.add(media)
        session.commit()
        paths = {m.report_id: settings.data_dir / m.path for m in session.exec(select(Media))}

    assert purge_expired(settings, engine) == 2

    assert paths[demo.id].exists()
    assert not paths[real.id].exists() and not paths[other.id].exists()
    with Session(engine) as session:
        purged = {m.report_id: m.purged_at for m in session.exec(select(Media)).all()}
    assert purged[demo.id] is None
    assert purged[real.id] is not None and purged[other.id] is not None
    assert purge_expired(settings, engine) == 0  # the demo photo stays on every later run
    assert paths[demo.id].exists()

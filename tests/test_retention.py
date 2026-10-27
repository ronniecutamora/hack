from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.db import init_db, make_engine
from app.intake import IntakeService
from app.models import Media, Report, utcnow
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

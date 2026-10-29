"""Database snapshots on a (fake) network volume: round-trips, retention, the lease and restarts."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import init_db, make_engine
from app.intake import IntakeService
from app.main import create_app
from app.models import Report
from app.snapshots import (
    LEASE_NAME,
    RESTORE_REQUEST,
    SnapshotStore,
    healthy,
    snapshot_name,
)


class FakeVolume:
    def __init__(self):
        self.commits = 0
        self.reloads = 0

    def commit(self):
        self.commits += 1

    def reload(self):
        self.reloads += 1


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def volume():
    return FakeVolume()


def store_for(settings, volume, clock, **kwargs):
    return SnapshotStore(
        settings.db_path,
        settings.data_dir / "volume" / "snapshots",
        volume=volume,
        clock=clock,
        sleep=lambda s: clock.advance(s),
        **kwargs,
    )


async def add_reports(settings, texts):
    engine = make_engine(settings)
    init_db(engine)
    intake = IntakeService(settings, engine)
    for i, text in enumerate(texts):
        await intake.ingest_text(100 + i, text)
    engine.dispose()


def report_texts(settings):
    engine = make_engine(settings)
    with Session(engine) as session:
        texts = [r.text for r in session.exec(select(Report).order_by(Report.id)).all()]
    engine.dispose()
    return texts


def wipe_local_disk(settings):
    """What a new container sees: no database file at all."""
    for suffix in ("", "-wal", "-shm"):
        settings.db_path.with_name(settings.db_path.name + suffix).unlink(missing_ok=True)


async def test_backup_restore_backup_restore_keeps_every_row(settings, volume, clock):
    texts = [f"baha report {i}" for i in range(25)]
    await add_reports(settings, [])
    live = sqlite3.connect(settings.db_path)  # an open connection, as in the running app,
    live.execute("SELECT count(*) FROM report").fetchall()  # keeps the WAL from being folded
    await add_reports(settings, texts)
    assert settings.db_path.with_name("app.db-wal").stat().st_size > 0  # rows in the WAL

    first = store_for(settings, volume, clock)
    assert first.open() == "kept"
    snapshot = first.backup()
    live.close()
    assert snapshot is not None and healthy(snapshot)
    assert volume.commits >= 2  # the lease, then the snapshot
    with sqlite3.connect(snapshot) as connection:  # one self-contained file
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    first.close()

    wipe_local_disk(settings)
    clock.advance(60)
    second = store_for(settings, volume, clock)
    assert second.open() == "restored"
    assert report_texts(settings) == texts
    await add_reports(settings, ["after the restart"])
    clock.advance(60)
    second.close()

    wipe_local_disk(settings)
    clock.advance(60)
    third = store_for(settings, volume, clock)
    assert third.open() == "restored"
    assert report_texts(settings) == [*texts, "after the restart"]


async def test_unchanged_database_writes_no_new_snapshot(settings, volume, clock):
    await add_reports(settings, ["one"])
    store = store_for(settings, volume, clock)
    store.open()
    assert store.backup() is not None
    clock.advance(120)
    assert store.backup() is None
    await add_reports(settings, ["two"])
    clock.advance(120)
    assert store.backup() is not None
    assert len(store.snapshots()) == 2
    assert not list(store.dir.glob(".*.tmp"))  # no temp file left behind


def test_empty_volume_and_no_database_starts_empty(settings, volume, clock):
    store = store_for(settings, volume, clock)
    assert store.open() == "empty"
    assert store.open() == "empty"  # idempotent: the lifespan calls it again after Modal's enter
    assert volume.reloads == 1


async def test_a_damaged_newest_snapshot_falls_back_to_an_older_one(settings, volume, clock):
    await add_reports(settings, ["kept"])
    store = store_for(settings, volume, clock)
    store.open()
    store.backup()
    clock.advance(60)
    (store.dir / snapshot_name(clock())).write_bytes(b"not a database")
    store.close()
    wipe_local_disk(settings)
    clock.advance(60)
    assert store_for(settings, volume, clock).open() == "restored"
    assert report_texts(settings) == ["kept"]


def test_retention_keeps_the_newest_and_one_per_day(tmp_path, clock):
    store = SnapshotStore(tmp_path / "app.db", tmp_path / "snaps", keep=2, keep_daily=3)
    store.dir.mkdir()
    start = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    names = [snapshot_name(start + timedelta(hours=6 * i)) for i in range(16)]  # four days
    for name in names:
        (store.dir / name).write_bytes(b"x")
    removed = store.prune()
    left = [p.name for p in store.snapshots()]
    # The newest two (29th 00:00 and 06:00), plus the last of each of the last three days.
    assert left == [names[9], names[13], names[14], names[15]]
    assert len(removed) == 12


async def test_a_restore_request_restores_that_snapshot(settings, volume, clock):
    await add_reports(settings, ["first"])
    store = store_for(settings, volume, clock)
    store.open()
    older = store.backup()
    await add_reports(settings, ["second"])
    clock.advance(60)
    store.close()
    (store.dir / RESTORE_REQUEST).write_text(older.name)

    clock.advance(60)
    again = store_for(settings, volume, clock)
    assert again.open() == "restored"  # even though a local database exists
    assert report_texts(settings) == ["first"]
    assert not (store.dir / RESTORE_REQUEST).exists()
    assert settings.db_path.with_name("app.db.before-restore").exists()


def test_a_restore_request_cannot_name_a_file_outside_the_snapshots(settings, volume, clock):
    store = store_for(settings, volume, clock)
    store.dir.mkdir(parents=True)
    (store.dir / RESTORE_REQUEST).write_text("../../app.db")
    assert store.open() == "empty"


async def test_a_new_container_waits_for_the_old_one_to_release_the_lease(settings, volume, clock):
    await add_reports(settings, ["before"])
    old = store_for(settings, volume, clock, owner="old")
    old.open()
    old.backup()

    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:  # the old container finishes its exit handler meanwhile
            old.close()
        clock.advance(seconds)

    new = SnapshotStore(
        settings.db_path, old.dir, volume=volume, clock=clock, sleep=sleep, owner="new"
    )
    new.open()
    assert len(waits) == 3
    assert new.lease().owner == "new" and not new.lease().released


def test_a_stale_lease_from_a_crashed_container_is_taken_over(settings, volume, clock):
    old = store_for(settings, volume, clock, owner="crashed")
    old.open()
    clock.advance(10 * 60)  # no heartbeat since: it died without releasing
    waits = []
    new = SnapshotStore(
        settings.db_path, old.dir, volume=volume, clock=clock, sleep=waits.append, owner="new"
    )
    new.open()
    assert waits == []


def test_a_held_lease_that_is_never_released_times_out(settings, volume, clock, caplog):
    store_for(settings, volume, clock, owner="stuck").open()
    new = SnapshotStore(
        settings.db_path,
        settings.data_dir / "volume" / "snapshots",
        volume=volume,
        clock=lambda: clock(),  # frozen: the holder's heartbeat stays fresh
        sleep=lambda s: None,
        owner="new",
    )
    new.open(wait_s=10)
    assert "still holds the database lease" in caplog.text
    assert new.lease().owner == "new"


async def test_close_is_final_and_releases_the_lease(settings, volume, clock):
    await add_reports(settings, ["x"])
    store = store_for(settings, volume, clock)
    store.open()
    assert store.close() is not None
    assert store.lease().released
    assert store.close() is None
    assert (store.dir / LEASE_NAME).exists()


async def test_the_app_restores_at_start_and_snapshots_at_shutdown(settings, tmp_path):
    """A container restart: new local disk, same volume, every report still there."""
    shared = tmp_path / "volume"
    first = settings.model_copy(
        update={"database_path": tmp_path / "disk1" / "app.db", "snapshot_dir": shared}
    )
    with TestClient(create_app(first), base_url="http://localhost") as client:
        intake = IntakeService(first, client.app.state.engine)
        await intake.ingest_text(1, "Baha sa Malaban")
        await intake.ingest_text(2, "Bagsak ang puno sa Casile")
    assert len(list(shared.glob("app-*.db"))) == 1  # the shutdown snapshot

    second = first.model_copy(update={"database_path": tmp_path / "disk2" / "app.db"})
    with TestClient(create_app(second), base_url="http://localhost") as client:
        html = client.get("/reports").text
    assert "Baha sa Malaban" in html and "Bagsak ang puno sa Casile" in html

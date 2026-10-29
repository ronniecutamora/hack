"""Durable SQLite where the container's disk is wiped on every restart (a Modal deploy).

The live database stays on local disk, where SQLite's locking and WAL work as designed. A network
volume (Modal Volume: last write wins, no file locks, WAL undocumented) only ever receives whole,
finished snapshot files:

1. **Start:** take the lease (below), then restore the newest healthy snapshot (or the one
   named in a RESTORE request) to the local database path. No snapshot: start empty (a deploy may
   then seed the synthetic demo, app.seed).
2. **Every SNAPSHOT_INTERVAL_MINUTES**, **soon after a report is acknowledged** (`request()`, at
   most one snapshot per SNAPSHOT_SOON_S) and **at shutdown:** an online backup with SQLite's
   backup API (consistent while the app keeps writing) to a temp file on the volume, then an
   atomic rename and a volume commit. An unchanged database writes nothing. The newest
   SNAPSHOT_KEEP spaced at least half an interval apart are kept (so a burst of reports does not
   push the older history out), plus the newest of each of the last SNAPSHOT_KEEP_DAILY days.

Loss window: a crash that skips the shutdown snapshot loses the database writes since the last
snapshot: at most about SNAPSHOT_SOON_S of acknowledged reports, and at most one interval
(default 2 minutes) of everything else (assessments, officer actions). Media files are written
to the volume directly and are committed with each snapshot (and by Modal's background commits
every few seconds).

**Lease:** one writer at a time. A starting container waits (up to LEASE_WAIT_S) for a previous
container that still holds a fresh lease to write its final snapshot and release it, so a
restart or redeploy never restores a snapshot older than the last writes. A crashed holder's
lease goes stale and is taken over.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.config import Settings

logger = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "app-"
SNAPSHOT_SUFFIX = ".db"
LEASE_NAME = "LEASE.json"
SNAPSHOT_NAME = re.compile(
    rf"{re.escape(SNAPSHOT_PREFIX)}\d{{8}}T\d{{12}}Z{re.escape(SNAPSHOT_SUFFIX)}"
)  # what snapshot_name() writes; anything else in the folder is left alone
RESTORE_REQUEST = "RESTORE"  # holds a snapshot file name: restore that one at the next start
LEASE_WAIT_S = 45.0  # Modal gives a stopping container 30 s for its exit handler
LEASE_POLL_S = 2.0
# A requested snapshot (an acknowledged report) runs at once, but at most one per this many
# seconds: a burst of reports costs one snapshot, not one each.
SNAPSHOT_SOON_S = 5.0


class Volume(Protocol):
    """What the store needs from a network volume (modal.Volume has both)."""

    def commit(self) -> None: ...

    def reload(self) -> None: ...


class LocalVolume:
    """A plain directory: nothing to commit or reload (local runs and tests)."""

    def commit(self) -> None:
        pass

    def reload(self) -> None:
        pass


def snapshot_name(when: datetime) -> str:
    return f"{SNAPSHOT_PREFIX}{when:%Y%m%dT%H%M%S%fZ}{SNAPSHOT_SUFFIX}"


def snapshot_time(path: Path) -> datetime:
    stamp = path.name.removeprefix(SNAPSHOT_PREFIX).removesuffix(SNAPSHOT_SUFFIX)
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)


def _read_only(path: Path) -> sqlite3.Connection:
    """A connection that never writes, not even a -wal or -shm file next to `path`."""
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)


def healthy(path: Path) -> bool:
    """A readable SQLite file that passes quick_check (opened read-only, nothing written)."""
    try:
        connection = _read_only(path)
        try:
            return connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def copy_database(source: Path, target: Path, *, source_read_only: bool) -> None:
    """SQLite online backup of `source` into a fresh `target`, stored in rollback-journal mode
    so the copy is one self-contained file."""
    target.unlink(missing_ok=True)
    src = _read_only(source) if source_read_only else sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Lease:
    owner: str
    heartbeat: datetime
    released: bool


class SnapshotStore:
    def __init__(
        self,
        db_path: Path,
        snapshot_dir: Path,
        *,
        volume: Volume | None = None,
        interval_s: float = 120.0,
        soon_s: float = SNAPSHOT_SOON_S,
        keep: int = 15,
        keep_daily: int = 7,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        owner: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.dir = snapshot_dir
        self.volume = volume or LocalVolume()
        self.interval_s = interval_s
        self.soon_s = soon_s
        self.keep = keep
        self.keep_daily = keep_daily
        self.clock = clock
        self.sleep = sleep
        self.owner = owner or uuid.uuid4().hex
        self.opened: str | None = None  # how open() found the database, once it ran
        self.closed = False
        self._last_digest: str | None = None
        self._lock = threading.Lock()
        self._wanted: asyncio.Event | None = None  # set by request(), created by run()

    @classmethod
    def from_settings(cls, settings: Settings, volume: Volume | None = None) -> "SnapshotStore":
        if settings.snapshot_dir is None:
            raise ValueError("SNAPSHOT_DIR is not set")
        return cls(
            settings.db_path,
            settings.snapshot_dir,
            volume=volume,
            interval_s=settings.snapshot_interval_minutes * 60,
            keep=settings.snapshot_keep,
            keep_daily=settings.snapshot_keep_daily,
        )

    # -- listing --

    def snapshots(self) -> list[Path]:
        """Finished snapshots, oldest first (names sort by time)."""
        if not self.dir.is_dir():
            return []
        return sorted(p for p in self.dir.iterdir() if SNAPSHOT_NAME.fullmatch(p.name))

    # -- lease --

    @property
    def _lease_path(self) -> Path:
        return self.dir / LEASE_NAME

    def lease(self) -> Lease | None:
        try:
            data = json.loads(self._lease_path.read_text(encoding="utf-8"))
            return Lease(
                data["owner"], datetime.fromisoformat(data["heartbeat"]), bool(data["released"])
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_lease(self, *, released: bool) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"owner": self.owner, "heartbeat": self.clock().isoformat(), "released": released}
        tmp = self.dir / f".{LEASE_NAME}.{self.owner}.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self._lease_path)

    def _held_by_another(self, lease: Lease | None) -> bool:
        if lease is None or lease.released or lease.owner == self.owner:
            return False
        # A holder refreshes the heartbeat with every snapshot tick; older means it crashed.
        stale_after = timedelta(seconds=2 * self.interval_s + 60)
        return self.clock() - lease.heartbeat < stale_after

    def _acquire(self, wait_s: float) -> None:
        waited = 0.0
        while True:
            self.volume.reload()
            lease = self.lease()
            if not self._held_by_another(lease):
                break
            if waited >= wait_s:
                logger.warning(
                    "the previous container still holds the database lease after %.0f s; "
                    "starting from the newest snapshot (its last writes may be missing)",
                    waited,
                )
                break
            self.sleep(LEASE_POLL_S)
            waited += LEASE_POLL_S
        self._write_lease(released=False)
        self.volume.commit()

    # -- start --

    def open(self, wait_s: float = LEASE_WAIT_S) -> str:
        """Take the lease and make sure a database is in place. Returns how it was found:
        "kept" (a local database exists), "restored" (from a snapshot) or "empty". Idempotent."""
        if self.opened is not None:
            return self.opened
        self._acquire(wait_s)
        request = self.dir / RESTORE_REQUEST
        requested = None
        if request.is_file():
            requested = self.dir / request.read_text(encoding="utf-8").strip()
            if requested.parent != self.dir or not requested.is_file():
                logger.error("RESTORE names %s, which is not a snapshot; ignored", requested.name)
                requested = None
        if requested is None and self.db_path.exists():
            self.opened = "kept"
        else:
            if requested is not None and self.db_path.exists():
                aside = self.db_path.with_name(f"{self.db_path.name}.before-restore")
                os.replace(self.db_path, aside)
                logger.warning("restore requested: kept the current database as %s", aside.name)
            restored = self.restore(requested)
            self.opened = "restored" if restored else "empty"
        if request.is_file():
            request.unlink()
            self.volume.commit()
        return self.opened

    def restore(self, snapshot: Path | None = None) -> Path | None:
        """Copy `snapshot`, or the newest healthy one, to the database path; None if none."""
        candidates = [snapshot] if snapshot else list(reversed(self.snapshots()))
        for candidate in candidates:
            if not healthy(candidate):
                logger.error("snapshot %s is damaged; trying an older one", candidate.name)
                continue
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.db_path.with_name(f".{self.db_path.name}.restore")
            copy_database(candidate, tmp, source_read_only=True)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)
            os.replace(tmp, self.db_path)
            logger.info("restored the database from snapshot %s", candidate.name)
            return candidate
        return None

    # -- snapshots --

    def backup(self) -> Path | None:
        """One snapshot of the live database, committed to the volume, and the lease heartbeat
        refreshed. Returns its path, or None when nothing changed since the last one."""
        with self._lock:
            if not self.db_path.exists():
                return None
            self.dir.mkdir(parents=True, exist_ok=True)
            name = snapshot_name(self.clock())
            tmp = self.dir / f".{name}.tmp"
            copy_database(self.db_path, tmp, source_read_only=False)
            digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
            final: Path | None = None
            if digest == self._last_digest:
                tmp.unlink()
            else:
                _fsync(tmp)
                final = self.dir / name
                os.replace(tmp, final)
                self._last_digest = digest
                self.prune()
            if not self.closed:
                self._write_lease(released=False)
            self.volume.commit()
            return final

    def request(self) -> None:
        """Ask for a snapshot soon: something that must survive a crash was just written (a
        report the bot acknowledged). Coalesced by run(); a no-op while run() is not running."""
        if self._wanted is not None:
            self._wanted.set()

    async def run(self) -> None:
        """Snapshot every interval, and soon after each request(), until cancelled (the web
        app's lifespan runs this)."""
        self._wanted = wanted = asyncio.Event()
        loop = asyncio.get_running_loop()
        last = -self.soon_s  # the first request snapshots at once
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wanted.wait(), timeout=self.interval_s)
                    # Requested: at once, unless the last snapshot was under soon_s ago.
                    await asyncio.sleep(max(0.0, self.soon_s - (loop.time() - last)))
                wanted.clear()  # a request from here on is caught by this snapshot or the next
                try:
                    await asyncio.to_thread(self.backup)
                except Exception:
                    logger.exception("database snapshot failed; the next tick tries again")
                last = loop.time()
        finally:
            self._wanted = None

    def prune(self) -> list[Path]:
        """Delete snapshots beyond the newest `keep` spaced at least half an interval apart
        (the newest always stays) and the newest of each of `keep_daily` days. Returns what was
        deleted."""
        snapshots = self.snapshots()
        kept: set[Path] = set()
        spacing = timedelta(seconds=self.interval_s / 2)
        previous: datetime | None = None
        for path in reversed(snapshots):
            if len(kept) >= self.keep:
                break
            when = snapshot_time(path)
            if previous is None or previous - when >= spacing:
                kept.add(path)
                previous = when
        newest_per_day: dict[str, Path] = {}
        for path in snapshots:
            newest_per_day[path.name[len(SNAPSHOT_PREFIX) : len(SNAPSHOT_PREFIX) + 8]] = path
        days = sorted(newest_per_day)[-self.keep_daily :] if self.keep_daily else []
        kept |= {newest_per_day[day] for day in days}
        removed = [p for p in snapshots if p not in kept]
        for path in removed:
            path.unlink(missing_ok=True)
        return removed

    def close(self) -> Path | None:
        """The final snapshot, then release the lease for the next container. Idempotent."""
        with self._lock:
            if self.closed:
                return None
        try:
            return self.backup()
        finally:
            with self._lock:
                self.closed = True
                with contextlib.suppress(OSError):
                    self._write_lease(released=True)
                self.volume.commit()
                logger.info("database lease released")

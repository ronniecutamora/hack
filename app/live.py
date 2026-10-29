"""Live dashboard updates: one database watcher per web process, fanned out over server-sent events.

The Telegram bot is a separate process writing the same SQLite file, so an in-process signal would
miss its writes. The watcher keeps one dedicated, read-only connection and asks SQLite every
POLL_SECONDS for `PRAGMA data_version`, which changes whenever any *other* connection (the bot,
the CLI, this app's own pool) commits to the file. That check is O(1) and never blocks a writer
(WAL). Only when it changes does the watcher read a small token per topic (below) inside one read
transaction, so a token always describes one consistent state, and it tells subscribers which
topics changed. However many browser tabs are open, there is one poller and one connection.

Events carry only topic names, opaque tokens and a time: never report text, places or reporter
pseudonyms. A client that missed events (a dropped connection, a polling fallback) compares the
tokens it has with the current ones, so it never needs the history.
"""

import asyncio
import hashlib
import json
import logging
import signal
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0  # an SSE comment, so proxies and browsers keep the stream open
RETRY_MS = 3000  # the browser's reconnect delay, sent as the stream's `retry:` field
MAX_SUBSCRIBERS = 64  # open dashboard tabs per process; the app listens on localhost only

# What each page region listens to. A topic's token is a hash of the rows that region shows,
# narrowed to the columns that change; append-only tables only need their size and last id.
# Rows are hashed in Python rather than summed in SQL so no two different states share a token.
TOPICS: dict[str, tuple[str, ...]] = {
    "reports": (
        "SELECT id, status, incident_id, barangay, lat, lon, hold_reason, grouping_error,"
        " grouping_needs_officer, officer_incident_id, officer_barangay,"
        " length(text), length(transcript) FROM report ORDER BY id",
        "SELECT id, blocked FROM reporter ORDER BY id",
        "SELECT id, updated_at, length(signals) FROM assessment ORDER BY id",
        "SELECT id, updated_at FROM photoassessment ORDER BY id",
        "SELECT count(*), max(id), count(purged_at), max(purged_at) FROM media",
    ),
    "incidents": (
        "SELECT id, status, active, barangay, title, last_seen, latest_facts FROM incident"
        " ORDER BY id",
        "SELECT count(*), max(id) FROM agentdecision",
        "SELECT count(*), max(id) FROM officeraction",
    ),
    "drafts": (
        "SELECT id, status, decided_at FROM draft ORDER BY id",
        "SELECT count(*), max(id) FROM draftevent",
    ),
    "spend": ("SELECT count(*), max(id), total(cost_estimate) FROM modelcall",),
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(rows: Any) -> str:
    return hashlib.blake2b(repr(rows).encode(), digest_size=8).hexdigest()


def read_tokens(connection: sqlite3.Connection) -> dict[str, str]:
    """One token per topic, read in a single transaction so they describe one state."""
    connection.execute("BEGIN")
    try:
        return {
            topic: _digest([connection.execute(sql).fetchall() for sql in queries])
            for topic, queries in TOPICS.items()
        }
    finally:
        connection.execute("COMMIT")


class Subscriber:
    """One open event stream: the topics changed since it last read, merged, never queued, so a
    slow tab holds a handful of names however many changes it missed."""

    def __init__(self) -> None:
        self._pending: set[str] = set()
        self._wake = asyncio.Event()

    def notify(self, topics: set[str]) -> None:
        self._pending |= topics
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def next(self, timeout: float) -> set[str] | None:
        """The changed topics, an empty set when woken to close, or None after `timeout`."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            return None
        self._wake.clear()
        topics, self._pending = self._pending, set()
        return topics


class ChangeHub:
    """The one watcher of a web process and the event streams it feeds."""

    def __init__(
        self,
        db_path: Path,
        poll_seconds: float = POLL_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        max_subscribers: int = MAX_SUBSCRIBERS,
    ) -> None:
        self.db_path = db_path
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_subscribers = max_subscribers
        self.tokens: dict[str, str] = {}
        self.changed_at = _now()
        self.closed = False
        self.connections_opened = 0
        self._subscribers: set[Subscriber] = set()
        self._connection: sqlite3.Connection | None = None
        self._file_id: tuple[int, int] | None = None
        self._data_version: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._failing = False

    # -- watching (the blocking parts run in a worker thread, one at a time) --

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            self._connection.close()
        # Autocommit, so a plain SELECT never leaves a read transaction open between polls
        # (an open one would pin the WAL and hide later commits from data_version).
        connection = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False, timeout=5
        )
        connection.execute("PRAGMA query_only = ON")
        stat = self.db_path.stat()
        self._connection, self._file_id = connection, (stat.st_dev, stat.st_ino)
        self._data_version = None
        self.connections_opened += 1
        return connection

    def _check(self) -> set[str]:
        """The topics whose token changed since the last check (empty when nothing committed)."""
        stat = self.db_path.stat()
        connection = self._connection
        if connection is None or self._file_id != (stat.st_dev, stat.st_ino):
            connection = self._connect()  # first run, or the file was replaced (a demo reset)
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        if version == self._data_version:
            return set()
        tokens = read_tokens(connection)
        self._data_version = version
        changed = {t for t, token in tokens.items() if self.tokens.get(t) != token}
        self.tokens = tokens
        return changed

    async def start(self) -> None:
        """Read the first tokens (no event for them), then poll in the background."""
        await asyncio.to_thread(self._check)
        self._task = asyncio.create_task(self._run(), name="live-change-watcher")

    async def _run(self) -> None:
        while not self.closed:
            try:
                changed = await asyncio.to_thread(self._check)
            except (sqlite3.Error, OSError) as exc:
                if not self._failing:  # log once per outage, not every second
                    logger.warning("live updates paused: cannot read the database: %s", exc)
                self._failing, changed = True, set()
                self._file_id = None  # reconnect on the next poll
            else:
                if self._failing:
                    logger.info("live updates resumed")
                self._failing = False
            if changed:
                self.publish(changed)
            await asyncio.sleep(self.poll_seconds)

    def publish(self, topics: set[str]) -> None:
        self.changed_at = _now()
        for subscriber in self._subscribers:
            subscriber.notify(topics)

    def close(self) -> None:
        """End every stream now (server shutdown); later requests get 503."""
        self.closed = True
        for subscriber in self._subscribers:
            subscriber.wake()

    async def stop(self) -> None:
        self.close()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- subscribers --

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def accepting(self) -> bool:
        return not self.closed and len(self._subscribers) < self.max_subscribers

    @contextmanager
    def subscribe(self) -> Iterator[Subscriber]:
        subscriber = Subscriber()
        self._subscribers.add(subscriber)
        try:
            yield subscriber
        finally:
            self._subscribers.discard(subscriber)

    def snapshot(self, topics: set[str] | None = None) -> dict[str, Any]:
        """What a client compares against: per-topic tokens, an overall token and the time of
        the last change. `topics` names what just changed (empty for a full snapshot)."""
        tokens = dict(self.tokens)
        return {
            "topics": sorted(topics or ()),
            "tokens": tokens,
            "token": _digest(sorted(tokens.items())),
            "changed_at": self.changed_at,
        }


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def event_stream(hub: ChangeHub) -> AsyncIterator[str]:
    """An SSE stream: the reconnect delay, a `hello` snapshot, then a `change` event per batch of
    changes and a comment as heartbeat. Subscribing before the snapshot means no change can fall
    between the two. Ends when the hub closes; a client disconnect cancels it, and leaving the
    `with` block removes the subscriber either way."""
    with hub.subscribe() as subscriber:
        yield f"retry: {RETRY_MS}\n\n"
        yield _event("hello", hub.snapshot())
        while not hub.closed:
            topics = await subscriber.next(hub.heartbeat_seconds)
            if hub.closed:
                break
            if topics is None:
                yield ": heartbeat\n\n"
            elif topics:
                yield _event("change", hub.snapshot(topics))


def close_on_shutdown_signals(hub: ChangeHub) -> Callable[[], None]:
    """End the event streams as soon as the server is asked to stop; returns an undo function.

    uvicorn waits for every open response to finish before it runs the lifespan shutdown, so an
    open event stream would hold Ctrl+C or a --reload restart forever. This chains onto the
    server's own SIGINT/SIGTERM handlers: close the streams, then let the server's handler run.
    Signals can only be handled on the main thread; elsewhere (the test client) it does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    loop = asyncio.get_running_loop()
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(hub.close)
        old = previous[signum]
        if callable(old):
            old(signum, frame)
        elif old == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous.items():
            if signal.getsignal(sig) is handler:
                signal.signal(sig, old)

    return restore

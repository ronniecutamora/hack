"""Live updates: one database watcher per process, SSE streams and the polling token.

Writes go through a separate engine, as the bot process would, so the watcher must see commits
from other connections, not in-process signals.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import init_db, make_engine
from app.live import TOPICS, ChangeHub, event_stream
from app.main import create_app
from app.models import Draft, Report, Reporter

SECRET = "Baha sa bahay ni Aling SECRET-NAME-42"
WAIT_S = 5.0


def add_report(settings, text=SECRET) -> int:
    """Commit a report through a new engine (another connection, like the bot's)."""
    engine = make_engine(settings)
    try:
        with Session(engine) as session:
            reporter = Reporter(pseudonym="f" * 64)
            session.add(reporter)
            session.flush()
            report = Report(reporter_id=reporter.id, channel="telegram", text=text)
            session.add(report)
            session.commit()
            return report.id
    finally:
        engine.dispose()


@pytest.fixture
def db(settings):
    engine = make_engine(settings)
    init_db(engine)
    engine.dispose()
    return settings


async def started_hub(settings, **kwargs) -> ChangeHub:
    hub = ChangeHub(settings.db_path, poll_seconds=0.02, **kwargs)
    await hub.start()
    return hub


async def test_two_subscribers_share_one_watcher_and_get_only_topic_names(db):
    hub = await started_hub(db)
    try:
        with hub.subscribe() as first, hub.subscribe() as second:
            assert hub.subscriber_count == 2
            await asyncio.to_thread(add_report, db)
            got = await asyncio.gather(first.next(WAIT_S), second.next(WAIT_S))
            assert got == [{"reports"}, {"reports"}]
            snapshot = json.dumps(hub.snapshot({"reports"}))
            assert "SECRET" not in snapshot and "f" * 64 not in snapshot
        assert hub.subscriber_count == 0
        assert hub.connections_opened == 1  # one poller and one connection for every tab
    finally:
        await hub.stop()


async def test_each_topic_changes_only_for_its_own_tables(db):
    hub = await started_hub(db)
    try:
        before = dict(hub.tokens)
        assert set(before) == set(TOPICS)
        engine = make_engine(db)
        with Session(engine) as session:
            session.add(Draft(kind="damaged_houses", version=1))
            session.commit()
        engine.dispose()
        with hub.subscribe() as subscriber:
            assert await subscriber.next(WAIT_S) == {"drafts"}
        assert hub.tokens["reports"] == before["reports"]
        assert hub.tokens["drafts"] != before["drafts"]
    finally:
        await hub.stop()


async def test_an_idle_database_publishes_nothing_and_streams_send_a_heartbeat(db):
    hub = await started_hub(db, heartbeat_seconds=0.05)
    try:
        stream = event_stream(hub)
        assert await anext(stream) == "retry: 3000\n\n"
        hello = await anext(stream)
        assert hello.startswith("event: hello\ndata: ")
        assert json.loads(hello.split("data: ", 1)[1])["tokens"] == hub.tokens
        assert hub.subscriber_count == 1
        assert await anext(stream) == ": heartbeat\n\n"
        await stream.aclose()  # what a client disconnect does
        assert hub.subscriber_count == 0
    finally:
        await hub.stop()


async def test_closing_the_hub_ends_open_streams(db):
    hub = await started_hub(db, heartbeat_seconds=60)
    stream = event_stream(hub)
    await anext(stream)
    await anext(stream)
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    await hub.stop()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, WAIT_S)
    assert hub.subscriber_count == 0 and not hub.accepting()


async def test_a_replaced_database_file_is_reopened(db):
    hub = await started_hub(db)
    try:
        db.db_path.unlink()
        for suffix in ("-wal", "-shm"):
            db.db_path.with_name(db.db_path.name + suffix).unlink(missing_ok=True)
        engine = make_engine(db)
        init_db(engine)
        engine.dispose()
        with hub.subscribe() as subscriber:
            await asyncio.to_thread(add_report, db)
            assert "reports" in await subscriber.next(WAIT_S)
        assert hub.connections_opened == 2
    finally:
        await hub.stop()


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        client.app.state.live.poll_seconds = 0.02
        client.app.state.live.heartbeat_seconds = 0.2  # wakes the reader so its deadline holds
        yield client


def read_events(lines, until):
    """Parse SSE frames from a line iterator until `until(name, data)` is true or WAIT_S passes."""
    deadline = time.monotonic() + WAIT_S
    name, frames = None, []
    for line in lines:
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            frames.append((name, line[len("data: ") :]))
            if until(name, json.loads(frames[-1][1])):
                return frames
        assert time.monotonic() < deadline, f"no matching event: {frames}"
    raise AssertionError(f"stream ended: {frames}")


def test_events_stream_reports_a_commit_from_another_process(client, settings):
    hub = client.app.state.live
    with client.stream("GET", "/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        lines = response.iter_lines()
        read_events(lines, lambda name, _data: name == "hello")
        add_report(settings)
        frames = read_events(lines, lambda name, data: "reports" in data["topics"])
    name, data = frames[-1]
    assert name == "change" and json.loads(data)["topics"] == ["reports"]
    assert "SECRET" not in "".join(d for _n, d in frames)
    deadline = time.monotonic() + WAIT_S
    while hub.subscriber_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hub.subscriber_count == 0  # the disconnect removed the subscriber


def test_polling_token_changes_after_a_write(client, settings):
    first = client.get("/events/token")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    before = first.json()
    assert set(before["tokens"]) == set(TOPICS)
    add_report(settings)
    deadline = time.monotonic() + WAIT_S
    while (after := client.get("/events/token").json())["token"] == before["token"]:
        assert time.monotonic() < deadline, "token never changed"
        time.sleep(0.02)
    assert after["tokens"]["reports"] != before["tokens"]["reports"]
    assert after["tokens"]["drafts"] == before["tokens"]["drafts"]
    assert "SECRET" not in json.dumps(after)


def test_event_endpoints_are_read_only_and_host_checked(client):
    assert client.post("/events/token").status_code == 405
    assert client.get("/events/token", headers={"host": "evil.example"}).status_code == 400
    client.app.state.live.close()
    assert client.get("/events").status_code == 503


def test_pages_carry_the_render_time_tokens_and_mark_live_regions(client):
    tokens = client.get("/events/token").json()["tokens"]
    for path, region in [
        ("/reports", 'id="live-reports" data-live="reports spend"'),
        ("/incidents", 'id="live-incidents" data-live="incidents reports"'),
        ("/drafts", 'id="live-drafts" data-live="reports incidents drafts"'),
        ("/damaged-houses", 'id="live-damaged-houses" data-live="reports incidents"'),
    ]:
        html = client.get(path).text
        assert region in html, path
        assert "data-live-tokens=" in html and tokens["reports"] in html
        assert "data-live-status" in html and "htmx.min.js" in html

"""Regression coverage for the slice 4 review. All reports and model clients are synthetic."""

import sqlite3

import pytest
from sqlmodel import Session

from app.db import DRAFT_TRIGGERS, init_db, make_engine
from app.models import Draft
from tests.test_rdana import AT, add_incident, add_report, evidence


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def triggers(path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
        return dict(rows.fetchall())


# --- Database triggers -------------------------------------------------------------------


def test_init_db_replaces_older_trigger_definitions(settings, engine):
    path = engine.url.database
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER draft_frozen")
        connection.execute(
            "CREATE TRIGGER draft_frozen BEFORE UPDATE ON draft "
            "BEGIN SELECT RAISE(ABORT, 'old definition'); END"
        )
        connection.execute(
            "CREATE TRIGGER draft_retired BEFORE INSERT ON draft "
            "BEGIN SELECT RAISE(ABORT, 'retired trigger'); END"
        )
    assert "old definition" in triggers(path)["draft_frozen"]

    init_db(engine)
    current = triggers(path)
    assert set(current) == set(DRAFT_TRIGGERS)  # the retired trigger is gone too
    assert "old definition" not in current["draft_frozen"]
    assert (
        current["draft_frozen"] == f"CREATE TRIGGER draft_frozen {DRAFT_TRIGGERS['draft_frozen']}"
    )
    # The new definition behaves: an open draft takes its decision, a decided one is frozen.
    with Session(engine) as session:
        session.add(Draft(kind="rdana", barangay="Malaban", version=1, created_at=AT))
        session.commit()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE draft SET status = 'approved' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            connection.execute("UPDATE draft SET decision_reason = 'edited' WHERE id = 1")


# --- Single-source share -----------------------------------------------------------------


def test_a_value_only_one_reporter_stated_is_single_source(engine):
    incident = add_incident(engine)
    add_report(engine, incident=incident, reporter="a", houses_partially_damaged=40)
    add_report(engine, incident=incident, reporter="b", hours=1, houses_partially_damaged=2)
    add_report(engine, incident=incident, reporter="c", hours=2, houses_partially_damaged=2)
    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 40 and count.single_source_value == 40

    add_report(engine, incident=incident, reporter="d", hours=3, houses_partially_damaged=45)
    add_report(engine, incident=incident, reporter="b", hours=4, houses_partially_damaged=50)
    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 50 and count.single_source_value == 50  # d's 45 is below 50

    add_report(engine, incident=incident, reporter="c", hours=5, houses_partially_damaged=50)
    count = evidence(engine).count("houses_partially_damaged")
    assert count.value == 50 and count.single_source_value == 0  # b and c both say 50

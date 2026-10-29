"""The deadlines table and the countdown, with an injected clock."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.deadlines import DEADLINES, DEADLINES_PATH, countdown, deadline, load
from app.models import manila_time

APP = Path(__file__).parent.parent / "app"


def test_the_table_has_the_rows_in_scope_each_with_its_source():
    assert {key: d.window_text for key, d in DEADLINES.items()} == {
        "pcic_fisheries_nl": "2 calendar days",
        "pcic_fisheries_ci": "7 calendar days",
        "rdana_eoc": "72 hours",
    }
    for row in DEADLINES.values():
        assert row.source and row.url.startswith("https://")
    for key in ("pcic_fisheries_nl", "pcic_fisheries_ci"):
        assert "Fisheries Insurance general information, 2024 V4" in deadline(key).source
        assert deadline(key).caveat.startswith("⚠️")
    assert "7.5.2" in deadline("rdana_eoc").source
    first_notice = deadline("pcic_fisheries_nl").first_notice
    assert "Citizen's Charter 2026" in first_notice["source"]
    assert "FB Messenger" in first_notice["text"]


def test_a_new_row_is_data_only(tmp_path):
    data = json.loads(DEADLINES_PATH.read_text(encoding="utf-8"))
    data["rows"].append(
        {
            "key": "pcic_example_ci",
            "label": "Example claim",
            "amount": 20,
            "unit": "calendar_days",
            "start": "occurrence of loss",
            "source": "Example brochure",
            "url": "https://example.org/brochure.pdf",
        }
    )
    path = tmp_path / "deadlines.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    table = load(path)
    assert table["pcic_example_ci"].window == timedelta(days=20)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"unit": "weeks"}, "bad window"),
        ({"amount": 0}, "bad window"),
        ({"source": " "}, "source"),
        ({"url": "http://insecure.example"}, "source"),
    ],
)
def test_rows_without_a_window_or_source_are_refused(tmp_path, change, message):
    data = json.loads(DEADLINES_PATH.read_text(encoding="utf-8"))
    data["rows"][0].update(change)
    path = tmp_path / "deadlines.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load(path)


# Loss at 23:59 and 00:30 Philippine time: the due date is counted in Manila, not UTC.
@pytest.mark.parametrize(
    "occurred_utc,due_manila",
    [
        (datetime(2026, 10, 3, 15, 59), "2026-10-05 23:59"),  # 23:59 Manila on Oct 3
        (datetime(2026, 10, 3, 16, 30), "2026-10-06 00:30"),  # 00:30 Manila on Oct 4
    ],
)
def test_due_at_across_midnight_in_manila(occurred_utc, due_manila):
    nl = deadline("pcic_fisheries_nl")
    assert manila_time(nl.due_at(occurred_utc)) == due_manila
    assert nl.due_at(occurred_utc) - occurred_utc == timedelta(days=2)
    assert deadline("pcic_fisheries_ci").due_at(occurred_utc) - occurred_utc == timedelta(days=7)


@pytest.mark.parametrize(
    "now_offset,overdue,text",
    [
        (timedelta(0), False, "due in 48h 0m"),
        (timedelta(hours=42, minutes=57), False, "due in 5h 3m"),
        (timedelta(hours=47, minutes=59), False, "due in 0h 1m"),
        (timedelta(hours=48), False, "due in 0h 0m"),  # the window edge is still in time
        (timedelta(hours=48, seconds=1), True, "overdue by 0h 0m"),
        (timedelta(hours=50, minutes=30), True, "overdue by 2h 30m"),
        (timedelta(hours=72), True, "overdue by 1 day"),
        (timedelta(days=12, hours=3), True, "overdue by 10 days"),
    ],
)
def test_countdown_at_the_window_edges_and_when_overdue(now_offset, overdue, text):
    occurred = datetime(2026, 10, 3, 15, 59)
    due = deadline("pcic_fisheries_nl").due_at(occurred)
    result = countdown(due, now=occurred + now_offset)
    assert (result.overdue, result.text) == (overdue, text)
    assert result.due_local == "2026-10-05 23:59"
    assert result.due_iso == "2026-10-05T15:59:00Z"


def test_no_deadline_literal_outside_the_table():
    """Deadlines live only in app/data/deadlines.json; the printed RDANA title is the form's own."""
    literal = re.compile(r"\b\d+\s*(?:calendar days?|hours?)\b")  # "72 HOURS" is printed text
    found = [
        f"{path.relative_to(APP)}:{number}: {line.strip()}"
        for path in sorted([*APP.rglob("*.py"), *APP.rglob("*.html")])
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if literal.search(line) and "N × 24 hours" not in line
    ]
    assert found == []


def test_the_rdana_drafts_page_reads_its_deadline_from_the_table(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings), base_url="http://localhost") as client:
        html = client.get("/drafts").text
    row = deadline("rdana_eoc")
    assert f"{row.label} within 72 hours of {row.start}" in html
    assert "7.5.2" in html

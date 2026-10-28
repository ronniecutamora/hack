"""Trust evidence is visible and escaped in the officer dashboard, without model calls."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import create_app
from app.models import Assessment, Incident, ModelCall, Report, Reporter, utcnow


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def add_report(session, reporter, *, incident_id=None, signals=None, **fields):
    report = Report(reporter_id=reporter.id, channel="telegram", incident_id=incident_id, **fields)
    session.add(report)
    session.flush()
    if signals is not None:
        session.add(Assessment(report_id=report.id, llm_model="fake", signals=signals))
    return report


def test_spend_header_is_visible_even_without_reports(client, settings):
    html = client.get("/reports").text
    assert f"Model spend, last 24 h: $0.00 of ${settings.daily_spend_cap_usd:.2f} cap" in html
    assert "No reports yet" in html


def test_spend_header_counts_rolling_window_and_warns_at_cap(client, settings):
    with Session(client.app.state.engine) as session:
        session.add_all(
            [
                ModelCall(model="fake", latency_ms=0, cached=False, cost_estimate=12.5),
                ModelCall(
                    model="fake",
                    latency_ms=0,
                    cached=False,
                    cost_estimate=100,
                    at=utcnow() - timedelta(hours=25),
                ),
            ]
        )
        session.commit()
    html = client.get("/reports").text
    assert (
        f'<p class="chip warn">Model spend, last 24 h: $12.50 of '
        f"${settings.daily_spend_cap_usd:.2f} cap</p>"
    ) in html


@pytest.mark.parametrize("reason", ["reporter blocked", "spend cap"])
def test_held_status_displays_reason(client, reason):
    with Session(client.app.state.engine) as session:
        reporter = Reporter(pseudonym="a" * 64)
        session.add(reporter)
        session.flush()
        add_report(session, reporter, status="held", hold_reason=reason)
        session.commit()
    html = client.get("/reports").text
    assert '<span class="status-held">held</span>' in html
    assert f'<div class="chip warn">{reason}</div>' in html


def test_reports_show_each_signal_severity_details_and_related_report_link(client):
    with Session(client.app.state.engine) as session:
        reporter = Reporter(pseudonym="a" * 64)
        session.add(reporter)
        session.flush()
        source = add_report(session, reporter)
        add_report(
            session,
            reporter,
            signals=[
                {
                    "code": "photo_reused",
                    "severity": "high",
                    "detail": "same photo <script>alert(1)</script>",
                    "related_report_id": source.id,
                },
                {"code": "photo_old", "severity": "medium", "detail": "captured 30 days ago"},
                {"code": "photo_resent", "severity": "info", "detail": "same reporter"},
            ],
        )
        session.commit()
        source_id = source.id
    html = client.get("/reports").text
    assert 'class="chip alert"' in html and 'class="chip warn"' in html
    assert 'class="chip">\n        photo resent: same reporter' in html
    assert "photo reused: same photo &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "photo old: captured 30 days ago" in html
    assert f"report #{source_id}" in html
    assert f'href="/reports#report-{source_id}"' not in html
    assert "<script>alert(1)</script>" not in html


def legacy_incident(session):
    incident = Incident(
        title="Malaban flood",
        barangay="Malaban",
        first_seen=utcnow(),
        last_seen=utcnow(),
        latest_facts={"report_count": 3},  # no trust keys in an older database
    )
    session.add(incident)
    session.flush()
    return incident


def test_incident_pages_show_stored_distinct_reporters_and_flagged_report_count(client):
    with Session(client.app.state.engine) as session:
        incident = legacy_incident(session)
        incident.latest_facts = {"report_count": 3, "reporter_count": 2, "flagged_report_count": 1}
        session.add(incident)
        a, b = Reporter(pseudonym="a" * 64), Reporter(pseudonym="b" * 64)
        session.add_all([a, b])
        session.flush()
        add_report(
            session,
            a,
            incident_id=incident.id,
            signals=[
                {"code": "photo_reused", "severity": "high", "detail": "image match"},
                {"code": "photo_old", "severity": "medium", "detail": "old photo"},
            ],
        )
        add_report(session, a, incident_id=incident.id, signals=[])
        add_report(
            session,
            b,
            incident_id=incident.id,
            signals=[{"code": "photo_resent", "severity": "info", "detail": "resent"}],
        )
        session.commit()
        incident_id = incident.id
    for path in ("/incidents", f"/incidents/{incident_id}"):
        response = client.get(path)
        assert response.status_code == 200
        assert "2 reporters" in response.text
        assert "1 flagged report (high/medium trust signals)" in response.text
        assert "single source" not in response.text
    html = client.get(f"/incidents/{incident_id}").text
    assert "Why the agent grouped these" in html
    assert "photo reused: image match" in html


def test_single_source_is_based_on_reporters_even_with_multiple_reports(client):
    with Session(client.app.state.engine) as session:
        incident = legacy_incident(session)
        reporter = Reporter(pseudonym="a" * 64)
        session.add(reporter)
        session.flush()
        add_report(session, reporter, incident_id=incident.id)
        add_report(session, reporter, incident_id=incident.id)
        incident.latest_facts = {"report_count": 2, "reporter_count": 1, "flagged_report_count": 0}
        session.add(incident)
        session.commit()
        incident_id = incident.id
    for path in ("/incidents", f"/incidents/{incident_id}"):
        html = client.get(path).text
        assert "1 reporter</span>" in html
        assert "1 reporters" not in html
        assert '<span class="chip warn">single source</span>' in html
        assert "flagged report" not in html


async def test_held_photo_is_visible_to_officer_before_assessment(client, settings):
    from sqlmodel import select

    from app.intake import IntakeService
    from app.models import Media
    from tests.test_assessment import jpeg

    engine = client.app.state.engine
    report = await IntakeService(settings, engine).ingest_photo("synthetic", jpeg())
    with Session(engine) as session:
        stored = session.get(Report, report.id)
        stored.status, stored.hold_reason = "held", "spend cap"
        session.add(stored)
        session.commit()
        media = session.exec(select(Media)).one()
        media_id = media.id
    html = client.get("/reports").text
    assert f'src="/media/{media_id}"' in html
    assert "Awaiting assessment." in html and "spend cap" in html


@pytest.mark.parametrize("stored", [True, False])
def test_dashboard_reads_trust_counts_from_rollup_with_legacy_fallback(client, stored):
    with Session(client.app.state.engine) as session:
        incident = legacy_incident(session)
        if stored:
            incident.latest_facts = {
                "report_count": 2,
                "reporter_count": 7,
                "flagged_report_count": 2,
            }
        session.add(incident)
        session.commit()
        incident_id = incident.id
    for path in ("/incidents", f"/incidents/{incident_id}"):
        html = client.get(path).text
        if stored:
            assert "7 reporters" in html
            assert (
                '<div class="chip warn">2 flagged reports (high/medium trust signals)</div>' in html
            )
        else:
            assert "Reporter count unavailable" in html
            assert "flagged report" not in html
            assert "single source" not in html


@pytest.mark.parametrize("count", [1, 2])
def test_trust_ui_pluralizes_and_hides_zero_flags(client, count):
    with Session(client.app.state.engine) as session:
        incident = legacy_incident(session)
        incident.latest_facts = {
            "report_count": count,
            "reporter_count": count,
            "flagged_report_count": 0,
        }
        session.add(incident)
        session.commit()
        incident_id = incident.id
    for path in ("/incidents", f"/incidents/{incident_id}"):
        html = client.get(path).text
        assert f"{count} reporter{'s' if count != 1 else ''}</span>" in html
        assert "flagged report" not in html
    html = client.get("/incidents").text
    assert f"{count} report{'s' if count != 1 else ''}<div>" in html


@pytest.mark.parametrize("linked", [True, False])
def test_related_report_older_than_report_page_has_no_broken_anchor(client, monkeypatch, linked):
    from app import main

    monkeypatch.setattr(main, "REPORT_LIMIT", 1)
    with Session(client.app.state.engine) as session:
        incident = legacy_incident(session)
        reporter = Reporter(pseudonym="c" * 64)
        session.add(reporter)
        session.flush()
        source = add_report(session, reporter, incident_id=incident.id if linked else None)
        report = add_report(
            session,
            reporter,
            incident_id=incident.id,
            signals=[
                {
                    "code": "photo_reused",
                    "severity": "high",
                    "detail": "synthetic match",
                    "related_report_id": source.id,
                }
            ],
        )
        source_id, incident_id = source.id, incident.id
        report.received_at = source.received_at + timedelta(seconds=1)
        session.add(report)
        session.commit()
    for path in ("/reports", f"/incidents/{incident_id}"):
        html = client.get(path).text
        assert f'href="/reports#report-{source_id}"' not in html
        if linked:
            assert f'href="/incidents/{incident_id}#report-{source_id}"' in html
        else:
            assert f"report #{source_id}" in html

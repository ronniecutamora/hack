"""Admin commands in the live container: reseed the synthetic demo and screen old messages.

The deployed state is built as a deploy builds it: the demo scenario (the small fixture, fake
models) loaded days ago in a scratch dir and installed, then real reports and officer work on
top. The fresh load is the same loader, in-process, with fake models: no Modal, no model call.
"""

import asyncio
import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, col, select

from app import admin, deploy
from app.assessment import AssessmentService
from app.config import Settings
from app.db import init_db, make_engine
from app.demo.loader import load_command
from app.deploy_targets import WEB_CLASS
from app.intake import IntakeService
from app.models import (
    CHANNEL_DEMO,
    DRAFT_APPROVED,
    DRAFT_OPEN,
    OFFICER_SET_PLACE,
    STATUS_NOT_A_REPORT,
    AgentDecision,
    Draft,
    DraftEvent,
    Incident,
    Media,
    ModelCall,
    OfficerAction,
    Report,
    utcnow,
)
from app.nol import countdowns, deadline_rows
from app.nol_form import FISHERIES
from app.reseed import DELETES, ID_GAP, cited_report_ids
from app.snapshots import PINNED_NAME, SnapshotStore, copy_database, is_snapshot_file_name
from tests.test_assessment import FakeLLM, FakeSam
from tests.test_demo_loader import FIXTURE, write_fixture_images
from tests.test_snapshots import FakeVolume

REPO_ROOT = Path(__file__).resolve().parent.parent


def scratch_settings(work: Path) -> Settings:
    return Settings(
        _env_file=None,
        meta_api_key="test-meta-key",
        baseten_api_key="test-baseten-key",
        pseudonym_salt="test-salt",
        data_dir=work,
    )


def load_into(work: Path, **kwargs) -> bool:
    """The demo load the container runs (`app.demo load --anchor-now`), with fake models."""
    write_fixture_images(work)
    settings = scratch_settings(work)
    engine = make_engine(settings)
    try:
        assessor = AssessmentService(settings, engine, FakeLLM(), FakeSam())
        code = load_command(
            settings, scenario_path=FIXTURE, assessor=assessor, anchor_now=True, **kwargs
        )
    finally:
        engine.dispose()
    return code == 0


def demo_ids(engine) -> list[int]:
    with Session(engine) as session:
        return list(session.exec(select(Report.id).where(Report.channel == CHANNEL_DEMO)).all())


def dump(db_path: Path) -> str:
    connection = sqlite3.connect(db_path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def nol_overdue(engine) -> bool:
    """Whether the fisheries Notice of Loss of the demo's earliest report is overdue now."""
    with Session(engine) as session:
        first = session.exec(
            select(Report.received_at)
            .where(Report.channel == CHANNEL_DEMO)
            .order_by(col(Report.received_at))
        ).first()
    ((_, countdown), *_) = countdowns(deadline_rows(FISHERIES, first), utcnow())
    return countdown.overdue


class Deployed:
    """A deployed database: an old demo seed plus real reports and officer work on top."""

    def __init__(self, tmp_path: Path, settings: Settings):
        self.settings = settings
        old = tmp_path / "seed" / "demo"
        old.mkdir(parents=True)
        assert load_into(old, now=utcnow() - timedelta(days=5))  # seeded 5 days ago
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        copy_database(old / "app.db", settings.db_path, source_read_only=False)
        shutil.copytree(old / "media", settings.media_dir)
        self.engine = make_engine(settings)
        init_db(self.engine)
        self.old_demo = demo_ids(self.engine)
        with Session(self.engine) as session:
            self.demo_incident = session.exec(
                select(Report.incident_id).where(
                    Report.channel == CHANNEL_DEMO, col(Report.incident_id).is_not(None)
                )
            ).first()
        assert self.old_demo and self.demo_incident is not None
        self.images = tmp_path / "volume" / "demo-images"
        self.images.mkdir(parents=True)
        self.store = SnapshotStore(
            settings.db_path, tmp_path / "volume" / "snapshots", volume=FakeVolume()
        )
        assert self.store.open() == "kept"

    def add_real_work(self):
        intake = IntakeService(self.settings, self.engine)
        real = asyncio.run(intake.ingest_text(9001, "Baha po dito sa Poblacion, hanggang tuhod."))
        self.real, self.real_reporter = real.id, real.reporter_id
        demo = self.old_demo[0]
        with Session(self.engine) as session:
            incident = Incident(title="Real flood", first_seen=utcnow(), last_seen=utcnow())
            session.add(incident)
            session.flush()
            self.real_incident = incident.id
            row = session.get(Report, real.id)
            row.incident_id = incident.id
            session.add(row)
            # The grouping compared the real report with the demo incident: a real audit row.
            session.add(
                AgentDecision(
                    kind="kept_separate",
                    report_id=real.id,
                    incident_id=incident.id,
                    compared_incident_id=self.demo_incident,
                    decision="different",
                    reason="Different place.",
                )
            )
            session.add(OfficerAction(kind=OFFICER_SET_PLACE, report_id=demo, officer="CDRRMO-1"))
            demo_draft = Draft(
                kind="rdana",
                barangay="Malaban",
                version=1,
                status=DRAFT_APPROVED,
                content={"report_ids": self.old_demo},
            )
            mixed_draft = Draft(
                kind="damaged_houses",
                version=1,
                status=DRAFT_OPEN,
                content={"total": {"report_ids": [demo, real.id]}},
            )
            session.add_all([demo_draft, mixed_draft])
            session.flush()
            self.demo_draft, self.mixed_draft = demo_draft.id, mixed_draft.id
            session.add(DraftEvent(draft_id=demo_draft.id, action="approved", actor="CDRRMO-1"))
            for report_id in (demo, real.id):
                session.add(ModelCall(model="m", latency_ms=1, cached=True, report_id=report_id))
            session.commit()

    def run(self, command="reseed", **kwargs):
        kwargs.setdefault("load_demo", load_into)
        return admin.run(command, self.settings, self.store, images_dir=self.images, **kwargs)

    def real_rows(self):
        connection = sqlite3.connect(self.settings.db_path)
        try:
            return [
                connection.execute("SELECT * FROM report WHERE id = ?", (self.real,)).fetchall(),
                connection.execute(
                    "SELECT * FROM agentdecision WHERE report_id = ?", (self.real,)
                ).fetchall(),
                connection.execute(
                    "SELECT * FROM modelcall WHERE report_id = ?", (self.real,)
                ).fetchall(),
                connection.execute(
                    "SELECT * FROM incident WHERE id = ?", (self.real_incident,)
                ).fetchall(),
            ]
        finally:
            connection.close()


@pytest.fixture
def deployed(tmp_path, settings):
    state = Deployed(tmp_path, settings)
    state.add_real_work()
    yield state
    state.engine.dispose()


def pinned(state) -> list[Path]:
    return sorted(p for p in state.store.dir.iterdir() if PINNED_NAME.fullmatch(p.name))


def test_every_table_has_a_reseed_rule():
    assert set(DELETES) == set(SQLModel.metadata.tables)


def test_cited_report_ids_finds_nested_lists():
    content = {"report_ids": [1], "total": {"pending_report_ids": [2]}, "rows": [{"x": 3}]}
    assert cited_report_ids(content) == {1, 2}


def test_reseed_replaces_only_the_demo_with_a_fresh_load(deployed):
    real_before = deployed.real_rows()
    old_max = max(deployed.old_demo + [deployed.real])
    assert nol_overdue(deployed.engine)  # what the stale deploy shows
    seen = {}

    def load_checking_the_snapshot(work):
        # The snapshot is already on the volume when the load starts, with the old demo in it.
        (snapshot,) = pinned(deployed)
        with sqlite3.connect(snapshot) as connection:
            seen["snapshot"] = [r[0] for r in connection.execute("SELECT id FROM report")]
        return load_into(work)

    result = deployed.run(load_demo=load_checking_the_snapshot)

    assert result["ok"], result["lines"]
    output = "\n".join(result["lines"])
    (snapshot,) = pinned(deployed)
    assert f"snapshot before the change: {snapshot.name}" in output
    assert snapshot.name.endswith("-before-reseed.db") and is_snapshot_file_name(snapshot.name)
    assert set(seen["snapshot"]) == {*deployed.old_demo, deployed.real}
    assert snapshot not in deployed.store.snapshots()  # never restored at a start by itself
    deployed.store.prune()
    assert snapshot.exists()  # never pruned
    assert deployed.store.snapshots()  # and a regular snapshot holds the new state

    # Real data untouched, byte for byte; the demo is new, with ids nothing ever had.
    assert deployed.real_rows() == real_before
    fresh = demo_ids(deployed.engine)
    assert len(fresh) == len(deployed.old_demo)
    assert min(fresh) > old_max + ID_GAP
    assert not nol_overdue(deployed.engine)  # the filing countdowns are live again
    with Session(deployed.engine) as session:
        latest = session.exec(
            select(Report.received_at)
            .where(Report.channel == CHANNEL_DEMO)
            .order_by(col(Report.received_at).desc())
        ).first()
        assert abs(utcnow() - timedelta(hours=1) - latest) < timedelta(minutes=5)
        for media in session.exec(select(Media).where(col(Media.report_id).in_(fresh))).all():
            assert (deployed.settings.data_dir / media.path).is_file()
        assert session.get(Draft, deployed.demo_draft) is None
        assert session.exec(select(DraftEvent)).all() == []
        assert session.get(Draft, deployed.mixed_draft) is not None  # cites a real report
        assert session.exec(select(OfficerAction)).all() == []
        calls = session.exec(select(ModelCall.report_id)).all()
        assert deployed.real in calls and not set(calls) & set(deployed.old_demo)
        # A real decision points at the old demo incident: kept, emptied, retired.
        kept = session.get(Incident, deployed.demo_incident)
        assert kept is not None and not kept.active
        assert session.exec(select(Report).where(col(Report.id).in_(deployed.old_demo))).all() == []
    assert "keeps draft(s)" in output and "keeps incident(s)" in output
    assert "before:" in output and "after:" in output
    # The audit triggers are back.
    connection = sqlite3.connect(deployed.settings.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM draft")
    connection.close()


def test_reseed_is_idempotent(deployed):
    first = deployed.run()
    real_after_first = deployed.real_rows()
    count = len(demo_ids(deployed.engine))
    second = deployed.run()
    assert first["ok"] and second["ok"], second["lines"]
    assert len(demo_ids(deployed.engine)) == count
    assert deployed.real_rows() == real_after_first
    assert len(pinned(deployed)) == 2


def test_dry_run_changes_nothing(deployed):
    before = dump(deployed.settings.db_path)
    called = []
    result = deployed.run(dry_run=True, load_demo=lambda work: called.append(work))
    assert result["ok"]
    assert dump(deployed.settings.db_path) == before
    assert not called and not pinned(deployed)
    output = "\n".join(result["lines"])
    assert "dry run: nothing changed" in output
    assert f"replaces {len(deployed.old_demo)} demo report(s)" in output


def test_a_mixed_incident_refuses_and_is_listed(deployed):
    with Session(deployed.engine) as session:
        row = session.get(Report, deployed.real)
        row.incident_id = deployed.demo_incident
        session.add(row)
        session.commit()
    before = dump(deployed.settings.db_path)
    result = deployed.run()
    assert not result["ok"]
    assert f"#{deployed.demo_incident}" in result["lines"][0]
    assert "both demo and real reports" in result["lines"][0]
    assert dump(deployed.settings.db_path) == before and not pinned(deployed)


def test_an_incident_mixed_during_the_load_rolls_back(deployed):
    """The plan is checked again inside the swap's transaction: a real report that joined a
    demo incident while the fresh demo loaded stops it, and nothing changes."""

    def load_then_mix(work):
        assert load_into(work)
        with Session(deployed.engine) as session:
            row = session.get(Report, deployed.real)
            row.incident_id = deployed.demo_incident
            session.add(row)
            session.commit()
        return True

    media_before = sorted(p.name for p in deployed.settings.media_dir.iterdir())
    before_rows = deployed.real_rows()
    result = deployed.run(load_demo=load_then_mix)
    assert not result["ok"] and "both demo and real reports" in result["lines"][0]
    assert any("(kept)" in line for line in result["lines"])  # the snapshot is named
    assert demo_ids(deployed.engine) == deployed.old_demo
    assert deployed.real_rows()[1:] == before_rows[1:]
    assert sorted(p.name for p in deployed.settings.media_dir.iterdir()) == media_before


def test_a_failed_load_changes_nothing(deployed):
    before = dump(deployed.settings.db_path)
    result = deployed.run(load_demo=lambda work: False)
    assert not result["ok"] and "nothing changed" in result["lines"][0]
    assert dump(deployed.settings.db_path) == before


def test_reseed_refuses_without_the_lease_or_without_a_demo(deployed):
    assert not deployed.run(demo_allowed=False)["ok"]
    lease = deployed.store.dir / "LEASE.json"
    lease.write_text(
        json.dumps({"owner": "other", "heartbeat": utcnow().isoformat(), "released": False})
    )
    before = dump(deployed.settings.db_path)
    result = deployed.run()
    assert not result["ok"] and "lease" in result["lines"][0]
    assert dump(deployed.settings.db_path) == before


def test_a_pinned_snapshot_restores(deployed, tmp_path):
    name = deployed.store.pin("before-reseed").name
    assert deployed.run()["ok"]
    deployed.store.close()
    (deployed.store.dir / "RESTORE").write_text(name)
    deployed.engine.dispose()
    store = SnapshotStore(deployed.settings.db_path, deployed.store.dir, volume=FakeVolume())
    assert store.open() == "restored"
    assert set(demo_ids(make_engine(deployed.settings))) == set(deployed.old_demo)


# screen


def add_old_message(state, text: str) -> int:
    """A message stored before the screen existed (intake would set it aside today)."""
    with Session(state.engine) as session:
        report = Report(reporter_id=state.real_reporter, channel="telegram", text=text)
        session.add(report)
        session.commit()
        return report.id


def test_screen_sets_aside_old_messages_without_printing_them(deployed):
    junk = add_old_message(deployed, "hahahahaha")
    before = dump(deployed.settings.db_path)
    dry = deployed.run("screen", dry_run=True)
    assert dry["ok"] and dump(deployed.settings.db_path) == before and not pinned(deployed)
    assert f"would set aside 1 message(s) as not reports: #{junk}" in dry["lines"]

    done = deployed.run("screen")
    assert done["ok"]
    output = "\n".join(done["lines"])
    assert f"set aside 1 message(s) as not reports: #{junk}" in output
    assert "hahaha" not in output and "Baha" not in output  # counts and ids, never text
    (snapshot,) = pinned(deployed)
    assert snapshot.name.endswith("-before-screen.db") and snapshot.name in output
    with Session(deployed.engine) as session:
        assert session.get(Report, junk).status == STATUS_NOT_A_REPORT
        assert session.get(Report, deployed.real).status != STATUS_NOT_A_REPORT

    again = deployed.run("screen")
    assert again["ok"] and "nothing to set aside" in "\n".join(again["lines"])
    assert len(pinned(deployed)) == 1  # nothing to change: no snapshot


def test_unknown_admin_command_refuses(deployed):
    assert not deployed.run("drop-everything")["ok"]


# the deploy CLI


def test_admin_commands_on_production_need_yes_unless_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        deploy,
        "admin",
        lambda target, command, dry_run: calls.append((target.name, command, dry_run)) or 0,
    )
    assert deploy.main(["reseed", "--target", "production"]) == 2
    assert deploy.main(["screen", "--target", "production"]) == 2
    assert "--yes" in capsys.readouterr().err and not calls
    assert deploy.main(["reseed", "--target", "production", "--dry-run"]) == 0
    assert deploy.main(["screen", "--target", "staging"]) == 0
    assert deploy.main(["reseed", "--target", "production", "--yes"]) == 0
    assert calls == [
        ("production", "reseed", True),
        ("staging", "screen", False),
        ("production", "reseed", False),
    ]


def test_admin_calls_the_live_container_through_the_modal_python(monkeypatch, tmp_path):
    modal = tmp_path / "modal"
    modal.write_text("#!/opt/modal/bin/python3.13\nimport modal\n")
    monkeypatch.setenv("MODAL_BIN", str(modal))
    monkeypatch.delenv("MODAL_PYTHON", raising=False)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["cwd"] = args, kwargs["cwd"]

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    assert deploy.admin(deploy.TARGETS["staging"], "reseed", dry_run=True) == 0
    assert seen["args"] == [
        "/opt/modal/bin/python3.13",
        "-m",
        "deploy.admin_call",
        "ulat-staging",
        "reseed",
        "--dry-run",
    ]
    modal.write_text("#!/usr/bin/env python3\n")
    assert deploy.modal_python() == "python3"
    monkeypatch.setenv("MODAL_PYTHON", "/x/python")
    assert deploy.modal_python() == "/x/python"


def test_the_modal_class_and_its_admin_method_match_the_client():
    source = (REPO_ROOT / "deploy" / "modal_app.py").read_text(encoding="utf-8")
    assert f"class {WEB_CLASS}:" in source
    assert "def admin(self, command: str, dry_run: bool = False)" in source
    client = (REPO_ROOT / "deploy" / "admin_call.py").read_text(encoding="utf-8")
    assert "web.admin.remote(args.command, dry_run=args.dry_run)" in client
    assert set(deploy.ADMIN_COMMANDS) == set(admin.COMMANDS)


def test_restore_accepts_a_pinned_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "run_modal", lambda args, **kw: calls.append(args))
    deploy.restore(deploy.TARGETS["staging"], "app-20261102T010000000000Z-before-reseed.db")
    assert calls[-1][:2] == ["app", "rollover"]
    for bad in ("app-20261102T010000000000Z-../x.db", "app-x-before.db", "../app.db"):
        with pytest.raises(deploy.DeployError):
            deploy.restore(deploy.TARGETS["staging"], bad)

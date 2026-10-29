"""The reused-photo fake (r18), the --anchor-now demo clock and a full fake-client demo load."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.assessment import AssessmentService
from app.demo import SCENARIO_PATH
from app.demo.__main__ import main as demo_main
from app.demo.images import jobs_for, photo_paths
from app.demo.loader import load_command
from app.demo.scenario import (
    ANCHOR_LEAD,
    anchor_shift,
    load_scenario,
    received_at,
    validate,
)
from app.drafts import DraftService
from app.incidents import IncidentService
from app.main import create_app
from app.models import Assessment, Report
from tests.test_assessment import JUDGMENT, FakeSam, facts
from tests.test_demo_loader import (
    FIXTURE,
    assessor_for,
    demo_settings,
    engine_for,
    reports,
    write_fixture_images,
)
from tests.test_incidents import DIFFERENT, FakeJudge
from tests.test_trust import encoded, flood_scene

SCENARIO = load_scenario(SCENARIO_PATH)
BY_KEY = {r.key: r for r in SCENARIO}
PENS = "fish_pens_or_cages"


# --- r18 in the scenario ------------------------------------------------------------------


def test_r18_resends_the_de_la_paz_fish_pen_photo_from_another_barangay():
    r18, r05 = BY_KEY["r18"], BY_KEY["r05"]
    assert validate(SCENARIO) == []
    assert r18.photo_from == "r05" and r18.photo_prompt is None
    assert r18.reporter not in {r.reporter for r in SCENARIO if r.key != "r18"}
    assert r18.barangay == "San Antonio" != r05.barangay
    assert "fish pen" in r18.text and "fish cage" in r18.text
    assert received_at(r18) == max(received_at(r) for r in SCENARIO)


def test_the_other_stories_are_unchanged():
    assert [r.key for r in SCENARIO] == [f"r{n:02d}" for n in range(1, 19)]
    assert all(r.photo_from is None for r in SCENARIO if r.key != "r18")


def test_a_resent_photo_needs_no_new_image_and_reuses_the_file(tmp_path):
    jobs = jobs_for(SCENARIO, tmp_path)
    assert "r18" not in {job.report.key for job in jobs}  # nothing to generate
    paths = photo_paths(SCENARIO, tmp_path)
    assert paths["r18"] == paths["r05"]
    assert set(paths) == {job.report.key for job in jobs} | {"r18"}


@pytest.mark.parametrize(
    "change,message",
    [
        ({"photo_from": "r99"}, "photo_from must name another report with a photo"),
        ({"photo_from": "r03"}, "photo_from must name another report with a photo"),  # no photo
        ({"photo_from": "r18"}, "photo_from must name another report with a photo"),
        ({"photo_prompt": "A lake"}, "cannot also have its own photo_prompt"),
    ],
)
def test_the_validator_checks_photo_from(change, message):
    changed = [replace(r, **change) if r.key == "r18" else r for r in SCENARIO]
    assert any(message in e for e in validate(changed))


def test_the_scenario_needs_a_reused_photo_beat():
    changed = [replace(r, photo_from=None) if r.key == "r18" else r for r in SCENARIO]
    assert any("resending another reporter's photo" in e for e in validate(changed))


# --- --anchor-now -------------------------------------------------------------------------


def test_anchor_now_puts_the_latest_report_just_before_now_and_keeps_every_gap():
    now = datetime(2026, 11, 2, 3, 27, 41)
    shift = anchor_shift(SCENARIO, now)
    times = [received_at(r, shift) for r in SCENARIO]
    latest = max(times)
    assert now - timedelta(days=1) <= latest <= now
    assert now - latest - ANCHOR_LEAD < timedelta(minutes=1)
    assert shift.total_seconds() % 60 == 0
    plain = [received_at(r) for r in SCENARIO]
    assert [b - a for a, b in zip(plain, plain[1:], strict=False)] == [
        b - a for a, b in zip(times, times[1:], strict=False)
    ]


def test_the_loader_anchors_only_when_asked(tmp_path, capsys):
    now = datetime(2026, 11, 2, 3, 27, 41)
    for name, anchor in (("plain/demo", False), ("anchored/demo", True)):
        settings = demo_settings(tmp_path, name)
        engine = engine_for(settings)
        write_fixture_images(settings.data_dir)
        assessor = assessor_for(settings, engine)
        assert (
            load_command(
                settings, scenario_path=FIXTURE, assessor=assessor, anchor_now=anchor, now=now
            )
            == 0
        )
        times = [r.received_at for r in reports(engine)]
        expected = [received_at(r) for r in load_scenario(FIXTURE)]
        if anchor:
            assert now - timedelta(days=1) <= max(times) <= now
            assert "anchored to now" in capsys.readouterr().out
        else:
            assert times == expected
            assert "anchored" not in capsys.readouterr().out


def test_the_cli_passes_anchor_now(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.demo.loader.load_command", lambda settings, **kwargs: seen.update(kwargs) or 0
    )
    assert demo_main(["load", "--anchor-now"]) == 0
    assert seen["anchor_now"] is True
    assert demo_main(["load"]) == 0
    assert seen["anchor_now"] is False


# --- A full fake-client load --------------------------------------------------------------


class ScenarioLLM:
    """Facts by keyword, so fisherfolk reports state fish-pen damage; a fixed photo judgment."""

    model = "fake-scenario-llm"

    def __init__(self):
        self.calls = []

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("image") is not None:
            return JUDGMENT
        if "fish" in kwargs["user_text"].lower():
            return facts(hazards=["strong_wind"], livelihood_damage=[PENS])
        return facts(hazards=["flood"])


def test_a_fake_load_flags_r18_only_and_its_notice_of_loss_shows_the_red_banner(tmp_path):
    settings = demo_settings(tmp_path)
    engine = engine_for(settings)
    for seed, job in enumerate(jobs_for(SCENARIO, settings.data_dir / "images"), start=1):
        job.path.parent.mkdir(parents=True, exist_ok=True)
        job.path.write_bytes(encoded(flood_scene(seed * 7)))  # distinct synthetic photos
    llm = ScenarioLLM()
    judge = FakeJudge(*[DIFFERENT] * 50)
    assessor = AssessmentService(
        settings, engine, llm, FakeSam(), IncidentService(settings, engine, judge)
    )
    assert load_command(settings, assessor=assessor) == 0

    with Session(engine) as session:
        by_text = {r.text: r for r in session.exec(select(Report)).all()}
        r05, r18 = by_text[BY_KEY["r05"].text], by_text[BY_KEY["r18"].text]
        signals = {a.report_id: a.signals or [] for a in session.exec(select(Assessment)).all()}
    reused = [s for s in signals[r18.id] if s["code"] == "photo_reused"]
    assert reused and reused[0]["severity"] == "high"
    assert reused[0]["related_report_id"] == r05.id and "identical bytes" in reused[0]["detail"]
    # Assessed as it arrived, the original report saw no later copy.
    assert not [s for s in signals[r05.id] if s["code"] == "photo_reused"]
    assert sum(any(s["code"] == "photo_reused" for s in v) for v in signals.values()) == 1
    assert r18.barangay == "San Antonio" and r18.incident_id != r05.incident_id

    draft = DraftService(settings, engine, None).generate_nol("fisheries", r18.incident_id)
    assert draft.content["reused_photos"] == [{"report_id": r18.id, "related_report_id": r05.id}]
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        page = client.get(f"/drafts/{draft.id}").text
    assert '<div class="banner alert keep"><strong>PHOTO REUSED.</strong>' in page
    original = DraftService(settings, engine, None).generate_nol("fisheries", r05.incident_id)
    assert original.content["reused_photos"] == []

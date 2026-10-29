"""The reused-photo fake (r18), the --anchor-now demo clock and a full fake-client demo load."""

import json
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
from tests.test_incidents import DIFFERENT, SAME, FakeJudge
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


def test_the_fisherfolk_report_and_follow_up_fall_inside_the_two_day_window():
    r05, r15, r18 = BY_KEY["r05"], BY_KEY["r15"], BY_KEY["r18"]
    assert (r05.day, r15.day, r18.day) == (13, 14, 14)
    assert 12 <= r05.hour < 18 and r15.hour < 12 and r18.hour >= 12  # afternoon, morning
    assert received_at(r05) < received_at(r15) < received_at(r18)
    assert received_at(r18) - received_at(r05) < timedelta(days=2)


def test_r18_is_its_own_fish_pen_place_not_the_san_antonio_roof_reports():
    r18 = BY_KEY["r18"]
    assert r18.place and all(r.place != r18.place for r in SCENARIO if r.key != "r18")
    assert "bahay" not in r18.text.lower() and "yero" not in r18.text.lower()
    shared = [replace(r, place="casile-purok2-houses") if r.key == "r18" else r for r in SCENARIO]
    assert any("r18: a resent photo needs its own place" in e for e in validate(shared))
    unplaced = [replace(r, place=None) if r.key == "r18" else r for r in SCENARIO]
    assert any("r18: a resent photo needs its own place" in e for e in validate(unplaced))


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


def test_anchor_now_refuses_an_already_loaded_demo(tmp_path, capsys):
    settings = demo_settings(tmp_path)
    engine = engine_for(settings)
    write_fixture_images(settings.data_dir)
    kwargs = {"scenario_path": FIXTURE, "assessor": assessor_for(settings, engine)}
    assert load_command(settings, **kwargs) == 0
    capsys.readouterr()
    assert load_command(settings, anchor_now=True, **kwargs) == 2
    out = capsys.readouterr().out
    assert "already loaded" in out and "--reset --anchor-now" in out
    assert len(reports(engine)) == 3


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


class LivelihoodJudge(FakeJudge):
    """ "same" only when both sides state the same livelihood damage at the same place."""

    async def chat_json(self, **kwargs):
        sent = json.loads(kwargs["user_text"])
        new = sent["new_report"]["facts"]
        old = [r["facts"] for r in sent["incident_recent_reports"]]
        if new.get("livelihood_damage") and all(
            o.get("livelihood_damage") == new["livelihood_damage"] for o in old
        ):
            return SAME
        return DIFFERENT


def fake_load(tmp_path, **kwargs):
    settings = demo_settings(tmp_path)
    engine = engine_for(settings)
    for seed, job in enumerate(jobs_for(SCENARIO, settings.data_dir / "images"), start=1):
        job.path.parent.mkdir(parents=True, exist_ok=True)
        job.path.write_bytes(encoded(flood_scene(seed * 7)))  # distinct synthetic photos
    judge = LivelihoodJudge()
    assessor = AssessmentService(
        settings, engine, ScenarioLLM(), FakeSam(), IncidentService(settings, engine, judge)
    )
    assert load_command(settings, assessor=assessor, **kwargs) == 0
    return settings, engine


def test_an_anchored_load_gives_the_de_la_paz_notice_a_live_clock(tmp_path):
    now = datetime(2026, 11, 2, 3, 27)
    settings, engine = fake_load(tmp_path, anchor_now=True, now=now)
    statuses = DraftService(settings, engine, None, clock=lambda: now).nol_statuses()
    by_place = {n.incident.barangay: n for n in statuses}
    assert set(by_place) == {"De La Paz", "San Antonio"}
    de_la_paz = by_place["De La Paz"]
    assert len(de_la_paz.incident.report_ids) == 2  # the damage report and its follow-up
    (nl, clock), _claim = de_la_paz.clocks
    assert clock.due_at > now and not clock.overdue and clock.text.startswith("due in 2")


def test_a_fake_load_flags_r18_only_and_its_notice_of_loss_shows_the_red_banner(tmp_path):
    settings, engine = fake_load(tmp_path)

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

"""`python -m app.demo replay`: live timestamps, pacing, the $0 cache proof and Ctrl-C."""

import asyncio
import json
import os
import signal
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import Session, select

from app.assessment import AssessmentService
from app.config import Settings
from app.db import init_db, make_engine
from app.demo import SCENARIO_PATH, require_demo_dir
from app.demo.__main__ import main as demo_main
from app.demo.images import jobs_for
from app.demo.loader import MANIFEST_NAME, clear_demo_data
from app.demo.replay import (
    DEFAULT_DIR,
    MAX_GAP_S,
    MIN_GAP_S,
    StoryClock,
    StoryClockJudge,
    replay_command,
    replay_settings,
    schedule,
)
from app.demo.scenario import load_scenario, received_at
from app.incidents import IncidentService
from app.meta.sam import URL as SAM_URL
from app.meta.spark import URL as SPARK_URL
from app.models import ModelCall, Report, utcnow
from app.pair_judge import judge_pair
from tests.test_assessment import JUDGMENT, FakeLLM, FakeSam, facts
from tests.test_demo_loader import demo_settings, gray_jpeg
from tests.test_incidents import SAME, FakeJudge
from tests.test_sam import STREAM, sse_response

FIXTURE = Path(__file__).parent / "fixtures" / "demo_scenario_replay.json"
REPORTS = load_scenario(FIXTURE)


def replay_dir(tmp_path, name="demo-replay") -> Settings:
    settings = demo_settings(tmp_path, name)
    for job in jobs_for(REPORTS, settings.data_dir / "images"):
        job.path.parent.mkdir(parents=True, exist_ok=True)
        job.path.write_bytes(gray_jpeg())
    return settings


def rows(settings):
    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        return session.exec(select(Report).order_by(Report.id)).all()


class Fakes:
    """An assessor factory with fake models; the pair judge still runs on story time."""

    def __init__(self):
        self.judge = FakeJudge()
        self.made = 0

    def __call__(self, settings, engine, clock, recorder=None):
        self.made += 1
        return AssessmentService(
            settings,
            engine,
            FakeLLM(facts(hazards=["flood"])),
            FakeSam(),
            IncidentService(settings, engine, StoryClockJudge(self.judge, clock)),
        )


def run(settings, *, pauses=None, **kwargs):
    async def pause(seconds):
        if pauses is not None:
            pauses.append(seconds)

    lines = []
    code = replay_command(
        settings,
        scenario_path=FIXTURE,
        seed_from=None,
        pause=pause,
        say=lines.append,
        **kwargs,
    )
    return code, "\n".join(lines)


# --- pacing ---------------------------------------------------------------------------------


def test_the_default_pace_plays_the_real_scenario_in_two_to_four_minutes():
    scenario = load_scenario(SCENARIO_PATH)
    plan = schedule(scenario)
    assert [r.key for r, _ in plan] == [r.key for r in sorted(scenario, key=received_at)]
    waits = [wait for _, wait in plan]
    assert waits[0] == 0 and all(MIN_GAP_S <= w <= MAX_GAP_S for w in waits[1:])
    assert 120 <= sum(waits) <= 240


def test_speed_scales_the_gaps_inside_the_clamp():
    # k1 -> k2 is 13 story hours, k2 -> k3 and k3 -> k4 are 25.
    assert [w for _, w in schedule(REPORTS, speed=3600 * 2)] == [0, 6.5, 12.5, 12.5]
    assert [w for _, w in schedule(REPORTS, speed=3600 * 13)] == [0, 4.0, 4.0, 4.0]
    assert [w for _, w in schedule(REPORTS, speed=360)] == [0, 20.0, 20.0, 20.0]
    with pytest.raises(ValueError):
        schedule(REPORTS, speed=0)


# --- a replay with fake models --------------------------------------------------------------


def test_replay_ingests_in_order_at_real_time_and_honours_the_speed(tmp_path):
    settings = replay_dir(tmp_path)
    fakes, pauses = Fakes(), []
    before = utcnow()
    code, out = run(settings, pauses=pauses, speed=7200, allow_paid=True, make=fakes)
    after = utcnow()

    assert code == 0
    assert pauses == pytest.approx([6.5, 12.5, 12.5], abs=0.5)  # minus processing time
    stored = rows(settings)
    assert [r.text for r in stored] == [r.text for r in REPORTS]
    times = [r.received_at for r in stored]
    assert times == sorted(times)
    assert all(before <= t <= after for t in times)  # real arrival times, not scenario times
    assert all(r.status == "assessed" for r in stored)
    manifest = json.loads((settings.data_dir / MANIFEST_NAME).read_text())
    assert manifest == {r.key: s.id for r, s in zip(REPORTS, stored, strict=True)}
    assert "[ 1/4] k1  Malaban" in out and "[ 4/4] k4  no barangay" in out
    assert "Replay finished: 4 sent (0 already sent)" in out
    assert fakes.made == 1  # --allow-paid skips the dry run

    # The pair judge compared k2 with k1 on story time, not on replay time.
    sent = json.loads(fakes.judge.calls[0]["user_text"])
    assert sent["incident_recent_reports"][0]["observed_at"] == received_at(REPORTS[0]).isoformat()
    assert sent["new_report"]["observed_at"] == received_at(REPORTS[1]).isoformat()

    code, out = run(settings, allow_paid=True, make=fakes)
    assert code == 0 and "already replayed" in out and len(rows(settings)) == 4


def test_ctrl_c_stops_after_the_report_in_progress_and_a_rerun_resumes(tmp_path):
    settings = replay_dir(tmp_path)
    fakes = Fakes()

    async def ctrl_c(seconds):
        os.kill(os.getpid(), signal.SIGINT)  # the replay's handler turns it into a stop
        await asyncio.sleep(0.05)  # as during a real pause, the loop handles the signal

    lines = []
    code = replay_command(
        settings,
        scenario_path=FIXTURE,
        seed_from=None,
        allow_paid=True,
        make=fakes,
        pause=ctrl_c,
        say=lines.append,
    )
    out = "\n".join(lines)
    assert code == 0
    assert "Stopping after the report in progress" in out
    assert "Replay stopped: 1 sent" in out and "again to resume" in out
    stored = rows(settings)
    assert len(stored) == 1 and stored[0].status == "assessed"
    assert json.loads((settings.data_dir / MANIFEST_NAME).read_text()) == {"k1": stored[0].id}

    code, out = run(settings, allow_paid=True, make=fakes)
    assert code == 0 and "4 sent" not in out and "3 sent (1 already sent)" in out
    assert [r.text for r in rows(settings)] == [r.text for r in REPORTS]


def test_a_rerun_finishes_assessing_a_report_a_hard_stop_left_behind(tmp_path):
    settings = replay_dir(tmp_path)
    fakes = Fakes()

    class DiesOnK2(Fakes):
        def __call__(self, settings, engine, clock, recorder=None):
            assessor = super().__call__(settings, engine, clock, recorder)
            assess = assessor.assess

            async def die_on_second(report_id):
                if report_id == 2:
                    raise KeyboardInterrupt  # a second Ctrl-C, or the process killed
                return await assess(report_id)

            assessor.assess = die_on_second
            return assessor

    with pytest.raises(KeyboardInterrupt):
        run(settings, allow_paid=True, make=DiesOnK2())
    assert [r.status for r in rows(settings)] == ["assessed", "new"]

    code, out = run(settings, allow_paid=True, make=fakes)
    assert code == 0 and "2 sent (2 already sent)" in out
    stored = rows(settings)
    assert [r.text for r in stored] == [r.text for r in REPORTS]  # nothing sent twice
    assert all(r.status == "assessed" for r in stored)


# --- the $0 proof with real clients ---------------------------------------------------------


def spark_side_effect(request):
    body = json.loads(request.content)
    name = body["response_format"]["json_schema"]["name"]
    content = {
        "DamageReportFacts": facts(hazards=["flood"], flood_depth="knee"),
        "DamageJudgement": JUDGMENT,
        "SameIncident": SAME,
    }[name]
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
        },
    )


@respx.mock
def test_replay_refuses_uncached_paid_calls_then_a_second_take_costs_nothing(tmp_path):
    settings = replay_dir(tmp_path)
    spark = respx.post(SPARK_URL).mock(side_effect=spark_side_effect)
    sam = respx.post(SAM_URL).mock(side_effect=lambda request: sse_response(STREAM))

    # Nothing is cached: the dry run refuses before anything is written or paid for.
    code, out = run(settings)
    assert code == 1 and "Refusing" in out and "muse-spark-1.3" in out
    assert not spark.called and not sam.called
    assert rows(settings) == []

    # One paid take fills the cache, the pair judge included.
    code, out = run(settings, allow_paid=True)
    assert code == 0 and "4 sent" in out
    paid, sam_paid = spark.call_count, sam.call_count
    names = [json.loads(c.request.content)["response_format"]["json_schema"]["name"]
             for c in spark.calls]  # fmt: skip
    assert "SameIncident" in names

    # A fresh take at a later wall-clock time: the dry run passes and nothing is paid.
    code, out = run(settings, reset=True, assume_yes=True)
    assert code == 0, out
    assert "All replies are cached" in out
    assert (spark.call_count, sam.call_count) == (paid, sam_paid)
    assert "0 uncached, new spend $0.0000" in out
    engine = make_engine(settings)
    with Session(engine) as session:
        calls = session.exec(select(ModelCall)).all()
    assert calls and all(c.cached for c in calls)  # the reset kept only the cache


# --- the story clock and the command --------------------------------------------------------


async def test_the_story_clock_keeps_the_pair_prompt_byte_identical():
    contexts = [{"facts": {"hazards": ["flood"]}, "observed_at": "2026-10-30T01:02:03.456789"}]
    new = {"facts": {"hazards": ["flood"]}, "observed_at": "2026-10-30T01:02:09.000001"}
    direct, proxied = FakeJudge(), FakeJudge()
    clock = StoryClock()
    await judge_pair(direct, contexts, new)
    await judge_pair(StoryClockJudge(proxied, clock), contexts, new)
    assert proxied.calls[0]["user_text"] == direct.calls[0]["user_text"]  # unknown times stay

    clock.times["2026-10-30T01:02:03.456789"] = "2026-10-03T19:10:00"
    await judge_pair(StoryClockJudge(proxied, clock), contexts, new)
    story = json.loads(proxied.calls[1]["user_text"])
    assert story["incident_recent_reports"][0]["observed_at"] == "2026-10-03T19:10:00"
    assert story["new_report"]["observed_at"] == new["observed_at"]


def test_replay_uses_its_own_dir_and_never_the_loaded_demo(tmp_path):
    defaults = Settings(_env_file=None)
    assert replay_settings(defaults).data_dir == DEFAULT_DIR
    assert replay_settings(demo_settings(tmp_path, "demo-x")).data_dir == tmp_path / "demo-x"
    assert require_demo_dir(demo_settings(tmp_path, "demo-replay"), "replay").name == "demo-replay"
    code, out = run(demo_settings(tmp_path, "demo"))
    assert code == 2 and "loaded demo" in out
    code, out = run(demo_settings(tmp_path, "var"))
    assert code == 2


def test_replay_reports_missing_images_and_seeds_them_from_another_demo_dir(tmp_path):
    source = replay_dir(tmp_path / "src", "demo")
    target = demo_settings(tmp_path, "demo-replay")
    code, out = run(target, allow_paid=True, make=Fakes())
    assert code == 1 and "Missing 1 image(s) for: k1" in out
    lines = []
    code = replay_command(
        target,
        scenario_path=FIXTURE,
        seed_from=source.data_dir,
        allow_paid=True,
        make=Fakes(),
        pause=lambda s: _noop(),
        say=lines.append,
    )
    assert code == 0 and "Copied 1 missing image/cache file(s)" in "\n".join(lines)
    assert len(list((source.data_dir / "images").iterdir())) == 1  # the source is untouched


async def _noop():
    return None


def test_the_cli_passes_replay_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.demo.replay.replay_command", lambda settings, **kwargs: seen.update(kwargs) or 0
    )
    assert demo_main(["replay", "--speed", "3600", "--allow-paid", "--reset", "--yes"]) == 0
    assert seen["speed"] == 3600 and seen["allow_paid"] and seen["reset"] and seen["assume_yes"]
    assert demo_main(["replay"]) == 0
    assert (
        seen["speed"] == 7200 and not seen["allow_paid"] and seen["seed_from"] == Path("var/demo")
    )


def test_replay_times_are_hours_not_days_apart():
    times = [received_at(r) for r in REPORTS]
    assert times[1] - times[0] == timedelta(hours=13)


def test_a_reset_keeps_the_cache_with_a_relative_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, data_dir=Path("var/demo-replay"))
    cached = settings.cache_dir / "muse-spark-1.3" / "ab" / "reply.json"
    cached.parent.mkdir(parents=True)
    cached.write_text("{}")
    (settings.data_dir / "images").mkdir()
    (settings.data_dir / "app.db").write_text("")
    clear_demo_data(settings, settings.data_dir.resolve())
    assert cached.is_file() and (settings.data_dir / "images").is_dir()
    assert not (settings.data_dir / "app.db").exists()

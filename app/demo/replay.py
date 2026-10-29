"""Replay the demo scenario live, so the officer dashboard fills up on camera.

    uv run python -m app.demo replay [--speed 7200] [--reset] [--allow-paid] [--seed-from var/demo]

Every scenario report goes through the same intake and assessment path as ``load``, but it
arrives now: ``received_at`` is the real arrival time (no backdating), so the live dashboard
animates and every filing countdown starts as if the report had just arrived. The
gaps between reports are the scenario's gaps divided by ``--speed`` (scenario seconds per real
second), each clamped to MIN_GAP_S..MAX_GAP_S, so the 14-day story plays in about 3 minutes.

**$0 by design.** Model replies come from the cache. The one prompt that carries time, the
incident pair judge's ``observed_at``, is shown the story's own time (``StoryClockJudge``):
the judge sees the days between reports that the scenario describes, and its prompts are the
same as a plain ``load``'s, so a replay after one ``load`` (or one paid replay) is answered
from the cache, gives the same grouping decisions every take, and costs nothing. Before
anything is written, a dry run of the whole scenario with cache-only clients proves it; any
uncached call makes the replay refuse unless ``--allow-paid`` is given.

The replay uses its own data dir (default ``var/demo-replay``; DATA_DIR may name another
"demo-*" dir, never ``var/demo``). Missing generated photos and cached replies are copied from
``--seed-from`` (default ``var/demo``), which is only read. Ctrl-C once stops after the report
in progress and leaves a consistent database; running again resumes where it stopped.
"""

import asyncio
import json
import logging
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, case, func
from sqlmodel import Session, col, select

from app.assessment import AssessmentService
from app.config import Settings
from app.db import init_db, make_engine, make_recorder
from app.demo import SCENARIO_PATH, DemoDirError, require_demo_dir
from app.demo.images import photo_paths
from app.demo.loader import (
    _close,
    _read_manifest,
    clear_demo_data,
    feed_one,
    missing_images,
)
from app.demo.scenario import ScenarioReport, load_scenario, received_at, validate
from app.incidents import IncidentService
from app.intake import IntakeService
from app.meta.base import ModelClient, Recorder
from app.meta.factory import make_agent_llm, make_pair_judge, make_sam
from app.meta.llm import AgentLLM
from app.models import STATUS_ASSESSING, STATUS_NEW, ModelCall, Report, utcnow

DEFAULT_DIR = Path("var/demo-replay")
DEFAULT_SEED = Path("var/demo")
# Scenario seconds per real second: 2 story hours a second plays the 14 days in ~3 minutes.
DEFAULT_SPEED = 7200.0
# Clamp every gap: long enough to see each report land, short enough to keep the video moving.
MIN_GAP_S, MAX_GAP_S = 4.0, 20.0

Pause = Callable[[float], Awaitable[None]]
AssessorFactory = Callable[[Settings, Engine, "StoryClock", Recorder | None], AssessmentService]


# --- pacing ---------------------------------------------------------------------------------


def schedule(
    reports: list[ScenarioReport],
    speed: float = DEFAULT_SPEED,
    min_gap: float = MIN_GAP_S,
    max_gap: float = MAX_GAP_S,
) -> list[tuple[ScenarioReport, float]]:
    """Reports in scenario order, each with the real seconds to wait before sending it."""
    if speed <= 0:
        raise ValueError("speed must be positive")
    ordered = sorted(reports, key=received_at)
    plan, previous = [], None
    for report in ordered:
        wait = 0.0
        if previous is not None:
            gap = (received_at(report) - received_at(previous)).total_seconds() / speed
            wait = min(max(gap, min_gap), max_gap)
        plan.append((report, wait))
        previous = report
    return plan


# --- the story clock ------------------------------------------------------------------------


class StoryClock:
    """Maps each replayed report's real arrival time to its scenario time (ISO strings)."""

    def __init__(self) -> None:
        self.times: dict[str, str] = {}

    def remember(self, engine: Engine, report_ids: list[int], report: ScenarioReport) -> None:
        story = received_at(report).isoformat()
        with Session(engine) as session:
            for report_id in report_ids:
                row = session.get(Report, report_id)
                if row is not None:
                    self.times[row.received_at.isoformat()] = story

    def retime(self, value: Any) -> Any:
        """Swap every known ``observed_at`` in a JSON-like value for its scenario time."""
        if isinstance(value, dict):
            return {
                k: (self.times.get(v, v) if k == "observed_at" and isinstance(v, str)
                    else self.retime(v))
                for k, v in value.items()
            }  # fmt: skip
        if isinstance(value, list):
            return [self.retime(v) for v in value]
        return value


class StoryClockJudge:
    """The pair judge, shown story time instead of replay time (see the module docstring).

    The request is rebuilt exactly as ``app.pair_judge.judge_pair`` serialises it, so the cache
    key equals that of a plain ``load`` of the same scenario.
    """

    def __init__(self, inner: AgentLLM, clock: StoryClock) -> None:
        self.inner, self.clock = inner, clock
        self.model = inner.model

    async def chat_json(self, **kwargs: Any) -> dict[str, Any]:
        try:
            sent = json.loads(kwargs["user_text"])
        except (KeyError, ValueError):
            return await self.inner.chat_json(**kwargs)
        user_text = json.dumps(self.clock.retime(sent), ensure_ascii=False, sort_keys=True)
        return await self.inner.chat_json(**{**kwargs, "user_text": user_text})

    async def aclose(self) -> None:
        await self.inner.aclose()


def replay_assessor(
    settings: Settings, engine: Engine, clock: StoryClock, recorder: Recorder | None = None
) -> AssessmentService:
    """``make_assessor`` with the pair judge on story time."""
    judge = StoryClockJudge(make_pair_judge(settings, recorder), clock)
    return AssessmentService(
        settings,
        engine,
        make_agent_llm(settings, recorder),
        make_sam(settings, recorder),
        IncidentService(settings, engine, judge),
    )


def model_clients(assessor: AssessmentService) -> list[ModelClient]:
    """The real clients behind an assessor (the pair judge unwrapped); fakes are skipped."""
    judge = assessor.incidents.judge
    clients = [assessor.llm, assessor.sam, getattr(judge, "inner", judge)]
    return [c for c in clients if isinstance(c, ModelClient)]


# --- playing --------------------------------------------------------------------------------


@dataclass
class Progress:
    sent: int = 0
    assessed: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: bool = False


async def play(
    reports: list[ScenarioReport],
    settings: Settings,
    engine: Engine,
    data_dir: Path,
    assessor: AssessmentService,
    clock: StoryClock,
    *,
    speed: float,
    pause: Pause,
    stop: asyncio.Event,
    images_dir: Path | None = None,
    say: Callable[[str], None] = print,
) -> Progress:
    """Send the reports in scenario order with paced gaps; resume past ones already sent."""
    progress = Progress()
    plan = schedule(reports, speed)
    manifest = _read_manifest(data_dir)
    with Session(engine) as session:
        manifest = {k: v for k, v in manifest.items() if session.get(Report, v) is not None}
    by_key = {r.key: r for r in reports}
    for key, report_id in manifest.items():  # a resumed replay keeps its story times
        if key in by_key:
            clock.remember(engine, [report_id], by_key[key])
    await _finish_interrupted(assessor, engine, list(manifest.values()))
    photos = photo_paths(reports, images_dir or data_dir / "images")
    intake = IntakeService(settings, engine)
    total = len(plan)
    last_sent = time.monotonic()
    for number, (report, wait) in enumerate(plan, start=1):
        if report.key in manifest:
            progress.skipped += 1
            continue
        if progress.sent and wait:
            # The gap runs from the previous report's arrival: its processing time counts.
            await pause(max(0.0, wait - (time.monotonic() - last_sent)))
        if stop.is_set():
            progress.stopped = True
            break
        photo = photos[report.key].read_bytes() if report.key in photos else None
        last_sent = time.monotonic()
        outs = await feed_one(
            intake,
            engine,
            assessor,
            report,
            photo,
            manifest,
            data_dir,
            backdate=False,
            after_ingest=lambda ids, r=report: clock.remember(engine, ids, r),
        )
        progress.sent += 1
        for out in outs:
            if out.status == "assessed":
                progress.assessed += 1
            else:
                progress.failed += 1
        say(_line(number, total, report, engine, manifest[report.key]))
    return progress


async def _finish_interrupted(
    assessor: AssessmentService, engine: Engine, report_ids: list[int]
) -> None:
    """A hard stop can leave a sent report unassessed; assess it before going on."""
    with Session(engine) as session:
        pending = session.exec(
            select(Report.id).where(
                col(Report.id).in_(report_ids),
                col(Report.status).in_([STATUS_NEW, STATUS_ASSESSING]),
            )
        ).all()
    for report_id in pending:
        await assessor.assess(report_id)


def _line(number: int, total: int, report: ScenarioReport, engine: Engine, report_id: int) -> str:
    with Session(engine) as session:
        row = session.get(Report, report_id)
    barangay = (row.barangay if row else None) or "no barangay"
    status = row.status if row else "missing"
    incident = f"incident #{row.incident_id}" if row and row.incident_id else "no incident"
    return f"[{number:2}/{total}] {report.key}  {barangay:<14} {status:<18} {incident}"


async def interruptible_sleep(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake at once when `stop` is set."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


@contextmanager
def stop_on_ctrl_c(stop: asyncio.Event, say: Callable[[str], None]) -> Iterator[None]:
    """First Ctrl-C finishes the report in progress, then stops; a second one aborts."""
    loop = asyncio.get_running_loop()

    def first() -> None:
        say("Stopping after the report in progress (Ctrl-C again to abort)...")
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)

    try:
        loop.add_signal_handler(signal.SIGINT, first)
    except (NotImplementedError, RuntimeError):  # no signal handlers on this platform/thread
        yield
        return
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGINT)


# --- the cache proof ------------------------------------------------------------------------


async def uncached_calls(
    reports: list[ScenarioReport],
    settings: Settings,
    make: AssessorFactory,
) -> list[str]:
    """Play the whole scenario in a throwaway dir with cache-only clients; list refused calls.

    The dry run reads the replay dir's images and model cache and writes nothing to either.
    """
    with tempfile.TemporaryDirectory(prefix="replay-dry-run-") as tmp:
        dry_dir = Path(tmp) / "demo-dry-run"
        dry_dir.mkdir()
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        (dry_dir / "model_cache").symlink_to(settings.cache_dir.resolve(), target_is_directory=True)
        dry = settings.model_copy(update={"data_dir": dry_dir})
        engine = make_engine(dry)
        init_db(engine)
        clock = StoryClock()
        assessor = make(dry, engine, clock, None)
        clients = model_clients(assessor)
        for client in clients:
            client.cache_only, client.uncached = True, []
        app_log = logging.getLogger("app")
        level = app_log.level
        app_log.setLevel(logging.ERROR)  # refused calls are counted, not logged
        try:
            await play(
                reports,
                dry,
                engine,
                dry_dir,
                assessor,
                clock,
                speed=DEFAULT_SPEED,
                pause=_no_pause,
                stop=asyncio.Event(),
                images_dir=settings.data_dir / "images",
                say=lambda _line: None,
            )
        finally:
            app_log.setLevel(level)
            await _close_all(assessor)
            engine.dispose()
        return [model for client in clients for model in client.uncached]


async def _no_pause(_seconds: float) -> None:
    return None


async def _close_all(assessor: AssessmentService) -> None:
    await _close(assessor)
    aclose = getattr(assessor.incidents.judge, "aclose", None)
    if aclose is not None:
        await aclose()


# --- the command ----------------------------------------------------------------------------


def replay_settings(settings: Settings) -> Settings:
    """Use DATA_DIR when it was set, else the replay's own default dir."""
    if "data_dir" in settings.model_fields_set:
        return settings
    return settings.model_copy(update={"data_dir": DEFAULT_DIR})


def seed(data_dir: Path, cache_dir: Path, source: Path | None, say: Callable[[str], None]) -> None:
    """Copy generated photos and cached replies missing here from another demo dir (read-only)."""
    if source is None or not source.is_dir() or source.resolve() == data_dir.resolve():
        return
    copied = 0
    for name, target in (("images", data_dir / "images"), ("model_cache", cache_dir)):
        origin = source / name
        if not origin.is_dir():
            continue
        for path in origin.rglob("*"):
            destination = target / path.relative_to(origin)
            if path.is_file() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                copied += 1
    if copied:
        say(f"Copied {copied} missing image/cache file(s) from {source} (left unchanged).")


def _spend_since(engine: Engine, since) -> tuple[int, int, float]:
    """Model calls logged since `since`: (calls, uncached calls, USD)."""
    with Session(engine) as session:
        calls, uncached, spend = session.exec(
            select(
                func.count(),
                # case(), not sum(cached): a summed Boolean column comes back as a bool
                func.coalesce(func.sum(case((col(ModelCall.cached), 0), else_=1)), 0),
                func.coalesce(func.sum(ModelCall.cost_estimate), 0.0),
            ).where(ModelCall.at >= since)
        ).one()
    return int(calls), int(uncached), float(spend)


def replay_command(
    settings: Settings,
    *,
    speed: float = DEFAULT_SPEED,
    reset: bool = False,
    assume_yes: bool = False,
    allow_paid: bool = False,
    seed_from: Path | None = DEFAULT_SEED,
    scenario_path: Path = SCENARIO_PATH,
    make: AssessorFactory = replay_assessor,
    pause: Pause | None = None,
    say: Callable[[str], None] = print,
) -> int:
    """Run the replay (see module docstring); returns a process exit code."""
    settings = replay_settings(settings)
    try:
        data_dir = require_demo_dir(settings, "replay")
    except DemoDirError as exc:
        say(str(exc))
        return 2
    if data_dir.name == "demo":
        say(
            f"Refusing: '{data_dir}' is the loaded demo; replay writes live times into its own "
            "dir. Leave DATA_DIR unset (var/demo-replay) or name another demo-* dir."
        )
        return 2
    if speed <= 0:
        say("--speed must be positive.")
        return 2
    if reset and data_dir.exists():
        if not assume_yes:
            answer = input(
                f"Delete the replay data in '{data_dir}' (keeps images and cache)? [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                say("Cancelled; replay data left alone.")
                return 0
        clear_demo_data(settings, data_dir)
        say(f"Deleted replay data in {data_dir}; kept images and the model cache.")
    data_dir.mkdir(parents=True, exist_ok=True)
    seed(data_dir, settings.cache_dir, seed_from, say)
    reports = load_scenario(scenario_path)
    problems = validate(reports) if scenario_path == SCENARIO_PATH else []
    if problems:
        say("Scenario is invalid:\n" + "\n".join(f"  - {p}" for p in problems))
        return 1
    missing = missing_images(reports, data_dir)
    if missing:
        say(f"Missing {len(missing)} image(s) for: {', '.join(missing)}.")
        say("Generate them in var/demo first (python -m app.demo images), or pass --seed-from.")
        return 1
    plan = schedule(reports, speed)
    sent = set(_read_manifest(data_dir))
    if sent >= {r.key for r in reports}:
        say(f"Everything is already replayed in {data_dir}. Start over with --reset.")
        return 0
    if not allow_paid:
        say("Checking that every model reply is cached (a dry run of the whole scenario)...")
        refused = asyncio.run(uncached_calls(reports, settings, make))
        if refused:
            counts = ", ".join(f"{n} x {m}" for m, n in sorted(_count(refused).items()))
            say(
                f"Refusing: the replay would make {len(refused)} or more uncached paid "
                f"call(s) ({counts}). Run `python -m app.demo load` in a demo dir first and "
                "--seed-from it, or pass --allow-paid once to pay for them."
            )
            return 1
        say("All replies are cached: this replay makes no paid call.")
    length = sum(wait for _report, wait in plan)
    say(
        f"Replaying {len(plan)} reports into {data_dir} at {speed:g}x "
        f"(about {length / 60:.1f} min). Dashboard: DATA_DIR={settings.data_dir} "
        "uv run uvicorn app.main:app"
    )
    return asyncio.run(_run(reports, settings, data_dir, speed, make, pause, say))


async def _run(reports, settings, data_dir, speed, make, pause, say) -> int:
    engine = make_engine(settings)
    init_db(engine)
    started_at, started = utcnow() - timedelta(seconds=1), time.monotonic()
    clock = StoryClock()
    assessor = make(settings, engine, clock, make_recorder(engine))
    stop = asyncio.Event()
    try:
        with stop_on_ctrl_c(stop, say):
            progress = await play(
                reports,
                settings,
                engine,
                data_dir,
                assessor,
                clock,
                speed=speed,
                pause=pause or (lambda seconds: interruptible_sleep(stop, seconds)),
                stop=stop,
                say=say,
            )
    finally:
        await _close_all(assessor)
    calls, uncached, spend = _spend_since(engine, started_at)
    engine.dispose()
    elapsed = time.monotonic() - started
    verb = "stopped" if progress.stopped else "finished"
    say(
        f"Replay {verb}: {progress.sent} sent ({progress.skipped} already sent) in "
        f"{elapsed / 60:.1f} min; assessed {progress.assessed}, failed {progress.failed}; "
        f"{calls} model calls, {uncached} uncached, new spend ${spend:.4f}."
    )
    if progress.stopped:
        say("Run the same command again to resume.")
    return 0


def _count(models: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in models:
        counts[model] = counts.get(model, 0) + 1
    return counts

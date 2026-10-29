"""Feed the scenario through the real intake and assessment pipeline.

``python -m app.demo load [--reset] [--yes] [--anchor-now]`` ingests every scenario
report with ``channel="demo"`` into a separate demo database (DATA_DIR must end in
"demo"), backdates it to scenario time and assesses it before the next one arrives, in scenario
time order, as the bot would: a check such as "this photo was already sent by another reporter" sees
only earlier reports. A manifest in the demo data dir maps scenario keys to report ids,
so re-running skips loaded reports instead of duplicating them.

Scenario times are fixed (a fictional landfall in early October 2026). ``--anchor-now``
moves them all so the latest report arrives an hour before the load, which makes the
filing countdowns live in a fresh load. It needs ``--reset`` once anything is loaded,
since loaded reports keep their times (the command refuses otherwise). Production code
never shifts the clock.
"""

import argparse
import asyncio
import json
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.assessment import AssessmentService, make_assessor
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.demo import SCENARIO_PATH, DemoDirError, require_demo_dir
from app.demo.geo import pick_point
from app.demo.images import jobs_for, photo_paths
from app.demo.scenario import (
    ScenarioReport,
    anchor_shift,
    load_scenario,
    received_at,
    validate,
)
from app.intake import IntakeService
from app.models import CHANNEL_DEMO, Report, manila_time, utcnow

MANIFEST_NAME = "manifest.json"
CHANNEL = CHANNEL_DEMO


def _manifest_path(data_dir: Path) -> Path:
    return data_dir / MANIFEST_NAME


def _read_manifest(data_dir: Path) -> dict[str, int]:
    try:
        return json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_manifest(data_dir: Path, manifest: dict[str, int]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _manifest_path(data_dir).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


async def _ingest_one(
    intake: IntakeService,
    engine: Engine,
    report: ScenarioReport,
    photo: bytes | None,
    shift: timedelta = timedelta(0),
    *,
    backdate: bool = True,
) -> list[int]:
    """Ingest one scenario report (photo + pin when present) and backdate it to scenario time
    (unless `backdate` is False: the replay keeps the real arrival time).

    Returns the ids of the reports created (normally one; the pin joins it).
    """
    if photo is not None:
        main = await intake.ingest_photo(
            report.reporter, photo, caption=report.text, channel=CHANNEL
        )
    else:
        main = await intake.ingest_text(report.reporter, report.text, channel=CHANNEL)
    assert main.id is not None
    ids = [main.id]
    if report.barangay is not None:
        lat, lon = pick_point(report.barangay, report.key, place=report.place)
        pin = await intake.ingest_location(report.reporter, lat, lon, channel=CHANNEL)
        assert pin.id is not None
        if pin.id not in ids:
            ids.append(pin.id)
    if not backdate:
        return ids
    when = received_at(report, shift)
    with Session(engine) as session:
        for report_id in ids:
            row = session.get(Report, report_id)
            if row is not None:
                row.received_at = when
                session.add(row)
        session.commit()
    return ids


async def feed_one(
    intake: IntakeService,
    engine: Engine,
    assessor: AssessmentService,
    report: ScenarioReport,
    photo: bytes | None,
    manifest: dict[str, int],
    data_dir: Path,
    *,
    shift: timedelta = timedelta(0),
    backdate: bool = True,
    after_ingest: Callable[[list[int]], None] | None = None,
) -> list[Report]:
    """Ingest one report, record it in the manifest, then assess what it created.

    The manifest is written before assessment, so an interrupted run never ingests a report
    twice; `after_ingest` runs between the two. Returns the assessed reports.
    """
    ids = await _ingest_one(intake, engine, report, photo, shift, backdate=backdate)
    manifest[report.key] = ids[0]
    _write_manifest(data_dir, manifest)
    if after_ingest is not None:
        after_ingest(ids)
    # Assess exactly the reports this report created, before the next one arrives.
    assessed = []
    for report_id in ids:
        out = await assessor.assess(report_id)
        if out is not None:
            assessed.append(out)
    return assessed


async def _close(assessor: AssessmentService) -> None:
    for client in (assessor.llm, assessor.sam):
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


async def load_reports(
    reports: list[ScenarioReport],
    settings: Settings,
    engine: Engine,
    data_dir: Path,
    *,
    assessor: AssessmentService | None = None,
    shift: timedelta = timedelta(0),
) -> dict:
    """Ingest and assess new reports one at a time, in scenario order; return a summary."""
    manifest = _read_manifest(data_dir)
    images = photo_paths(reports, data_dir / "images")
    intake = IntakeService(settings, engine)
    owned = assessor is None
    assessor = assessor or make_assessor(settings, engine, make_recorder(engine))
    loaded, existing = 0, 0
    try:
        with Session(engine) as session:
            known = {
                report_id
                for report_id in manifest.values()
                if session.get(Report, report_id) is not None
            }
        manifest = {k: v for k, v in manifest.items() if v in known}
        assessed, failed = 0, 0
        # In scenario time order, as the reports would reach the bot.
        for report in sorted(reports, key=received_at):
            if report.key in manifest:
                existing += 1
                continue
            photo = None
            if report.key in images:
                photo = images[report.key].read_bytes()
            outs = await feed_one(
                intake, engine, assessor, report, photo, manifest, data_dir, shift=shift
            )
            loaded += 1
            for out in outs:
                if out.status == "assessed":
                    assessed += 1
                else:
                    failed += 1
        with Session(engine) as session:
            barangays = sorted(
                {
                    row.barangay
                    for row in session.exec(
                        select(Report).where(col(Report.id).in_(list(manifest.values())))
                    ).all()
                    if row.barangay
                }
            )
        return {
            "loaded": loaded,
            "existing": existing,
            "assessed": assessed,
            "failed": failed,
            "barangays": barangays,
        }
    finally:
        if owned:
            await _close(assessor)


def load_command(
    settings: Settings,
    *,
    reset: bool = False,
    assume_yes: bool = False,
    scenario_path: Path = SCENARIO_PATH,
    assessor: AssessmentService | None = None,
    anchor_now: bool = False,
    now: datetime | None = None,
) -> int:
    """Run the load (see module docstring); returns a process exit code."""
    try:
        data_dir = require_demo_dir(settings, "load")
    except DemoDirError as exc:
        print(exc)
        return 2
    if reset and data_dir.exists():
        if not assume_yes:
            answer = input(
                f"Delete demo data in '{data_dir}' (keeps generated images and the model "
                "cache) and reload? [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled; demo data left alone.")
                return 0
        clear_demo_data(settings, data_dir)
        print(f"Deleted demo data in {data_dir}; kept generated images and the model cache.")
    engine = make_engine(settings)
    init_db(engine)
    reports = load_scenario(scenario_path)
    problems = validate(reports) if scenario_path == SCENARIO_PATH else []
    if problems:
        print("Scenario is invalid:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    missing = missing_images(reports, data_dir)
    if missing:
        print(f"Missing {len(missing)} image(s) for: {', '.join(missing)}.")
        print("Generate them first: DATA_DIR=var/demo uv run python -m app.demo images")
        return 1
    if anchor_now and _read_manifest(data_dir):
        print(
            "--anchor-now moves every report's time, but reports are already loaded at their "
            "old times. Reload from scratch: DATA_DIR=var/demo uv run python -m app.demo load "
            "--reset --anchor-now"
        )
        return 2
    shift = anchor_shift(reports, now or utcnow()) if anchor_now else timedelta(0)
    if anchor_now:
        latest = max(received_at(r, shift) for r in reports)
        print(f"Scenario times anchored to now: the latest report arrives {manila_time(latest)}.")
    summary = asyncio.run(
        load_reports(reports, settings, engine, data_dir, assessor=assessor, shift=shift)
    )
    print(
        f"Reports loaded: {summary['loaded']} "
        f"({summary['existing']} already loaded), "
        f"assessed: {summary['assessed']}, failed: {summary['failed']}."
    )
    found = ", ".join(summary["barangays"]) or "-"
    print(f"Barangays found: {found}.")
    return 0


def clear_demo_data(settings: Settings, data_dir: Path) -> None:
    """Delete a demo dir's databases, uploads and manifest; keep its images and model cache.

    Generated photos and cached model responses cost credits to make again, and a reload of
    the same scenario reuses both.
    """
    # Both sides resolved: with a relative DATA_DIR, the unresolved cache path never matched
    # the listed (absolute) one, so a reset deleted the cache it promised to keep.
    data_dir = data_dir.resolve()
    kept = {data_dir / "images", settings.cache_dir.resolve()}
    for path in data_dir.iterdir():
        if path in kept and path.is_dir() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def missing_images(reports: list[ScenarioReport], data_dir: Path) -> list[str]:
    """Keys of the reports whose generated photo is not in the demo dir yet."""
    return [
        job.report.key for job in jobs_for(reports, data_dir / "images") if not job.path.is_file()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the demo scenario into a demo database.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the demo data (keeps generated images and the model cache)",
    )
    parser.add_argument("--yes", action="store_true", help="delete without asking")
    add_anchor_argument(parser)
    args = parser.parse_args(argv)
    return load_command(
        get_settings(), reset=args.reset, assume_yes=args.yes, anchor_now=args.anchor_now
    )


def add_anchor_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--anchor-now",
        action="store_true",
        help="move scenario times so the latest report arrives an hour before now",
    )


if __name__ == "__main__":
    raise SystemExit(main())

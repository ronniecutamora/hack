"""Feed the scenario through the real intake and assessment pipeline.

``python -m app.demo load [--reset] [--yes]`` ingests every scenario report with
``channel="demo"`` into a separate demo database (DATA_DIR must end in "demo"),
backdates each report to scenario time, then assesses them all. A manifest in the
demo data dir maps scenario keys to report ids, so re-running skips loaded reports
instead of duplicating them.
"""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.assessment import AssessmentService, make_assessor
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_recorder
from app.demo import SCENARIO_PATH, DemoDirError, require_demo_dir
from app.demo.geo import pick_point
from app.demo.images import jobs_for
from app.demo.scenario import ScenarioReport, load_scenario, received_at, validate
from app.intake import IntakeService
from app.models import Report

MANIFEST_NAME = "manifest.json"
CHANNEL = "demo"


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
) -> list[int]:
    """Ingest one scenario report (photo + pin when present) and backdate it.

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
    when = received_at(report)
    with Session(engine) as session:
        for report_id in ids:
            row = session.get(Report, report_id)
            if row is not None:
                row.received_at = when
                session.add(row)
        session.commit()
    return ids


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
) -> dict:
    """Ingest new reports, assess them, and return a summary dict."""
    manifest = _read_manifest(data_dir)
    images = {job.report.key: job.path for job in jobs_for(reports, data_dir / "images")}
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
        new_ids: list[int] = []
        for report in reports:
            if report.key in manifest:
                existing += 1
                continue
            photo = None
            if report.key in images:
                photo = images[report.key].read_bytes()
            ids = await _ingest_one(intake, engine, report, photo)
            manifest[report.key] = ids[0]
            new_ids.extend(ids)
            _write_manifest(data_dir, manifest)
            loaded += 1
        # Assess exactly the reports created by this run.
        assessed, failed = 0, 0
        for report_id in new_ids:
            out = await assessor.assess(report_id)
            if out is None:
                continue
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
) -> int:
    """Run the load (see module docstring); returns a process exit code."""
    try:
        data_dir = require_demo_dir(settings, "load")
    except DemoDirError as exc:
        print(exc)
        return 2
    if reset and data_dir.exists():
        if not assume_yes:
            answer = input(f"Delete '{data_dir}' and reload the demo from scratch? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled; demo data left alone.")
                return 0
        shutil.rmtree(data_dir)
        print(f"Deleted {data_dir}.")
    engine = make_engine(settings)
    init_db(engine)
    reports = load_scenario(scenario_path)
    problems = validate(reports) if scenario_path == SCENARIO_PATH else []
    if problems:
        print("Scenario is invalid:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    missing = [
        job.report.key for job in jobs_for(reports, data_dir / "images") if not job.path.is_file()
    ]
    if missing:
        print(f"Missing {len(missing)} image(s) for: {', '.join(missing)}.")
        print("Generate them first: DATA_DIR=var/demo uv run python -m app.demo images")
        return 1
    summary = asyncio.run(load_reports(reports, settings, engine, data_dir, assessor=assessor))
    print(
        f"Reports loaded: {summary['loaded']} "
        f"({summary['existing']} already loaded), "
        f"assessed: {summary['assessed']}, failed: {summary['failed']}."
    )
    found = ", ".join(summary["barangays"]) or "-"
    print(f"Barangays found: {found}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the demo scenario into a demo database.")
    parser.add_argument("--reset", action="store_true", help="delete the demo data dir first")
    parser.add_argument("--yes", action="store_true", help="delete without asking")
    args = parser.parse_args(argv)
    return load_command(get_settings(), reset=args.reset, assume_yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())

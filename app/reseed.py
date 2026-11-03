"""Replace a deployed app's synthetic demo with a fresh `--anchor-now` load; real data stays.

The deployed demo was seeded once (app.seed), so its times age and its filing countdowns turn
"overdue". `reseed` swaps only the synthetic demo data (`Report.channel == "demo"`) for a fresh
load, inside the live container (app.admin), so the one-writer lease still holds:

1. **Plan** (read-only, also the dry run): the demo reports and reporters, the incidents only
   demo reports touch, the drafts that cite only demo reports, and the officer actions, draft
   events, grouping decisions, photos and model calls of those rows. It refuses, listing ids
   only, when an incident has both demo and real reports, a reporter has sent both, or a demo
   report is being assessed. An incident a real row also points at (a real report's grouping
   decision or an officer action) is kept and recomputed, so a real audit row never points at
   nothing. A draft that cites real reports too is kept as decided; it shows as out of date.
2. **Fresh load** in a scratch directory, exactly as the first seed (`python -m app.demo load
   --anchor-now`, through the real pipeline and the uploaded model cache), so the new demo never
   sees, groups with or flags against real reports. The scratch tables count their ids on from
   the live database's highest id plus ID_GAP (AUTOINCREMENT), so every fresh row keeps its
   own ids, and every id in its text and JSON ("photo reused from report #12") stays right, with
   no renumbering, and no id of a replaced row is ever handed out again.
3. **Swap** in one write transaction: the plan is computed again (the load took minutes), the
   planned rows are deleted (the audit triggers are lifted only inside this transaction, as in
   app.db.init_db), the fresh rows are copied in, and a digest of every real report and its
   rows must be unchanged before it commits. Any failure rolls back: nothing changed.

The caller (app.admin) pins a snapshot before step 2 and takes a regular one after step 3; the
replaced demo's photo files stay on the volume, so restoring the pinned snapshot shows them.
Live dashboards update on their own: the swap is a commit their watcher sees (app.live).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine
from sqlmodel import Session, SQLModel, select

from app.config import Settings
from app.db import AUDIT_TRIGGERS, SQLITE_BUSY_TIMEOUT_S, make_engine, make_recorder
from app.demo.loader import MANIFEST_NAME
from app.drafts import KIND_RDANA, DraftService
from app.incidents import refresh_incidents
from app.meta.factory import make_agent_llm
from app.models import (
    CHANNEL_DEMO,
    DRAFT_APPROVED,
    DRAFT_REJECTED,
    STATUS_ASSESSED,
    STATUS_ASSESSING,
    Draft,
    Report,
    manila_time,
)
from app.nol_form import COMMODITIES

logger = logging.getLogger(__name__)

# Fresh ids start this far above the live database's highest id, so the real reports and model
# calls that arrive during the minutes of the fresh load never take an id the load also used.
ID_GAP = 1000
DEMO_DRAFTS = Path(__file__).resolve().parent.parent / "data" / "demo" / "drafts.json"

# What the swap deletes from each table, as a condition on the staged id sets (temp tables
# named after the DemoPlan fields). Every table of the schema must be listed (a test checks):
# a new table is a decision about demo data, never silently left behind.
DELETES: dict[str, str] = {
    "modelcall": "report_id IN {reports} OR draft_id IN {drafts}",
    "draftevent": "draft_id IN {drafts}",
    "draft": "id IN {drafts}",
    "officeraction": "id IN {officer_actions}",
    "agentdecision": "report_id IN {reports}",
    "photoassessment": "report_id IN {reports}",
    "assessment": "report_id IN {reports}",
    "media": "report_id IN {reports}",
    "report": "id IN {reports}",
    "reporter": "id IN {reporters}",
    "incident": "id IN {incidents}",
}
STAGED = ("reports", "reporters", "incidents", "drafts", "officer_actions")


class ReseedRefused(Exception):
    """The reseed would touch real data or cannot run now; nothing was changed."""


@dataclass
class DemoPlan:
    """What a reseed replaces, by id. `conflicts` non-empty means it must refuse."""

    reports: set[int] = field(default_factory=set)
    reporters: set[int] = field(default_factory=set)
    incidents: set[int] = field(default_factory=set)  # deleted: only demo rows point at them
    kept_incidents: set[int] = field(default_factory=set)  # demo left them; real rows remain
    drafts: set[int] = field(default_factory=set)
    decided_drafts: int = 0
    kept_drafts: set[int] = field(default_factory=set)  # cite demo and real reports
    officer_actions: set[int] = field(default_factory=set)
    real_mentions: int = 0  # real grouping decisions that compared with a demo report
    conflicts: list[str] = field(default_factory=list)


def cited_report_ids(content: Any) -> set[int]:
    """Every report id a draft's content cites (any `report_ids` / `*_report_ids` list)."""
    found: set[int] = set()
    stack = [content]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("report_ids") and isinstance(item, list):
                    found.update(i for i in item if isinstance(i, int))
                else:
                    stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)
    return found


def _listed(ids: Iterable[int], limit: int = 20) -> str:
    ordered = sorted(ids)
    more = f" and {len(ordered) - limit} more" if len(ordered) > limit else ""
    return ", ".join(f"#{i}" for i in ordered[:limit]) + more


def plan(connection: sqlite3.Connection) -> DemoPlan:
    """What a reseed would replace in this database (read-only)."""
    result = DemoPlan()
    rows = connection.execute(
        "SELECT id, channel = ?, reporter_id, incident_id, officer_incident_id, status FROM report",
        (CHANNEL_DEMO,),
    ).fetchall()
    demo = {r[0] for r in rows if r[1]}
    result.reports = demo
    demo_members = {i for r in rows if r[1] for i in (r[3], r[4]) if i is not None}
    real_members = {i for r in rows if not r[1] for i in (r[3], r[4]) if i is not None}
    demo_reporters = {r[2] for r in rows if r[1]}
    real_reporters = {r[2] for r in rows if not r[1]}
    result.reporters = demo_reporters - real_reporters

    if mixed := demo_members & real_members:
        result.conflicts.append(
            f"incident(s) {_listed(mixed)} have both demo and real reports: move the real "
            "reports out (Keep separate) or leave the demo as it is"
        )
    if shared := demo_reporters & real_reporters:
        result.conflicts.append(f"reporter(s) {_listed(shared)} sent both demo and real reports")
    if busy := {r[0] for r in rows if r[1] and r[5] == STATUS_ASSESSING}:
        result.conflicts.append(
            f"demo report(s) {_listed(busy)} are being assessed: try again in a few minutes"
        )

    demo_touched = set(demo_members)
    real_refs = set(real_members)
    for report_id, incident_id, compared_incident_id, compared_report_id in connection.execute(
        "SELECT report_id, incident_id, compared_incident_id, compared_report_id FROM agentdecision"
    ):
        if report_id in demo:
            demo_touched.add(incident_id)
        else:
            real_refs |= {incident_id, compared_incident_id}
            result.real_mentions += compared_report_id in demo

    for action_id, report_id, reporter_id, incident_id, from_incident_id in connection.execute(
        "SELECT id, report_id, reporter_id, incident_id, from_incident_id FROM officeraction"
    ):
        if report_id in demo or (report_id is None and reporter_id in result.reporters):
            result.officer_actions.add(action_id)
            demo_touched |= {incident_id, from_incident_id}
        else:
            real_refs |= {incident_id, from_incident_id}

    for draft_id, status, incident_id, content in connection.execute(
        "SELECT id, status, incident_id, content FROM draft"
    ):
        cited = cited_report_ids(json.loads(content) if content else None)
        if cited and cited <= demo:
            result.drafts.add(draft_id)
            result.decided_drafts += status in (DRAFT_APPROVED, DRAFT_REJECTED)
        else:
            real_refs.add(incident_id)
            if cited & demo:
                result.kept_drafts.add(draft_id)

    demo_touched.discard(None)
    real_refs.discard(None)
    result.incidents = demo_touched - real_refs
    result.kept_incidents = (demo_touched & real_refs) - real_members
    return result


def _tables() -> list[str]:
    """The schema's tables, parents before children."""
    return [table.name for table in SQLModel.metadata.sorted_tables]


def _max_ids(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        name: connection.execute(f'SELECT coalesce(max(id), 0) FROM "{name}"').fetchone()[0]
        for name in _tables()
    }


def prepare_scratch(scratch_db: Path, live: sqlite3.Connection, gap: int = ID_GAP) -> None:
    """Create the scratch database with the app's schema, but AUTOINCREMENT ids counting on
    from the live database's highest ids plus `gap`. The demo load then finds its schema in
    place (init_db adds only what is missing) and gives every row an id the live database
    never had."""
    metadata = MetaData()
    for table in SQLModel.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        copy.dialect_kwargs["sqlite_autoincrement"] = True
    engine = create_engine(f"sqlite:///{scratch_db}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    start = _max_ids(live)
    scratch = sqlite3.connect(scratch_db)
    try:
        scratch.executemany(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            [(name, highest + gap) for name, highest in start.items()],
        )
        scratch.commit()
    finally:
        scratch.close()


def _columns(connection: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _digest_real(connection: sqlite3.Connection) -> str:
    """A digest of every real report and the rows that belong to it."""
    real = f"(SELECT id FROM report WHERE channel != '{CHANNEL_DEMO}')"
    queries = [
        f"SELECT * FROM report WHERE id IN {real} ORDER BY id",
        f"SELECT * FROM reporter WHERE id IN (SELECT reporter_id FROM report WHERE id IN {real})"
        " ORDER BY id",
    ]
    for table in (
        "assessment",
        "photoassessment",
        "media",
        "agentdecision",
        "modelcall",
        "officeraction",
    ):
        queries.append(f'SELECT * FROM "{table}" WHERE report_id IN {real} ORDER BY id')
    digest = hashlib.sha256()
    for query in queries:
        for row in connection.execute(query):
            digest.update(repr(row).encode())
    return digest.hexdigest()


def summary(connection: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the before/after lines (no report text, places or pseudonyms)."""

    def one(sql: str, *params: Any) -> Any:
        return connection.execute(sql, params).fetchone()[0]

    latest = one("SELECT max(received_at) FROM report WHERE channel = ?", CHANNEL_DEMO)
    return {
        "demo_reports": one("SELECT count(*) FROM report WHERE channel = ?", CHANNEL_DEMO),
        "real_reports": one("SELECT count(*) FROM report WHERE channel != ?", CHANNEL_DEMO),
        "incidents": one("SELECT count(*) FROM incident WHERE active"),
        "drafts": one("SELECT count(*) FROM draft"),
        "officer_actions": one("SELECT count(*) FROM officeraction"),
        "model_calls": one("SELECT count(*) FROM modelcall"),
        "latest_demo": latest,
    }


def summary_line(label: str, counts: dict[str, Any]) -> str:
    latest = counts["latest_demo"]
    when = manila_time(datetime.fromisoformat(latest)) + " Manila" if latest else "-"
    return (
        f"{label}: {counts['demo_reports']} demo report(s), latest {when}; "
        f"{counts['real_reports']} real report(s); {counts['incidents']} active incident(s), "
        f"{counts['drafts']} draft(s), {counts['officer_actions']} officer action(s), "
        f"{counts['model_calls']} model call(s)"
    )


def plan_lines(found: DemoPlan) -> list[str]:
    lines = [
        f"replaces {len(found.reports)} demo report(s) from {len(found.reporters)} demo "
        f"reporter(s), {len(found.incidents)} demo incident(s), {len(found.drafts)} draft(s) "
        f"({found.decided_drafts} decided) and {len(found.officer_actions)} officer action(s) "
        "with their photos, grouping decisions, draft events and model calls"
    ]
    if found.kept_incidents:
        lines.append(
            f"keeps incident(s) {_listed(found.kept_incidents)}: real grouping decisions or "
            "officer actions point at them; recomputed without the demo reports"
        )
    if found.kept_drafts:
        lines.append(
            f"keeps draft(s) {_listed(found.kept_drafts)}: they cite real reports too; they will "
            "show as out of date"
        )
    if found.real_mentions:
        lines.append(
            f"{found.real_mentions} real grouping decision(s) compared with a demo report; "
            "their explanation still names the old demo report"
        )
    return lines


def connect(db_path: Path) -> sqlite3.Connection:
    """Autocommit: transactions are explicit (BEGIN IMMEDIATE ... COMMIT)."""
    return sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_S, isolation_level=None)


def _stage(connection: sqlite3.Connection, found: DemoPlan) -> dict[str, str]:
    names = {}
    for name in STAGED:
        table = f"reseed_{name}"
        connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
        connection.execute(f"CREATE TEMP TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.executemany(
            f"INSERT INTO temp.{table} (id) VALUES (?)", [(i,) for i in getattr(found, name)]
        )
        names[name] = f"(SELECT id FROM temp.{table})"
    return names


def swap(db_path: Path, scratch_db: Path) -> DemoPlan:
    """Delete the planned demo rows and copy the fresh ones in, in one write transaction.
    Raises ReseedRefused (rolled back, nothing changed) on a conflict or an id clash."""
    connection = connect(db_path)
    try:
        connection.execute("ATTACH DATABASE ? AS fresh", (str(scratch_db),))
        connection.execute("BEGIN IMMEDIATE")
        try:
            found = plan(connection)
            if found.conflicts:
                raise ReseedRefused("; ".join(found.conflicts))
            before = _digest_real(connection)
            staged = _stage(connection, found)
            for name in AUDIT_TRIGGERS:
                connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            for table, condition in DELETES.items():
                connection.execute(f'DELETE FROM main."{table}" WHERE {condition.format(**staged)}')
            for table in _tables():
                clash = connection.execute(
                    f'SELECT count(*) FROM fresh."{table}"'
                    f' WHERE id IN (SELECT id FROM main."{table}")'
                ).fetchone()[0]
                if clash:
                    raise ReseedRefused(f"{clash} fresh {table} id(s) are taken: run it again")
                live = set(_columns(connection, "main", table))
                fresh = [c for c in _columns(connection, "fresh", table) if c in live]
                columns = ", ".join(f'"{c}"' for c in fresh)
                connection.execute(
                    f'INSERT INTO main."{table}" ({columns}) SELECT {columns} FROM fresh."{table}"'
                )
            for name, statement in AUDIT_TRIGGERS.items():
                connection.execute(f"CREATE TRIGGER {name} {statement}")
            if _digest_real(connection) != before:
                raise RuntimeError("real reports changed during the swap; rolled back")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            for name in STAGED:
                connection.execute(f"DROP TABLE IF EXISTS temp.reseed_{name}")
        return found
    finally:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("DETACH DATABASE fresh")
        connection.close()


def _copy_media(scratch_media: Path, media_dir: Path) -> list[Path]:
    """Copy the fresh demo's photo folders (media/<report id>/, new ids) into place."""
    copied = []
    if not scratch_media.is_dir():
        return copied
    media_dir.mkdir(parents=True, exist_ok=True)
    for folder in sorted(p for p in scratch_media.iterdir() if p.is_dir()):
        target = media_dir / folder.name
        if target.exists():
            raise ReseedRefused(f"media folder {folder.name} already exists: run it again")
        shutil.copytree(folder, target)
        copied.append(target)
    return copied


def draft_plan() -> dict[str, Any]:
    """The scenario's selected officer outputs, independent of database ids."""
    return json.loads(DEMO_DRAFTS.read_text(encoding="utf-8"))


def draft_plan_line(plan: dict[str, Any]) -> str:
    claims = ", ".join(f"{c['commodity']} ({c['report_key']})" for c in plan["claims"])
    return (
        f"prepares 1 RDANA draft for {plan['barangay']} and "
        f"{len(plan['claims'])} claim draft(s): {claims}"
    )


def prepare_drafts(settings: Settings, work: Path, plan: dict[str, Any]) -> None:
    """Generate the configured drafts in scratch, using the officer's draft service.

    Summary requests are cache-only: fresh ids usually change the prompt, so a miss uses the
    service's deterministic fallback and a daily job cannot create unbounded model spend.
    """
    manifest = json.loads((work / MANIFEST_NAME).read_text(encoding="utf-8"))
    scratch_settings = settings.model_copy(
        update={"data_dir": work, "database_path": work / "app.db"}
    )
    engine = make_engine(scratch_settings)
    llm = make_agent_llm(scratch_settings, make_recorder(engine))
    llm.cache_only = True
    service = DraftService(scratch_settings, engine, llm)

    async def generate() -> None:
        try:
            with Session(engine) as session:
                rdana = session.exec(
                    select(Draft)
                    .where(Draft.kind == KIND_RDANA, Draft.barangay == plan["barangay"])
                    .order_by(Draft.version.desc())
                ).first()
            if rdana is None or service.is_stale(rdana):
                await service.generate_rdana(plan["barangay"])
            for claim in plan["claims"]:
                commodity = COMMODITIES[claim["commodity"]]
                with Session(engine) as session:
                    report = session.get(Report, manifest[claim["report_key"]])
                    if (
                        report is None
                        or report.channel != CHANNEL_DEMO
                        or report.incident_id is None
                    ):
                        raise ReseedRefused("the configured demo claim report has no incident")
                    incident_id = report.incident_id
                    existing = session.exec(
                        select(Draft)
                        .where(Draft.kind == commodity.kind, Draft.incident_id == incident_id)
                        .order_by(Draft.version.desc())
                    ).first()
                if existing is None or service.is_stale(existing):
                    service.generate_nol(commodity.key, incident_id)
        finally:
            await llm.aclose()

    try:
        asyncio.run(generate())
    finally:
        engine.dispose()


def fresh_load_lines(db_path: Path) -> list[str]:
    """Refuse an incomplete load and summarize logged model calls without report data."""
    with contextlib.closing(connect(db_path)) as connection:
        incomplete = connection.execute(
            "SELECT count(*) FROM report WHERE channel = ? AND "
            "(status != ? OR (grouping_error IS NOT NULL AND grouping_error != ''))",
            (CHANNEL_DEMO, STATUS_ASSESSED),
        ).fetchone()[0]
        if incomplete:
            raise ReseedRefused(f"{incomplete} fresh demo report(s) failed assessment or grouping")
        count, cost = connection.execute(
            "SELECT count(*), coalesce(sum(cost_estimate), 0) FROM modelcall"
        ).fetchone()
        pairs = connection.execute(
            "SELECT model, count(*), sum(cached) FROM modelcall WHERE step = ? GROUP BY model",
            ("pair_judge",),
        ).fetchall()
    lines = [f"fresh model calls: {count}, estimated cost ${cost:.4f}"]
    lines.extend(
        f"pair judge {model}: {n} call(s), {cached or 0} cached" for model, n, cached in pairs
    )
    return lines


def reseed(
    settings: Settings,
    load_demo: Callable[[Path], bool],
    *,
    scratch: Path,
    before_change: Callable[[], str],
    dry_run: bool = False,
) -> list[str]:
    """Replace the synthetic demo (module docstring). `load_demo(work)` fills work/app.db with
    a fresh demo load (app.seed.run_demo_load); `before_change()` pins a snapshot and returns
    its name; `scratch` is an empty directory for the fresh load. Returns the output lines;
    raises ReseedRefused when it would touch real data (nothing changed)."""
    db_path = settings.db_path
    if not db_path.exists():
        raise ReseedRefused(f"no database at {db_path}")
    connection = connect(db_path)
    try:
        found = plan(connection)
        before = summary(connection)
    finally:
        connection.close()
    outputs = draft_plan()
    lines = [summary_line("before", before), *plan_lines(found), draft_plan_line(outputs)]
    if found.conflicts:
        raise ReseedRefused("; ".join(found.conflicts))
    if dry_run:
        return [*lines, "dry run: nothing changed, no snapshot taken"]

    lines.append(f"snapshot before the change: {before_change()}")
    work = scratch / "demo"  # the demo commands only accept a dir named "demo"
    work.mkdir()
    connection = connect(db_path)
    try:
        prepare_scratch(work / "app.db", connection)
    finally:
        connection.close()
    if not load_demo(work):
        raise ReseedRefused("the fresh demo load failed (see the logs); nothing changed")
    prepare_drafts(settings, work, outputs)
    lines.extend(fresh_load_lines(work / "app.db"))
    copied = _copy_media(work / "media", settings.media_dir)
    try:
        done = swap(db_path, work / "app.db")
    except BaseException:
        for folder in copied:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    if done.kept_incidents:
        engine = make_engine(settings)
        try:
            with Session(engine) as session:
                refresh_incidents(session, set(done.kept_incidents))
                session.commit()
        finally:
            engine.dispose()
    connection = connect(db_path)
    try:
        after = summary(connection)
    finally:
        connection.close()
    if done.reports != found.reports:
        lines.append(f"replaced {len(done.reports)} demo report(s) (the plan changed meanwhile)")
    return [*lines, summary_line("after", after)]

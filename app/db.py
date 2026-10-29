"""SQLite engine and helpers. The database lives in DATA_DIR/app.db."""

from sqlalchemy import Engine, event, inspect
from sqlmodel import Session, SQLModel, create_engine

from app import models
from app.config import Settings
from app.meta.base import CallRecord, Recorder

# The bot and the web app are separate processes sharing one file: WAL lets readers run
# during a write, and the busy timeout makes a writer wait instead of failing at once.
SQLITE_BUSY_TIMEOUT_S = 15


def make_engine(settings: Settings) -> Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # check_same_thread is off because FastAPI serves sync routes from a thread pool.
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_S},
    )
    event.listen(engine, "connect", _enable_wal)
    return engine


def _enable_wal(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db(engine: Engine) -> None:
    """Create or upgrade the schema in one write transaction, safe when processes start together.

    pysqlite runs DDL in autocommit, so `engine.begin()` alone would not make this atomic: the
    bot and the web app starting together could both drop the triggers and the second CREATE
    would fail. An explicit BEGIN IMMEDIATE takes SQLite's write lock first; the other process
    waits (busy timeout), then finds the finished schema.
    """
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        SQLModel.metadata.create_all(connection)
        # create_all does not alter existing tables. Nullable/defaulted additions preserve
        # older DBs. Every audit trigger is dropped first and recreated last, inside this
        # transaction, so a changed, renamed or removed definition reaches existing databases.
        # A backfill that must UPDATE decided drafts goes between the two, where no trigger can
        # abort it; a new column with a DEFAULT needs no UPDATE at all and is the preferred way.
        for (name,) in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name IN ('draft', 'draftevent', 'officeraction')"
        ).all():
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
        additions = {
            "report": {
                "incident_id": "INTEGER REFERENCES incident(id)",
                "grouping_fingerprint": "VARCHAR",
                "grouping_error": "VARCHAR",
                "grouping_needs_officer": "BOOLEAN NOT NULL DEFAULT 0",
                "hold_reason": "VARCHAR",
                "assessing_started_at": "DATETIME",
                "album": "VARCHAR",
                "officer_incident_id": "INTEGER REFERENCES incident(id)",
                "officer_barangay": "VARCHAR",
            },
            "reporter": {"blocked": "BOOLEAN NOT NULL DEFAULT 0"},
            "media": {"phash": "VARCHAR"},
            "photoassessment": {"taken_at": "DATETIME"},
            "assessment": {"signals": "JSON"},
            "draft": {"incident_id": "INTEGER REFERENCES incident(id)"},
        }
        for table, additions_by_column in additions.items():
            columns = {c["name"] for c in inspect(connection).get_columns(table)}
            for name, sql_type in additions_by_column.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_report_incident_id ON report (incident_id)"
        )
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_media_sha256 ON media (sha256)")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_draft_incident_id ON draft (incident_id)"
        )
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_modelcall_at ON modelcall (at)")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_report_reporter_received "
            "ON report (reporter_id, received_at)"
        )
        for name, statement in AUDIT_TRIGGERS.items():
            connection.exec_driver_sql(f"CREATE TRIGGER {name} {statement}")
        connection.commit()


# The audit is append-only and a decided draft is frozen, even against a stray UPDATE: the
# only change a draft ever takes is its single decision (draft -> approved/rejected/superseded).
DRAFT_GENERATED_COLUMNS = (
    "kind",
    "barangay",
    "incident_id",
    "version",
    "event_name",
    "event_type",
    "event_at",
    "content",
    "evidence_fingerprint",
    "summary_model",
    "summary_fallback",
    "summary_validation",
    "created_at",
)
AUDIT_TRIGGERS = {
    "officeraction_no_update": "BEFORE UPDATE ON officeraction "
    "BEGIN SELECT RAISE(ABORT, 'officer audit rows are immutable'); END",
    "officeraction_no_delete": "BEFORE DELETE ON officeraction "
    "BEGIN SELECT RAISE(ABORT, 'officer audit rows are immutable'); END",
    "draftevent_no_update": "BEFORE UPDATE ON draftevent "
    "BEGIN SELECT RAISE(ABORT, 'draft audit rows are immutable'); END",
    "draftevent_no_delete": "BEFORE DELETE ON draftevent "
    "BEGIN SELECT RAISE(ABORT, 'draft audit rows are immutable'); END",
    "draft_no_delete": "BEFORE DELETE ON draft "
    "BEGIN SELECT RAISE(ABORT, 'drafts are never deleted'); END",
    "draft_frozen": "BEFORE UPDATE ON draft "
    "WHEN OLD.status != 'draft' OR "
    + " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in DRAFT_GENERATED_COLUMNS)
    + " BEGIN SELECT RAISE(ABORT, 'a decided draft or generated content never changes'); END",
}


def make_recorder(engine: Engine) -> Recorder:
    """Return a model-client recorder that writes each call to the ModelCall table."""

    def record(call: CallRecord) -> None:
        with Session(engine) as session:
            session.add(
                models.ModelCall(
                    model=call.model,
                    latency_ms=call.latency_ms,
                    cached=call.cached,
                    cost_estimate=call.cost_estimate,
                )
            )
            session.commit()

    return record

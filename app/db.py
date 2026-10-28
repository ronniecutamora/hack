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
    SQLModel.metadata.create_all(engine)
    # create_all does not alter existing tables. These nullable additions preserve slice-2 DBs.
    with engine.begin() as connection:
        columns = {c["name"] for c in inspect(connection).get_columns("report")}
        additions = {
            "incident_id": "INTEGER REFERENCES incident(id)",
            "grouping_fingerprint": "VARCHAR",
            "grouping_error": "VARCHAR",
            "grouping_needs_officer": "BOOLEAN NOT NULL DEFAULT 0",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE report ADD COLUMN {name} {sql_type}")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_report_incident_id ON report (incident_id)"
        )


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

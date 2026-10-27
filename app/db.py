"""SQLite engine and helpers. The database lives in DATA_DIR/app.db."""

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from app import models
from app.config import Settings
from app.meta.base import CallRecord, Recorder


def make_engine(settings: Settings) -> Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # check_same_thread is off because FastAPI serves sync routes from a thread pool.
    return create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


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

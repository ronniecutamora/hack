"""Database tables. Times are naive UTC. No names, phone numbers or raw Telegram ids are stored."""

from datetime import UTC, datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel

# Report.status values
STATUS_NEW = "new"
STATUS_TRANSCRIPTION_FAILED = "transcription_failed"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Reporter(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    pseudonym: str = Field(unique=True, index=True)  # HMAC-SHA256(salt, telegram user id)
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Report(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    reporter_id: int = Field(foreign_key="reporter.id", index=True)
    channel: str  # e.g. "telegram"
    received_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    text: str | None = None  # typed message or photo caption
    transcript: str | None = None  # from Voice Transcribe
    lat: float | None = None
    lon: float | None = None
    barangay: str | None = None
    status: str = STATUS_NEW


class Media(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", index=True)
    kind: str  # "photo" | "voice"
    path: str  # relative to DATA_DIR
    sha256: str
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    # Set when the raw file is deleted; derived text and assessments are kept.
    purged_at: NaiveDatetime | None = None


class ModelCall(SQLModel, table=True):
    """One row per model request (cache hits included) for observability and cost tracking."""

    id: int | None = Field(default=None, primary_key=True)
    at: NaiveDatetime = Field(default_factory=utcnow)
    model: str
    latency_ms: int
    cached: bool
    cost_estimate: float = 0.0  # USD, 0 for cache hits or when the price is unknown

"""Database tables. Times are naive UTC. No names, phone numbers or raw Telegram ids are stored."""

from datetime import UTC, datetime
from typing import Any

from pydantic import NaiveDatetime
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

# Report.status values
STATUS_NEW = "new"
STATUS_TRANSCRIPTION_FAILED = "transcription_failed"
STATUS_ASSESSING = "assessing"
STATUS_ASSESSED = "assessed"
STATUS_ASSESSMENT_FAILED = "assessment_failed"  # partial results are still saved


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Reporter(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    pseudonym: str = Field(unique=True, index=True)  # HMAC-SHA256(salt, telegram user id)
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Report(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    reporter_id: int = Field(foreign_key="reporter.id", index=True)
    incident_id: int | None = Field(default=None, foreign_key="incident.id", index=True)
    grouping_fingerprint: str | None = None
    grouping_error: str | None = None
    grouping_needs_officer: bool = False
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
    kind: str  # "photo" | "voice" | "overlay" (SAM masks drawn on a photo; purged like raw media)
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


class Assessment(SQLModel, table=True):
    """The agent's reading of a report: cleaned transcript, extracted facts and barangay."""

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", unique=True, index=True)
    llm_model: str  # which agent LLM produced the facts (Spark 1.3 or Spark Open)
    transcript_clean: str | None = None  # Report.transcript stays raw, as evidence
    facts: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # How the barangay was found: pin | exif | text | transcript | llm | ambiguous | none
    geo_source: str = "none"
    geo_detail: str | None = None  # matched words, or the candidates when ambiguous
    error: str | None = None
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class PhotoAssessment(SQLModel, table=True):
    """One photo's damage judgment (agent LLM) and measurements (SAM 3.1)."""

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", index=True)
    media_id: int = Field(foreign_key="media.id", unique=True)
    overlay_media_id: int | None = Field(default=None, foreign_key="media.id")
    llm_model: str
    judgment: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    measurement: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # GPS from the photo's EXIF (only when it was sent as a file); used if there is no pin.
    gps_lat: float | None = None
    gps_lon: float | None = None
    error: str | None = None
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class Incident(SQLModel, table=True):
    """Current grouping and deterministic rollup; empty retired groups remain for the audit."""

    id: int | None = Field(default=None, primary_key=True)
    barangay: str | None = Field(default=None, index=True)
    title: str
    first_seen: NaiveDatetime
    last_seen: NaiveDatetime = Field(index=True)
    status: str = "open"  # open | needs_officer
    active: bool = True
    latest_facts: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class AgentDecision(SQLModel, table=True):
    """Immutable grouping explanation, including deterministic actions without a model call."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # new_incident | merged | kept_separate | needs_officer | re-linked
    report_id: int = Field(foreign_key="report.id", index=True)
    incident_id: int = Field(foreign_key="incident.id", index=True)
    compared_incident_id: int | None = Field(default=None, foreign_key="incident.id")
    compared_report_id: int | None = Field(default=None, foreign_key="report.id")
    judge_model: str | None = None  # None means a deterministic rule, never a guessed model
    decision: str
    confidence: float | None = None
    reason: str
    created_at: NaiveDatetime = Field(default_factory=utcnow)

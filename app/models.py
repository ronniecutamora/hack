"""Database tables. Times are naive UTC. No names, phone numbers or raw Telegram ids are stored."""

from datetime import UTC, datetime, timedelta, timezone
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
STATUS_HELD = "held"

# Report.channel of the synthetic demo scenario (app.demo): generated reports, photos and
# reporters, no real person's data. Retention keeps its media; real channels are purged.
CHANNEL_DEMO = "demo"


MANILA = timezone(timedelta(hours=8))  # Philippine time, no daylight saving


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def manila_time(value: datetime) -> str:
    """Format a naive-UTC datetime as Philippine local time."""
    return value.replace(tzinfo=UTC).astimezone(MANILA).strftime("%Y-%m-%d %H:%M")


class Reporter(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    pseudonym: str = Field(unique=True, index=True)  # HMAC-SHA256(salt, telegram user id)
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    blocked: bool = False


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
    assessing_started_at: NaiveDatetime | None = None
    hold_reason: str | None = None
    # Telegram's media_group_id when the photos came as one album: the album is one report.
    album: str | None = None
    # Officer overrides the agent obeys (each written with its OfficerAction audit row): the
    # incident a merge or keep-separate pinned this report to, which grouping never changes, and
    # the barangay an officer set, which geolocation never overwrites.
    officer_incident_id: int | None = Field(default=None, foreign_key="incident.id")
    officer_barangay: str | None = None
    # The same reporter's earlier report this one corrects: they replied to the bot's summary of
    # it ("Mali? I-reply ang tamang detalye."). Grouping routes it there by rule.
    corrects_report_id: int | None = Field(default=None, foreign_key="report.id")


class Media(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", index=True)
    kind: str  # "photo" | "voice" | "overlay" (SAM masks drawn on a photo; purged like raw media)
    path: str  # relative to DATA_DIR
    sha256: str = Field(index=True)
    phash: str | None = None  # retained 64-bit dHash; NULL = untried, "" = unavailable
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    # Set when the raw file is deleted; derived text and assessments are kept.
    purged_at: NaiveDatetime | None = None


class ModelCall(SQLModel, table=True):
    """One row per model request (cache hits included) for observability and cost tracking."""

    id: int | None = Field(default=None, primary_key=True)
    at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    model: str
    latency_ms: int
    cached: bool
    cost_estimate: float = 0.0  # USD, 0 for cache hits or when the price is unknown
    # The agent trace (app.trace): what the call served. NULL for calls outside a pipeline step
    # (evals, the CLI's ad-hoc calls) and for every call logged before the trace existed.
    report_id: int | None = Field(default=None, foreign_key="report.id", index=True)
    draft_id: int | None = Field(default=None, foreign_key="draft.id", index=True)
    step: str | None = None  # an app.trace.Step value
    subject: str | None = None  # "media:<id>", "incident:<id>" or "part:<n>"


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
    signals: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))


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
    taken_at: NaiveDatetime | None = None  # EXIF Manila local capture time, converted to UTC
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


# OfficerAction.kind values
OFFICER_MERGED = "officer_merged"
OFFICER_KEPT_SEPARATE = "officer_kept_separate"
OFFICER_SET_PLACE = "officer_set_place"
OFFICER_SIGNAL_REVIEWED = "officer_signal_reviewed"
OFFICER_FALLEN_TREE = "officer_fallen_tree"
OFFICER_BLOCKED = "officer_blocked"
OFFICER_UNBLOCKED = "officer_unblocked"


class OfficerAction(SQLModel, table=True):
    """Append-only audit of an officer's override (database triggers reject updates and deletes).

    Separate from AgentDecision, which explains the agent's own grouping per report and
    incident: an officer also reviews trust signals and blocks reporters, which have no incident,
    and every row names the officer. The incident timeline shows both, labelled.
    """

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)
    report_id: int | None = Field(default=None, foreign_key="report.id", index=True)
    # Where the report is after the action, and where it was before (a merge moves it).
    incident_id: int | None = Field(default=None, foreign_key="incident.id", index=True)
    from_incident_id: int | None = Field(default=None, foreign_key="incident.id", index=True)
    reporter_id: int | None = Field(default=None, foreign_key="reporter.id")
    officer: str  # initials or designation, free text until there is a login; never a reporter
    note: str | None = None
    # Action specifics: the barangay set and the one before, a signal's key, code and verdict.
    detail: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)


# Draft.status values. Only a draft can be decided; an approved draft never changes.
DRAFT_OPEN = "draft"
DRAFT_APPROVED = "approved"
DRAFT_REJECTED = "rejected"
DRAFT_SUPERSEDED = "superseded"  # a newer version of the same output was generated undecided


class Draft(SQLModel, table=True):
    """An officer output drafted from report evidence: one RDANA form, the damaged-houses table
    or one incident's PCIC Notice of Loss.

    `content` is the complete filled output (values, evidence and sources) as generated; pages
    render it, never a recomputation, so an approved draft reads exactly as it was approved.
    """

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # rdana | damaged_houses | pcic_nl_<commodity>
    barangay: str | None = Field(default=None, index=True)  # None for the city-wide table
    # The incident a per-incident output (a Notice of Loss) is about; None for the others.
    incident_id: int | None = Field(default=None, foreign_key="incident.id", index=True)
    version: int
    status: str = DRAFT_OPEN
    event_name: str | None = None  # officer input (RDANA 1.1.1)
    event_type: str | None = None  # officer override of 1.1.2
    event_at: NaiveDatetime | None = None  # officer override of 1.1.3, naive UTC
    content: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    evidence_fingerprint: str | None = None  # changes when the counted evidence changes
    summary_model: str | None = None  # model that wrote the accepted summary; None = rule
    summary_fallback: bool = False
    summary_validation: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    decided_at: NaiveDatetime | None = None
    decision_reason: str | None = None
    decided_by: str | None = None  # an officer's designation (free text), never a reporter


class DraftEvent(SQLModel, table=True):
    """Append-only draft audit (generated, superseded, approved, rejected); database triggers
    reject updates and deletes. Separate from AgentDecision, which is per report and incident."""

    id: int | None = Field(default=None, primary_key=True)
    draft_id: int = Field(foreign_key="draft.id", index=True)
    action: str
    reason: str | None = None
    actor: str | None = None
    detail: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: NaiveDatetime = Field(default_factory=utcnow)

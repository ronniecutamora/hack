"""Agent trace plumbing: which report, pipeline step and subject each model call served.

The model clients know nothing about reports. The code that runs a pipeline step sets a
request-scoped context around it (``traced``), and the call-log recorder (app.db) stores that
context on every ModelCall row. A ``ContextVar`` is copied into each asyncio task, so the bot's
concurrent per-report assessments, and the photo judge and SAM calls running side by side inside
one of them, never see each other's context. A call made outside any context stays unlinked.

``STEPS`` is the one list of pipeline steps: the recorder, the trace view and the tests share it.
``MODEL_FAMILIES`` is the one table naming each recorded model id for an officer.
"""

from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from app.meta.fireworks import FireworksGlimmerClient, FireworksSparkOpenClient
from app.meta.image import MODEL as IMAGE_MODEL
from app.meta.sam import MODEL as SAM_MODEL
from app.meta.spark import MODEL as SPARK_MODEL
from app.meta.spark_open import MODEL as SPARK_OPEN_MODEL
from app.meta.transcribe import MODEL as TRANSCRIBE_MODEL


class Step(StrEnum):
    """Pipeline steps in the order the agent runs them for one report (drafts last)."""

    TRANSCRIBE = "transcribe"
    CLEANUP = "cleanup"
    FACTS = "facts"
    PHOTO_JUDGE = "photo_judge"
    SAM_ROOF = "sam_roof"
    SAM_FLOODWATER = "sam_floodwater"
    GEOLOCATE = "geolocate"
    TRUST = "trust"
    GROUPING_RULE = "grouping_rule"
    PAIR_JUDGE = "pair_judge"
    RDANA_SUMMARY = "rdana_summary"


Actor = Literal["model", "rule"]


@dataclass(frozen=True)
class StepInfo:
    label: str
    actor: Actor  # "rule": deterministic code, never a model call


STEPS: dict[Step, StepInfo] = {
    Step.TRANSCRIBE: StepInfo("Transcribe the voice note", "model"),
    Step.CLEANUP: StepInfo("Clean the transcript", "rule"),
    Step.FACTS: StepInfo("Read the facts", "model"),
    Step.PHOTO_JUDGE: StepInfo("Judge photo damage", "model"),
    Step.SAM_ROOF: StepInfo("Find roofs (SAM)", "model"),
    Step.SAM_FLOODWATER: StepInfo("Find floodwater (SAM)", "model"),
    Step.GEOLOCATE: StepInfo("Find the barangay", "rule"),
    Step.TRUST: StepInfo("Check trust signals", "rule"),
    Step.GROUPING_RULE: StepInfo("Group into an incident", "rule"),
    Step.PAIR_JUDGE: StepInfo("Judge: same incident?", "model"),
    Step.RDANA_SUMMARY: StepInfo("Write the RDANA summary", "model"),
}
STEP_ORDER = {step: index for index, step in enumerate(Step)}
# One SAM step per concept: SAM takes one concept per request (app.photo.SAM_CONCEPTS).
SAM_STEPS = {"roof": Step.SAM_ROOF, "floodwater": Step.SAM_FLOODWATER}


@dataclass(frozen=True)
class ModelFamily:
    name: str  # the Meta model family, as Meta names it
    host: str  # who served the request


# Keyed by the ids the clients log, taken from the clients themselves so the two never drift.
MODEL_FAMILIES: dict[str, ModelFamily] = {
    TRANSCRIBE_MODEL: ModelFamily("Muse Voice Transcribe", "Meta Model API"),
    SPARK_MODEL: ModelFamily("Muse Spark 1.3", "Meta Model API"),
    "muse-spark-1.2": ModelFamily("Muse Spark 1.2", "Meta Model API"),
    SAM_MODEL: ModelFamily("SAM 3.1", "Meta Model API"),
    IMAGE_MODEL: ModelFamily("Muse Image", "Meta Model API"),
    SPARK_OPEN_MODEL: ModelFamily("Muse Spark Open", "Baseten"),
    FireworksSparkOpenClient.model: ModelFamily("Muse Spark Open", "Fireworks"),
    FireworksGlimmerClient.model: ModelFamily("Muse Glimmer", "Fireworks"),
    # The eval results name backends, not ids (app.eval_results).
    "muse-spark-open": ModelFamily("Muse Spark Open", "Baseten"),
}


def model_family(model: str | None) -> ModelFamily | None:
    return MODEL_FAMILIES.get(model) if model else None


@dataclass(frozen=True)
class TraceContext:
    report_id: int | None = None
    step: Step | None = None
    # What within the report the call was about: "media:<id>" (a photo), "incident:<id>" (a
    # pair judgment) or "part:<n>" (a chunk of a long voice note).
    subject: str | None = None
    # Collects the ids of the calls recorded inside the context, so a row created only after
    # its calls (a draft) can be linked to them in the same transaction that creates it.
    call_ids: list[int] | None = None


_current: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def current() -> TraceContext | None:
    return _current.get()


@contextmanager
def traced(**fields) -> Iterator[TraceContext]:
    """Set trace fields for the calls made inside the block, on top of the enclosing ones."""
    context = replace(_current.get() or TraceContext(), **fields)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


async def in_step[T](step: Step, awaitable: Awaitable[T]) -> T:
    """Await inside a step: for calls run side by side with asyncio.gather, each in its task."""
    with traced(step=step):
        return await awaitable

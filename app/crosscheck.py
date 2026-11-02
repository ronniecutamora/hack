"""Spark and SAM 3.1 check each other on every photo; a disagreement worth asking about is a
trust signal, so Review asks the officer (app.questions).

Spark judges the photo (``floodwater`` yes or no, ``roof_damage``); SAM outlines and measures it
(``floodwater_present`` and ``roof_count``, at the thresholds in app.photo). Each row of CHECKS
pairs one judgment with the measurement that should back it, as data: the judgment field and
the values that mean Spark sees the thing, the measurement field that means SAM outlined it
(true, or a count above 0), and which way round the two must disagree.

Every row is measured on the labeled photo sets (scripts.eval_crosscheck, task "crosscheck" in
data/eval/results.json); only the rows whose disagreements are worth an officer's minute ask
(``asks``). The rest stay measured, so the choice can be checked and changed with the data.
The counts keep using Spark's judgment either way: the signal only flags the report until the
officer answers, like every other trust signal (app.trust).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.photo import FLOOD_LOW_THRESHOLD, FLOOD_MIN_AREA, FLOOD_THRESHOLD, ROOF_THRESHOLD

SEVERITY = "medium"  # flags the report until the officer answers (app.trust.flagging)


@dataclass(frozen=True)
class Check:
    code: str  # the trust signal's code (app.modes.PLAIN_SIGNALS words it)
    concept: str  # what both models look at, as the eval's label names it
    judgment_field: str
    judged_values: frozenset  # Spark sees the thing when judgment[field] is one of these
    measurement_field: str  # SAM outlined it when this is true (or a count above 0)
    spark_sees: bool  # the check fires when Spark says this and SAM says the opposite
    asks: bool  # becomes a trust signal; False: measured only (see `why`)
    label: str  # the row on /accuracy and in the README
    why: str  # why it asks, or why it does not
    detail: str  # the signal's detail after "Photo <id>: "

    def spark(self, judgment: dict[str, Any]) -> bool:
        return judgment.get(self.judgment_field) in self.judged_values

    @staticmethod
    def seen(value: Any) -> bool:
        return value is True or (isinstance(value, int | float) and value > 0)

    def sam(self, measurement: dict[str, Any]) -> bool:
        return self.seen(measurement.get(self.measurement_field))

    def fires(self, judgment: dict[str, Any] | None, measurement: dict[str, Any] | None) -> bool:
        """Spark says `spark_sees` and SAM says the opposite (never when either is missing)."""
        if not judgment or not measurement or self.measurement_field not in measurement:
            return False
        return self.spark(judgment) == self.spark_sees and self.sam(measurement) != self.spark_sees


_SAM_FLOOD = (
    f"no mask at confidence {FLOOD_THRESHOLD:g} and under {FLOOD_MIN_AREA:.0%} of the photo "
    f"at {FLOOD_LOW_THRESHOLD:g}"
)
CHECKS: tuple[Check, ...] = (
    Check(
        code="floodwater_not_outlined",
        concept="floodwater",
        judgment_field="floodwater",
        judged_values=frozenset({True}),
        measurement_field="floodwater_present",
        spark_sees=True,
        asks=True,
        label="Floodwater: Spark sees it, SAM outlines none",
        why="Spark's floodwater judgment goes into the counts, so a flood SAM cannot outline "
        "is asked about before the officer relies on it.",
        detail=f"Spark judged floodwater; SAM 3.1 outlined none ({_SAM_FLOOD}).",
    ),
    Check(
        code="floodwater_not_judged",
        concept="floodwater",
        judgment_field="floodwater",
        judged_values=frozenset({True}),
        measurement_field="floodwater_present",
        spark_sees=False,
        asks=False,
        label="Floodwater: SAM outlines it, Spark sees none",
        why="Not asked: by the lake SAM outlines Laguna de Bay, canals, fish-pen water and wet "
        "roads as floodwater, so on the labeled photos Spark was the one who was right.",
        detail="SAM 3.1 outlined floodwater; Spark judged none.",
    ),
    Check(
        code="roof_not_outlined",
        concept="roof_damage",
        judgment_field="roof_damage",
        judged_values=frozenset({"partial"}),
        measurement_field="roof_count",
        spark_sees=True,
        asks=True,
        label="Partly damaged roof: SAM outlines no roof",
        why="A partly damaged roof is still mostly there, so SAM should outline it; when it "
        "outlines no roof at all, the damage judgment is asked about.",
        detail=f"Spark judged a partly damaged roof; SAM 3.1 outlined no roof "
        f"(none at confidence {ROOF_THRESHOLD:g}).",
    ),
    Check(
        code="roof_gone",
        concept="roof_damage",
        judgment_field="roof_damage",
        judged_values=frozenset({"severe"}),
        measurement_field="roof_count",
        spark_sees=True,
        asks=False,
        label="Severe roof damage: SAM outlines no roof",
        why="Not asked: a severely damaged roof may be gone, so finding no roof agrees with "
        "the judgment more often than not.",
        detail=f"Spark judged severe roof damage; SAM 3.1 outlined no roof "
        f"(none at confidence {ROOF_THRESHOLD:g}).",
    ),
)
BY_CODE: dict[str, Check] = {c.code: c for c in CHECKS}
ASKED = tuple(c for c in CHECKS if c.asks)


def disagreements(
    judgment: dict[str, Any] | None, measurement: dict[str, Any] | None, *, asked_only: bool = True
) -> list[Check]:
    """The checks that fire on one photo (only those that ask, unless `asked_only` is False)."""
    return [c for c in (ASKED if asked_only else CHECKS) if c.fires(judgment, measurement)]


def signals(photos: Iterable[Any]) -> list[dict[str, Any]]:
    """The trust signals for a report's photo assessments (app.models.PhotoAssessment)."""
    return [
        {
            "code": check.code,
            "severity": SEVERITY,
            "detail": f"Photo {photo.media_id}: {check.detail} Officer check required.",
            "media_id": photo.media_id,
        }
        for photo in photos
        for check in disagreements(photo.judgment, photo.measurement)
    ]

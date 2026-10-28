"""The demo scenario: synthetic Taglish reports over two weeks after a fictional landfall.

Each report is evidence-shaped: a reporter ref, a day/hour offset from landfall, the
Taglish text (a photo caption when there is a photo), an optional ``photo_prompt``
scene for Muse Image, and an optional ``barangay`` pin name. Reports that must not
be pinned (ambiguous or placeless) leave ``barangay`` null on purpose.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.gazetteer import BY_NAME, find_in_text

# Fixed fictional landfall all scenario times are measured from (naive UTC, like Report).
LANDFALL_AT = datetime(2026, 10, 3, 17, 0)
TYPHOON_NAME = "Bagyong Lusing (fictional)"
# The flooded-street series all reporters describe from their own end of the same street.
STREET_SERIES = "malaban-street"
MIN_REPORTS, MAX_REPORTS = 15, 20


@dataclass(frozen=True)
class ScenarioReport:
    key: str
    reporter: str  # synthetic ref, e.g. "demo-01"
    day: int  # days after landfall (0..14)
    hour: int  # hour of day (0..23)
    text: str  # message text, or the photo caption
    photo_prompt: str | None = None  # scene description for Muse Image
    barangay: str | None = None  # pin name; null means deliberately unpinned
    series: str | None = None
    flood_cm: int | None = None  # stated water depth, for series reports
    corrects: str | None = None  # key of the earlier report this one corrects


def received_at(report: ScenarioReport) -> datetime:
    """When the report supposedly arrived: landfall day + offset, at the given hour."""
    return (LANDFALL_AT + timedelta(days=report.day)).replace(hour=report.hour, minute=10)


def load_scenario(path: Path) -> list[ScenarioReport]:
    """Read the scenario file (a JSON list of report objects)."""
    return [ScenarioReport(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def validate(reports: list[ScenarioReport]) -> list[str]:
    """Check the scenario contract; return a list of problems (empty means valid)."""
    errors: list[str] = []
    if not MIN_REPORTS <= len(reports) <= MAX_REPORTS:
        errors.append(f"need {MIN_REPORTS}-{MAX_REPORTS} reports, have {len(reports)}")
    keys = [r.key for r in reports]
    if len(set(keys)) != len(keys):
        errors.append("report keys are not unique")
    by_key = {r.key: r for r in reports}
    for r in reports:
        if not r.text.strip():
            errors.append(f"{r.key}: text is empty")
        if not 0 <= r.day <= 14:
            errors.append(f"{r.key}: day {r.day} is outside 0-14")
        if not 0 <= r.hour <= 23:
            errors.append(f"{r.key}: hour {r.hour} is outside 0-23")
        if r.barangay is not None and r.barangay not in BY_NAME:
            errors.append(f"{r.key}: unknown barangay {r.barangay!r}")
        if r.corrects is not None and r.corrects not in by_key:
            errors.append(f"{r.key}: corrects unknown report {r.corrects!r}")
    if len({r.barangay for r in reports if r.barangay}) < 4:
        errors.append("need reports in at least 4 different barangays")
    errors.extend(_validate_series(reports))
    errors.extend(_validate_story_beats(reports, by_key))
    return errors


def _validate_series(reports: list[ScenarioReport]) -> list[str]:
    members = [r for r in reports if r.series == STREET_SERIES]
    if len(members) < 3:
        return [f"need 3+ reports in series {STREET_SERIES!r}, have {len(members)}"]
    errors = []
    if len({r.reporter for r in members}) < 3:
        errors.append(f"series {STREET_SERIES!r}: need 3+ different reporters")
    if len({r.day for r in members}) != len(members):
        errors.append(f"series {STREET_SERIES!r}: reports must fall on different days")
    if {r.barangay for r in members} != {"Malaban"}:
        errors.append(f"series {STREET_SERIES!r}: every report must pin Malaban")
    depths = [r.flood_cm for r in sorted(members, key=received_at)]
    if any(d is None for d in depths):
        errors.append(f"series {STREET_SERIES!r}: every report needs a flood depth")
    elif not _rises_then_falls(depths):
        errors.append(f"series {STREET_SERIES!r}: depth must rise then fall, got {depths}")
    return errors


def _rises_then_falls(depths: list[int]) -> bool:
    peak = depths.index(max(depths))
    if peak == 0 or peak == len(depths) - 1:
        return False  # the peak must sit between a rise and a fall
    rising = all(a < b for a, b in zip(depths[:peak], depths[1 : peak + 1], strict=False))
    falling = all(a > b for a, b in zip(depths[peak:], depths[peak + 1 :], strict=False))
    return rising and falling


def _validate_story_beats(
    reports: list[ScenarioReport], by_key: dict[str, ScenarioReport]
) -> list[str]:
    errors = []
    corrections = [r for r in reports if r.corrects is not None]
    if not corrections:
        errors.append("need a correction report (one that corrects an earlier report)")
    elif not any("mali" in r.text.lower() for r in corrections):
        errors.append("the correction should own the mistake ('mali po ...')")
    for r in corrections:
        target = by_key.get(r.corrects or "")
        if target is not None and received_at(r) < received_at(target):
            errors.append(f"{r.key}: a correction must come after {r.corrects}")
    if not any(len(find_in_text(r.text).barangays) >= 2 for r in reports if not r.barangay):
        errors.append("need an unpinned report naming two barangays")
    if not any(r.barangay is None and not find_in_text(r.text).barangays for r in reports):
        errors.append("need a report with no place at all")
    if not any("fish cage" in r.text.lower() or "fish pen" in r.text.lower() for r in reports):
        errors.append("need a fisherfolk report about damaged fish cages/pens")
    if not any("juan dela cruz" in r.text.lower() for r in reports):
        errors.append("need a reporter introducing themself with an obviously fake name")
    return errors

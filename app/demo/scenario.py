"""The demo scenario: synthetic Taglish reports over two weeks after a fictional landfall.

Each report is evidence-shaped: a reporter ref, a day/hour offset from landfall, the
Taglish text (a photo caption when there is a photo), an optional ``photo_prompt``
scene for Muse Image, and an optional ``barangay`` pin name. Reports that must not
be pinned (ambiguous or placeless) leave ``barangay`` null on purpose. A shared
``place`` keeps reports' synthetic pins near each other inside that barangay.
``photo_from`` resends another report's photo file byte for byte (a reused-photo
fake), so no new image is generated for it.

Times are fixed offsets from a fictional landfall. ``anchor_shift`` moves them all by
the same amount so the latest report lands just before a chosen moment (the demo
loader's ``--anchor-now``), keeping every gap between reports.
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
    place: str | None = None  # shared synthetic location anchor within the barangay
    photo_from: str | None = None  # key of a report whose photo this one resends


# --anchor-now puts the latest report this long before the load time.
ANCHOR_LEAD = timedelta(hours=1)


def received_at(report: ScenarioReport, shift: timedelta = timedelta(0)) -> datetime:
    """When the report supposedly arrived: landfall day + offset, at the given hour, moved by
    `shift` (see `anchor_shift`)."""
    return (LANDFALL_AT + timedelta(days=report.day)).replace(hour=report.hour, minute=10) + shift


def anchor_shift(reports: list[ScenarioReport], now: datetime) -> timedelta:
    """The whole-minute shift that puts the latest report ANCHOR_LEAD before `now` (naive UTC),
    so a fresh demo load shows live filing countdowns; every gap between reports is kept."""
    shift = now - ANCHOR_LEAD - max(received_at(r) for r in reports)
    return shift - timedelta(seconds=shift.total_seconds() % 60)


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
        if r.place is not None and (not r.place.strip() or r.barangay is None):
            errors.append(f"{r.key}: place needs a nonempty name and a barangay pin")
        if r.corrects is not None and r.corrects not in by_key:
            errors.append(f"{r.key}: corrects unknown report {r.corrects!r}")
        if r.photo_from is not None:
            source = by_key.get(r.photo_from)
            if source is None or source.photo_prompt is None or source.key == r.key:
                errors.append(f"{r.key}: photo_from must name another report with a photo")
            if r.photo_prompt is not None:
                errors.append(f"{r.key}: a resent photo cannot also have its own photo_prompt")
            # Its own place keeps the copy's pin apart from every other story in that barangay.
            if not r.place or any(o.place == r.place for o in reports if o.key != r.key):
                errors.append(f"{r.key}: a resent photo needs its own place")
    if len({r.barangay for r in reports if r.barangay}) < 4:
        errors.append("need reports in at least 4 different barangays")
    errors.extend(_validate_series(reports))
    errors.extend(_validate_story_beats(reports, by_key))
    return errors


def _validate_series(reports: list[ScenarioReport]) -> list[str]:
    errors = []
    for series in sorted({r.series for r in reports if r.series is not None}):
        group = [r for r in reports if r.series == series]
        places = {r.place for r in group}
        if len(places) != 1 or any(p is None or not p.strip() for p in places):
            errors.append(f"series {series!r}: every report must share one nonempty place")
        if len({r.barangay for r in group}) != 1 or any(r.barangay is None for r in group):
            errors.append(f"series {series!r}: every report must pin the same barangay")
    members = [r for r in reports if r.series == STREET_SERIES]
    if len(members) < 3:
        return [*errors, f"need 3+ reports in series {STREET_SERIES!r}, have {len(members)}"]
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
    if not any(
        r.photo_from in by_key and by_key[r.photo_from].reporter != r.reporter for r in reports
    ):
        errors.append("need a report resending another reporter's photo (photo_from)")
    return errors

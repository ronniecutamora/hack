"""Filing deadlines as data (app/data/deadlines.json), and the countdown to each one.

Every deadline the app shows comes from that one table, with the source that states it; no
other module writes a deadline as a literal. Adding a deadline (crop insurance later, say) is one
more row.

Times are naive UTC like the rest of the database; the table's time zone (Asia/Manila, no
daylight saving) is only used to show them. "Within N calendar days from occurrence" is read
as occurrence + N × 24 hours: the earliest reasonable reading, so the countdown never shows
more time than the assured may have. The countdown is computed when a page is rendered, from
an injected clock, and keeps running after a draft is approved: approval is not filing.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.models import manila_time

DEADLINES_PATH = Path(__file__).parent / "data" / "deadlines.json"
UNITS = {
    "calendar_days": ("calendar day", timedelta(days=1)),
    "hours": ("hour", timedelta(hours=1)),
}


@dataclass(frozen=True)
class Deadline:
    key: str
    label: str
    amount: int
    unit: str  # a key of UNITS
    start: str  # what the window is counted from, as the source says it
    source: str
    url: str
    caveat: str | None = None
    first_notice: dict[str, str] | None = None  # an allowed first notice before the written one

    @property
    def window(self) -> timedelta:
        return UNITS[self.unit][1] * self.amount

    @property
    def window_text(self) -> str:
        name = UNITS[self.unit][0]
        return f"{self.amount} {name}{'s' if self.amount != 1 else ''}"

    def due_at(self, start: datetime) -> datetime:
        """The deadline for a window starting at `start` (naive UTC in and out)."""
        return start + self.window

    def as_dict(self) -> dict[str, Any]:
        """Stored in a draft, so it keeps reading as it was generated."""
        return {
            "key": self.key,
            "label": self.label,
            "window": self.window_text,
            "start": self.start,
            "source": self.source,
            "url": self.url,
            "caveat": self.caveat,
            "first_notice": self.first_notice,
        }


def load(path: Path = DEADLINES_PATH) -> dict[str, Deadline]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("timezone") != "Asia/Manila":
        raise ValueError("deadline times are shown in Asia/Manila only")
    table: dict[str, Deadline] = {}
    for row in data["rows"]:
        deadline = Deadline(**row)
        if deadline.key in table:
            raise ValueError(f"duplicate deadline {deadline.key!r}")
        if deadline.unit not in UNITS or deadline.amount <= 0:
            raise ValueError(f"deadline {deadline.key!r}: bad window")
        if not deadline.source.strip() or not deadline.url.startswith("https://"):
            raise ValueError(f"deadline {deadline.key!r}: every row needs its source")
        table[deadline.key] = deadline
    return table


DEADLINES = load()


def deadline(key: str) -> Deadline:
    return DEADLINES[key]


@dataclass(frozen=True)
class Countdown:
    due_at: datetime  # naive UTC
    overdue: bool
    text: str  # "due in 5h 3m" or "overdue by 2 days"

    @property
    def due_local(self) -> str:
        return manila_time(self.due_at)

    @property
    def due_iso(self) -> str:
        """For the page's live ticker."""
        return self.due_at.isoformat(timespec="seconds") + "Z"


def _hours_minutes(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60}m"


def countdown(due_at: datetime, now: datetime) -> Countdown:
    """Time left until `due_at`, or how long ago it passed (naive UTC). Due exactly now is due."""
    if now <= due_at:
        return Countdown(due_at, False, f"due in {_hours_minutes(due_at - now)}")
    late = now - due_at
    if late < timedelta(days=1):
        return Countdown(due_at, True, f"overdue by {_hours_minutes(late)}")
    days = late.days
    return Countdown(due_at, True, f"overdue by {days} day{'s' if days != 1 else ''}")

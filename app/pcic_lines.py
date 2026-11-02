"""Which PCIC claim lines this office handles: data with a source per line, and one override.

app.nol_form defines every commodity Ulat can draft a claim for. Which of them an office's
officers see (the drafts list, Home's to-do, the filing clocks, the demo's reports) is
app/data/pcic_lines.json: one row per commodity, on or off, each with the source that says the
office's area has that livelihood. Fisheries is on for Biñan; rice is off until a source shows
rice area there. ``PCIC_COMMODITIES`` (Settings.pcic_commodities, comma-separated keys) replaces
the file's on/off for a deployment or a demo capture, so turning a line on is configuration,
never a code change. A draft already made for a line that is off still opens, prints and keeps
its clock on its own page: turning a line off hides it from the lists, it deletes nothing.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.nol_form import COMMODITIES, Commodity

LINES_PATH = Path(__file__).parent / "data" / "pcic_lines.json"


@dataclass(frozen=True)
class Line:
    commodity: Commodity
    on: bool
    source: str
    url: str


def load(path: Path = LINES_PATH) -> tuple[str, list[Line]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    lines = []
    for row in data["lines"]:
        if row["commodity"] not in COMMODITIES:
            raise ValueError(f"pcic_lines: unknown commodity {row['commodity']!r}")
        if not row["source"].strip() or not row["url"].startswith("https://"):
            raise ValueError(f"pcic_lines: {row['commodity']!r} needs its source")
        lines.append(
            Line(COMMODITIES[row["commodity"]], bool(row["on"]), row["source"], row["url"])
        )
    keys = [line.commodity.key for line in lines]
    if sorted(keys) != sorted(COMMODITIES):
        raise ValueError("pcic_lines: every commodity needs exactly one row")
    return data["office"], lines


OFFICE, LINES = load()


def enabled(settings: Settings) -> list[Commodity]:
    """The commodities this deployment drafts claims for, in the file's order."""
    wanted = settings.pcic_commodities
    if wanted is None:
        return [line.commodity for line in LINES if line.on]
    unknown = sorted(set(wanted) - set(COMMODITIES))
    if unknown:
        raise ValueError(f"PCIC_COMMODITIES names unknown commodities: {', '.join(unknown)}")
    return [line.commodity for line in LINES if line.commodity.key in wanted]


def is_enabled(settings: Settings, key: str) -> bool:
    return any(c.key == key for c in enabled(settings))

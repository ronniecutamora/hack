"""What the bot tells a reporter it understood: a short bilingual summary built from the stored
facts, never from a model call, so the reporter can correct a wrong barangay or count.

Only typed fact fields are used (enums and counts) and the place detail goes through the same
redaction as every other output (`redacted_place`): the reply never repeats a name or a phone
number, not even the reporter's own.
"""

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.models import STATUS_ASSESSED, Report
from app.pair_judge import redacted_place

# Taglish first, then English, like every other bot message.
HEADER = "Ito ang naintindihan namin: / Here is what we understood:"
CORRECTION = "Itinala bilang pagtatama. / Recorded as a correction."
PLACE_UNKNOWN = (
    "Lugar / Place: hindi pa alam: magpadala ng location pin. / "
    "not identified yet: send a location pin."
)
FOOTER = "Mali? I-reply ang tamang detalye. / Wrong? Reply with the correct details."
DEPTHS = {
    "none": ("walang baha", "no flooding"),
    "ankle": ("hanggang bukung-bukong", "ankle-deep"),
    "knee": ("hanggang tuhod", "knee-deep"),
    "waist": ("hanggang baywang", "waist-deep"),
    "chest": ("hanggang dibdib", "chest-deep"),
    "above_head": ("lampas-ulo", "above head"),
}
COUNTS = {
    "houses_totally_damaged": ("bahay na wasak", "houses destroyed"),
    "houses_partially_damaged": ("bahay na may sira", "houses partly damaged"),
    "houses_flooded": ("bahay na pinasok ng baha", "houses flooded"),
    "families_affected": ("pamilyang apektado", "families affected"),
    "families_evacuated": ("pamilyang lumikas", "families evacuated"),
    "persons_evacuated": ("taong lumikas", "people evacuated"),
    "dead": ("patay", "dead"),
    "injured": ("sugatan", "injured"),
    "missing": ("nawawala", "missing"),
    "fish_cages_or_pens_damaged": ("fish cage o pen na nasira", "fish cages or pens damaged"),
    "livestock_lost": ("hayop na nawala o namatay", "animals lost"),
}
NEEDS = {
    "food": ("pagkain", "food"),
    "water": ("tubig", "water"),
    "shelter": ("matutuluyan", "shelter"),
    "medicine": ("gamot", "medicine"),
    "rescue": ("rescue", "rescue"),
    "transport": ("sasakyan", "transport"),
    "clearing": ("paglilinis ng daan", "clearing"),
    "livelihood_materials": ("gamit sa kabuhayan", "livelihood materials"),
    "other": ("iba pa", "other"),
}


def _pair(items: list[tuple[str, str]]) -> str:
    return ", ".join(t for t, _ in items) + " / " + ", ".join(e for _, e in items)


def summary_text(
    report: Report, facts: dict[str, Any] | None, place: str | None = None
) -> str | None:
    """The reply for an assessed report; None for any other status (held, failed, pending):
    those get no facts back. `place` is the barangay to show when the report itself names none
    but its incident does (a correction without a place, a report grouped by GPS)."""
    if report.status != STATUS_ASSESSED:
        return None
    facts = facts or {}
    lines = [HEADER]
    if facts.get("is_correction"):
        lines.append(CORRECTION)
    if place := report.barangay or place:
        if detail := redacted_place(facts.get("place_detail")):
            place = f"{detail}, {place}"
        lines.append(f"Lugar / Place: {place}")
    else:
        lines.append(PLACE_UNKNOWN)
    if (depth := facts.get("flood_depth")) in DEPTHS:
        tagalog, english = DEPTHS[depth]
        lines.append(f"Baha / Flood: {tagalog} / {english}")
    counts = [
        (f"{facts[key]} {tagalog}", f"{facts[key]} {english}")
        for key, (tagalog, english) in COUNTS.items()
        if isinstance(facts.get(key), int) and not isinstance(facts.get(key), bool)
    ]
    lines.append(
        f"Bilang / Counts: {_pair(counts)}"
        if counts
        else "Bilang / Counts: walang nabanggit / none stated"
    )
    needs = [NEEDS[n] for n in facts.get("needs") or [] if n in NEEDS]
    if needs:
        lines.append(f"Kailangan / Needs: {_pair(needs)}")
    lines.append(FOOTER)
    return "\n".join(lines)


# --- Which report owes its reporter a summary (bot process memory only) -----------------------

# Seconds a report must stay quiet (no joined caption, pin or album photo) after its assessment
# before the summary goes out, so a photo, its pin and its caption get one summary, not three.
ACK_QUIET_S = 15.0
# A reply to a summary within this time is a correction of that report.
REPLY_WINDOW_S = 24 * 3600.0
SENT_LIMIT = 2000  # remembered summaries; the oldest are forgotten first


@dataclass
class _Pending:
    generation: int
    message: Any  # the latest message about the report: the summary goes to its chat


class Acknowledgements:
    """Reports that owe their reporter a summary, and which sent summary belongs to which report.

    In memory only, by design: the chat lives inside the Telegram message objects and is never
    written anywhere; after a restart, pending summaries are skipped and replies to older
    summaries are ordinary reports. Every message joining a report bumps its generation; only
    the task of the latest generation sends.
    """

    def __init__(
        self,
        quiet_s: float = ACK_QUIET_S,
        reply_window_s: float = REPLY_WINDOW_S,
        limit: int = SENT_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.quiet_s, self.reply_window_s, self.limit, self.clock = (
            quiet_s,
            reply_window_s,
            limit,
            clock,
        )
        self._pending: dict[int, _Pending] = {}
        self._sent: OrderedDict[tuple[Any, Any], tuple[int, float]] = OrderedDict()
        self._flushed = False
        self._flush: asyncio.Event | None = None  # created in the event loop, by quiet()

    async def quiet(self) -> None:
        """Wait out the quiet period, or less once flush() ran (the bot is shutting down)."""
        if self._flushed:
            return
        if self._flush is None:
            self._flush = asyncio.Event()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._flush.wait(), timeout=self.quiet_s)

    def flush(self) -> None:
        """End every quiet period now: at shutdown a summary goes out at once or never."""
        self._flushed = True
        if self._flush is not None:
            self._flush.set()

    def touch(self, report_id: int, message: Any) -> int:
        previous = self._pending.get(report_id)
        generation = previous.generation + 1 if previous else 1
        self._pending[report_id] = _Pending(generation, message)
        return generation

    def due(self, report_id: int, generation: int) -> Any | None:
        """The message to answer if `generation` is still the latest, else None (superseded)."""
        pending = self._pending.get(report_id)
        if pending is None or pending.generation != generation:
            return None
        del self._pending[report_id]
        return pending.message

    def remember(self, sent: Any, report_id: int) -> None:
        key = (getattr(sent, "chat_id", None), getattr(sent, "message_id", None))
        if key[1] is None:
            return
        self._sent[key] = (report_id, self.clock())
        self._sent.move_to_end(key)
        while len(self._sent) > self.limit:
            self._sent.popitem(last=False)

    def replied_to(self, message: Any) -> int | None:
        """The report whose summary `message` replies to, within the reply window."""
        original = getattr(message, "reply_to_message", None)
        if original is None:
            return None
        found = self._sent.get((getattr(message, "chat_id", None), original.message_id))
        if found is None or self.clock() - found[1] > self.reply_window_s:
            return None
        return found[0]

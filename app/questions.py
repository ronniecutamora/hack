"""Simple mode's Review: what waits for the officer, as one question at a time.

Each open reason on a review card (app.review) becomes one question with one or two big answers,
and every answer is one of the officer actions that already exist (merge, keep separate, set
place, signal review, fallen-tree verdict), posted to the same endpoint with the same audit.
Nothing here decides anything: it only says the decision the officer is asked for in plain
words, and never asks what does not apply (a fallen tree only when a photo judgment or the
report's words suggest one).

`plan` derives the questions from the cards alone (no queries), so Home's to-do count, the
navigation badge and the queue's "1 of 9" are the same number. `questions` adds what the queue
shows beside each question (photos, the other report, likely barangays).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, func, select

from app.gazetteer import BARANGAYS, find_in_text
from app.models import MANILA, Assessment, Incident, Media, Report
from app.modes import PLAIN_SIGNALS
from app.review import TREE_VERDICTS, ReviewCard

DUPLICATE, PLACE, ALONE, SIGNAL, TREE = "duplicate", "place", "alone", "signal", "tree"
PLACE_CHOICES_MAX = 4
# The reason the audit records for a barangay picked with a tap (set_place requires one).
PLACE_PICKED_REASON = "Picked on Review from Ulat's likely places for this report"
PLACE_LISTED_REASON = "Picked on Review from the list of Biñan's barangays"


@dataclass(frozen=True)
class Kind:
    """One kind of question: how Home's to-do card counts it."""

    one: str
    many: str  # with "{n}"


# The single table of question kinds; the question text and answers are built in `_answers`.
KINDS: dict[str, Kind] = {
    DUPLICATE: Kind("1 may be the same flood as another report", "{n} may be the same flood"),
    PLACE: Kind("1 report without a known barangay", "{n} reports without a known barangay"),
    ALONE: Kind("1 report Ulat could not compare", "{n} reports Ulat could not compare"),
    SIGNAL: Kind("1 photo or pin to double-check", "{n} photos or pins to double-check"),
    TREE: Kind("1 fallen tree to confirm", "{n} fallen trees to confirm"),
}


@dataclass(frozen=True)
class Answer:
    label: str  # the button
    action: str  # the review endpoint (app.main.REVIEW_ACTIONS)
    fields: dict[str, str]  # hidden fields the action needs
    done: str  # what the snackbar says after it
    tone: str = "filled"  # filled | tonal | outlined


@dataclass
class Question:
    key: str  # stable across renders: the queue's DOM id and its ?at= value
    kind: str
    card: ReviewCard
    signal: tuple[str, dict[str, Any]] | None = None  # (key, signal) for a signal question
    ask: str = ""
    why: str = ""  # one line under the question: why Ulat asks
    answers: list[Answer] = field(default_factory=list)
    other: Incident | None = None  # the flood it may repeat
    other_count: int = 0
    compare: Report | None = None  # the report shown beside this one
    choices: list[str] = field(default_factory=list)  # likely barangays (place)


def plan(cards: list[ReviewCard]) -> list[Question]:
    """The open questions, in the queue's order: each report's place, then whether it repeats
    another flood, then its photo or pin checks, then a suggested fallen tree."""
    out: list[Question] = []
    for card in cards:
        r = card.report
        if card.grouping:
            kind = PLACE if not r.barangay else DUPLICATE if card.targets else ALONE
            out.append(Question(f"q-{r.id}-{kind}", kind, card))
        for key, signal, _verdict in card.open_signals:
            out.append(Question(f"q-{r.id}-signal-{key}", SIGNAL, card, signal=(key, signal)))
        if card.tree_suggested and card.tree_verdict is None:
            out.append(Question(f"q-{r.id}-tree", TREE, card))
    return out


def summary(items: list[Question]) -> str:
    """Home's to-do detail: how many of each kind, most first ("2 may be the same flood; ...")."""
    counts: dict[str, int] = {}
    for q in items:
        counts[q.kind] = counts.get(q.kind, 0) + 1
    parts = [
        KINDS[k].one if n == 1 else KINDS[k].many.format(n=n)
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    text = "; ".join(parts)
    return text[:1].upper() + text[1:]


def day(at: datetime) -> str:
    """A date the way people say it: "Oct 30"."""
    local = at.replace(tzinfo=UTC).astimezone(MANILA)
    return f"{local:%b} {local.day}"


def place_choices(assessment: Assessment | None, report: Report, active: list[str]) -> list[str]:
    """The likely barangays for a report without one: the names the geolocation found too many
    of, then names loosely matched in the words, then where floods are reported now."""
    names: list[str] = []
    if assessment and assessment.geo_source == "ambiguous" and assessment.geo_detail:
        names += assessment.geo_detail.split(" or ")
    facts = (assessment.facts or {}) if assessment else {}
    for text in (
        report.text,
        assessment.transcript_clean if assessment else None,
        report.transcript,
        facts.get("barangay_mentioned"),
    ):
        names += [b.name for b in find_in_text(text, require_cues=False).barangays]
    names += active
    known = {b.name for b in BARANGAYS}
    unique = list(dict.fromkeys(n for n in names if n in known))
    return unique[:PLACE_CHOICES_MAX]


def _answers(q: Question) -> None:
    """The question's text and its answers: the one place each kind is worded."""
    card = q.card
    if q.kind == PLACE:
        q.ask = "Where is this?"
        q.why = "Ulat could not tell the barangay from the message."
        q.answers = [
            Answer(name, "place", {"barangay": name, "note": PLACE_PICKED_REASON}, f"Saved: {name}")
            for name in q.choices
        ]
    elif q.kind == DUPLICATE:
        title = q.other.title if q.other else "another flood"
        n = q.other_count
        q.ask = f"Is this the same flood as {title} ({n} report{'s' if n != 1 else ''})?"
        q.why = "If it is, Ulat counts the damage once."
        q.answers = [
            Answer("Same flood", "merge", {"incident_id": str(q.other.id)}, "Saved: same flood"),
            Answer("Different", "keep", {}, "Saved: a different flood", "tonal"),
        ]
    elif q.kind == ALONE:
        q.ask = "Count this report as its own flood?"
        q.why = "Ulat could not compare it with other reports, and no other flood is open here."
        q.answers = [Answer("Yes, count it on its own", "keep", {}, "Saved: its own flood")]
    elif q.kind == SIGNAL:
        key, signal = q.signal
        statement = PLAIN_SIGNALS.get(signal.get("code", ""), "Something about this report")
        when = f", on {day(q.compare.received_at)}" if q.compare else ""
        q.ask = f"{statement}{when}. Is it OK to use?"
        q.why = "If not, the forms keep marking what it says as doubtful."
        q.answers = [
            Answer(
                "It's fine", "signal", {"signal": key, "verdict": "genuine"}, "Saved: OK to use"
            ),
            Answer(
                "Not OK",
                "signal",
                {"signal": key, "verdict": "problem"},
                "Saved: kept as doubtful",
                "tonal",
            ),
        ]
    elif q.kind == TREE:
        hazards = ((card.assessment.facts or {}).get("hazards") or []) if card.assessment else []
        q.ask = "Is there a fallen tree?"
        q.why = (
            "The message mentions one."
            if "fallen_tree" in hazards
            else "A photo may show one. Ulat cannot tell fallen trees reliably."
        )
        labels = {"confirmed": ("Yes", "Saved: fallen tree"), "absent": ("No", "Saved: no tree")}
        q.answers = [
            Answer(
                labels[v][0], "tree", {"verdict": v}, labels[v][1], "filled" if i == 0 else "tonal"
            )
            for i, v in enumerate(TREE_VERDICTS)
        ]


def photos(session: Session, report_ids: set[int]) -> dict[int, list[Media]]:
    """Each report's photos that are still stored, oldest first."""
    found: dict[int, list[Media]] = {}
    if not report_ids:
        return found
    for media in session.exec(
        select(Media)
        .where(
            col(Media.report_id).in_(report_ids),
            Media.kind == "photo",
            col(Media.purged_at).is_(None),
        )
        .order_by(col(Media.id))
    ).all():
        found.setdefault(media.report_id, []).append(media)
    return found


def questions(session: Session, cards: list[ReviewCard]) -> list[Question]:
    """`plan` with what the queue shows beside each question."""
    items = plan(cards)
    active = [
        name
        for name, _ in session.exec(
            select(Incident.barangay, func.count())
            .where(Incident.active == True, col(Incident.barangay).is_not(None))  # noqa: E712
            .group_by(Incident.barangay)
            .order_by(func.count().desc(), func.max(Incident.last_seen).desc())
        ).all()
    ]
    compare_ids: dict[str, int] = {}
    for q in items:
        card = q.card
        if q.kind == DUPLICATE:
            first = next((d for d in card.comparisons if d.compared_incident_id), None)
            wanted = first.compared_incident_id if first else None
            q.other = next((t for t in card.targets if t.id == wanted), card.targets[0])
            q.other_count = int((q.other.latest_facts or {}).get("report_count") or 0)
            if first and first.compared_report_id and first.compared_incident_id == q.other.id:
                compare_ids[q.key] = first.compared_report_id
            else:
                latest = session.exec(
                    select(Report.id)
                    .where(Report.incident_id == q.other.id)
                    .order_by(col(Report.received_at).desc(), col(Report.id).desc())
                ).first()
                if latest is not None:
                    compare_ids[q.key] = latest
        elif q.kind == SIGNAL and q.signal[1].get("related_report_id"):
            compare_ids[q.key] = int(q.signal[1]["related_report_id"])
        elif q.kind == PLACE:
            q.choices = place_choices(card.assessment, card.report, active)
    wanted_ids = set(compare_ids.values())
    compared = (
        {r.id: r for r in session.exec(select(Report).where(col(Report.id).in_(wanted_ids))).all()}
        if wanted_ids
        else {}
    )
    for q in items:
        q.compare = compared.get(compare_ids.get(q.key, -1))
        _answers(q)
    return items


def current(items: list[Question], at: str | None) -> int:
    """The index of the question the officer asked for (a Skip or Back link), else the first."""
    return next((i for i, q in enumerate(items) if q.key == at), 0)

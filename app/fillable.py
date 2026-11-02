"""Officer entries on a draft form: each field's control, server-side validation, and the view
that merges the generated values with what the officer typed.

The RDANA form (app.rdana_form) and a PCIC claim form (app.nol_form) give every field a
`control` (one of the presets below), its `options`, a plain-language `hint`, whether only the
office can answer it (`ask`) and whether it is `personal`. Templates and validation read those;
no template names a field.

A draft version is never edited in place: saving entries makes the next version (app.drafts).
Entries live beside the generated content, never inside it, so the evidence values, their
links and the "from the reports" counts stay as generated, and every officer value is shown as
the officer's, with the value it replaced one tap away.

Privacy (RA 10173): a personal field (names, ages, phone numbers, emails, addresses, policy or
lot numbers) is typed into the page for the printout only. Its input has no `name`, so the
browser never posts it; the server drops it if one arrives anyway, so it is never stored or
logged. `is_personal` is the one rule.

Every field also carries a short `help` (what to enter, with an example, in plain words) and
its `help_source`: the official document and page that explains it, or "The form's own label"
(and where the label is printed) when no document says more. The "?" button beside each label
shows both; `check_field` refuses a field without them.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from app.models import MANILA

KINDS = ("text", "long", "number", "date", "datetime", "choice", "multi")
CHOICES = ("choice", "multi")
MULTI_SEPARATOR = ", "  # a multi-choice value is its options in form order, joined
NAME_PREFIX = "f."  # posted field names: "f.<key>", so no field can shadow "officer" or "version"
RESTORE = "restore"  # a posted "restore=<key>" drops the officer's entry for that field
EARLIEST = date(2000, 1, 1)  # an older date is a typo on a disaster assessment form
Present = Callable[[str | None], str | None]  # a generated text as the page's mode words it
HELP_MAX = 240  # characters of help text, besides its source line
LABEL_SOURCE = "The form's own label"  # a help_source that cites no document beyond the form
# Shown under every personal field and in the print checklist.
PERSONAL_NOTE = "Saved only on this device for printing. Not stored by Ulat."


def as_is(text: str | None) -> str | None:
    return text


@dataclass(frozen=True)
class Control:
    """How a field is entered and checked."""

    kind: str = "text"
    max_length: int = 300
    min: float | None = None
    max: float | None = None
    integer: bool = True
    past_only: bool = False  # date and date-time: not later than now (Philippine time)
    unit: str | None = None  # shown after a number ("ha", "days")
    keyboard: str | None = None  # a text field's phone keyboard: "tel" or "email"

    @property
    def inputmode(self) -> str | None:
        if self.kind == "number":
            return "numeric" if self.integer else "decimal"
        return self.keyboard


TEXT = Control()
PHONE = Control(max_length=40, keyboard="tel")
EMAIL = Control(max_length=120, keyboard="email")
LONG = Control("long", max_length=2000)
COUNT = Control("number", min=0, max=1_000_000)  # people, families, houses (Biñan: ~400,000 people)
CLASSROOMS = Control("number", min=0, max=10_000)
DAYS = Control("number", min=0, max=365, unit="days")
AGE = Control("number", min=0, max=120)
HECTARES = Control("number", min=0, max=100_000, integer=False, unit="ha")
PERCENT = Control("number", min=0, max=100, integer=False, unit="%")
DATE = Control("date")
PAST_DATE = Control("date", past_only=True)
PAST_DATETIME = Control("datetime", past_only=True)
CHOICE = Control("choice")
MULTI = Control("multi")


class Field(Protocol):
    """What both form definitions give each field."""

    key: str
    label: str
    control: Control
    options: tuple[str, ...]
    personal: bool
    ask: bool
    hint: str | None
    help: str | None
    help_source: str | None

    @property
    def editable(self) -> bool: ...


def is_personal(field: Field) -> bool:
    """A field whose value identifies a person: typed for the printout only, never stored."""
    return field.personal


def storable(field: Field) -> bool:
    return field.editable and not is_personal(field)


def check_field(field: Field, where: str) -> None:
    """Reject a definition the controls cannot render; called by each form's own check."""
    control = field.control
    if control.kind not in KINDS:
        raise ValueError(f"{where}: unknown control {control.kind!r}")
    if control.kind in CHOICES and not field.options:
        raise ValueError(f"{where}: a {control.kind} control needs options")
    for old, new in getattr(field, "renamed", ()):
        if old in field.options or not set(new) <= set(field.options):
            raise ValueError(f"{where}: renamed option {old!r} must map to current options")
    if not (field.help or "").strip() or not (field.help_source or "").strip():
        raise ValueError(f"{where}: every field needs help and a help_source")
    if len(field.help) > HELP_MAX:
        raise ValueError(f"{where}: help is {len(field.help)} characters, over {HELP_MAX}")


def cites_document(field: Field) -> bool:
    """Whether a field's help comes from a document beyond the form's own label."""
    return not (field.help_source or "").startswith(LABEL_SOURCE)


class FieldError(ValueError):
    """A value the control refuses; the message is shown under the field."""


def _bound(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,}"


def _number(text: str, control: Control) -> str:
    try:
        number = float(text.replace(",", ""))
    except ValueError as exc:
        raise FieldError("Enter a number, such as 12.") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise FieldError("Enter a number, such as 12.")
    if control.integer and not number.is_integer():
        raise FieldError("Enter a whole number.")
    if control.min is not None and number < control.min:
        raise FieldError(f"Enter {_bound(control.min)} or more.")
    if control.max is not None and number > control.max:
        raise FieldError(f"Enter {_bound(control.max)} or less.")
    return f"{number:g}" if not number.is_integer() else str(int(number))


def _when(text: str, control: Control, now: datetime) -> str:
    """A date as YYYY-MM-DD, a date-time as YYYY-MM-DD HH:MM, both Philippine time."""
    text = text.replace("T", " ")
    try:
        if control.kind == "date":
            value = datetime.strptime(text, "%Y-%m-%d")
        else:
            value = datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
            if len(text) not in (16, 19):  # minutes, or seconds a browser may add
                raise ValueError(text)
    except ValueError as exc:
        example = "2026-10-03" if control.kind == "date" else "2026-10-03 19:10"
        raise FieldError(
            f"Enter a date{' and time' * (control.kind != 'date')}, like {example}."
        ) from exc
    if value.date() < EARLIEST:
        raise FieldError("Check the year: the date is too far in the past.")
    local_now = now.replace(tzinfo=UTC).astimezone(MANILA).replace(tzinfo=None)
    later = value.date() > local_now.date() if control.kind == "date" else value > local_now
    if control.past_only and later:
        raise FieldError("This cannot be in the future (Philippine time).")
    return value.strftime("%Y-%m-%d" if control.kind == "date" else "%Y-%m-%d %H:%M")


def normalize(field: Field, values: Sequence[str], now: datetime) -> str | None:
    """One field's posted values as the stored text, or None when empty; FieldError if invalid.

    `now` is naive UTC. Choices must be the form's own options; a multi-choice keeps them in the
    form's order.
    """
    control = field.control
    picked = [v for v in values if v.strip()]
    if control.kind == "multi":
        unknown = [v for v in picked if v not in field.options]
        if unknown:
            raise FieldError("Choose from the form's own options.")
        chosen = [o for o in field.options if o in picked]
        return MULTI_SEPARATOR.join(chosen) or None
    if len(picked) > 1:
        raise FieldError("Give one answer.")
    if not picked:
        return None
    text = picked[0]
    if control.kind == "long":
        text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    else:
        text = " ".join(text.split())
    if not text:
        return None
    if len(text) > control.max_length:
        raise FieldError(f"Use at most {control.max_length} characters.")
    if control.kind == "choice":
        if text not in field.options:
            raise FieldError("Choose from the form's own options.")
        return text
    if control.kind == "number":
        return _number(text, control)
    if control.kind in ("date", "datetime"):
        return _when(text, control, now)
    return text


def parts(field: Field, value: str) -> list[str]:
    """A stored value as the pieces it names: a multi-choice value split back into its options
    (an option may itself hold the separator, as "Banditry, Hold-up" does: the longest option
    wins), a choice as its one answer. An option the form has since reworded (`renamed`) reads
    as its current wording, so a value saved before still ticks the right boxes."""
    choice = field.control.kind == "choice"
    pieces = [value] if choice else value.split(MULTI_SEPARATOR)
    renamed = dict(getattr(field, "renamed", ()))
    known = set(field.options) | set(renamed)
    out, i = [], 0
    while i < len(pieces):
        j = next(
            (j for j in range(len(pieces), i, -1) if MULTI_SEPARATOR.join(pieces[i:j]) in known),
            i + 1,
        )
        piece = MULTI_SEPARATOR.join(pieces[i:j])
        out.extend(renamed.get(piece, (piece,)))
        i = j
    return out


def fits(field: Field, value: str | None, now: datetime) -> str | None:
    """A generated value as the control would hold it, or None when it does not fit the
    control (then the page shows it as "From the reports" beside an empty control)."""
    if value is None:
        return None
    try:
        values = parts(field, value) if field.control.kind in CHOICES else [value]
        return normalize(field, values, now)
    except FieldError:
        return None


def posted_name(field: Field) -> str:
    return NAME_PREFIX + field.key


@dataclass
class FillResult:
    """The changes a save makes, or the errors that stop it."""

    changes: dict[str, str | None]  # field key -> new value (None: emptied)
    restored: list[str]  # field keys whose officer entry is dropped
    errors: dict[str, str]
    posted: dict[str, list[str]]  # what was typed, to show again with the errors


def read_post(
    fields: Iterable[Field],
    pairs: Sequence[tuple[str, str]],
    generated: dict[str, str | None],
    entries: dict[str, dict[str, Any]],
    now: datetime,
    present: Present = as_is,
) -> FillResult:
    """Compare what was posted with what the version shows.

    Only fields present in the post are read (each control posts an empty hidden value first,
    so an unticked choice still arrives). Personal fields are dropped unread. A value equal to
    the one the page showed is no change; a value equal to the generated one drops the entry.
    `present` is how the page worded generated text (Simple mode's plain words), so an untouched
    field is no change in either mode.
    """
    posted: dict[str, list[str]] = {}
    restore = {v for k, v in pairs if k == RESTORE}
    for name, value in pairs:
        if name.startswith(NAME_PREFIX):
            posted.setdefault(name.removeprefix(NAME_PREFIX), []).append(value)
    result = FillResult({}, [], {}, {})
    for field in fields:
        if not storable(field):
            continue
        if field.key in restore and field.key in entries:
            result.restored.append(field.key)
            continue
        if field.key not in posted:
            continue
        result.posted[field.key] = posted[field.key]
        try:
            value = normalize(field, posted[field.key], now)
        except FieldError as exc:
            result.errors[field.key] = str(exc)
            continue
        gen = generated.get(field.key)
        shown = shown_value(field, present(gen), entries.get(field.key), now)
        if value == shown:
            continue
        if field.key in entries and value in (
            fits(field, gen, now),
            fits(field, present(gen), now),
        ):
            result.restored.append(field.key)
        else:
            result.changes[field.key] = value
    return result


def shown_value(
    field: Field, generated: str | None, entry: dict[str, Any] | None, now: datetime
) -> str | None:
    """What the field's control holds when the page is drawn: an entry saved in an option's
    old wording reads in the current one, so it is no change unless the officer makes one."""
    if entry is not None:
        value = entry.get("value")
        if value and field.control.kind in CHOICES:
            return fits(field, value, now) or value
        return value
    return fits(field, generated, now)


def apply(
    entries: dict[str, dict[str, Any]],
    result: FillResult,
    generated: dict[str, str | None],
    officer: str,
    at: datetime,
) -> dict[str, dict[str, Any]]:
    """The next version's entries: each change records who, when (naive UTC, ISO) and the
    generated value it was typed over (`base`), so a later version can say the evidence moved."""
    merged = {k: v for k, v in entries.items() if k not in result.restored}
    for key, value in result.changes.items():
        merged[key] = {
            "value": value,
            "by": officer,
            "at": at.isoformat(timespec="seconds"),
            "base": generated.get(key),
        }
    return merged


def carry(
    previous: dict[str, dict[str, Any]] | None, generated: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Entries a regenerated version keeps: all of the previous version's, except where the
    officer gave the value again when generating (the generated value is the officer's then)."""
    return {
        key: entry
        for key, entry in (previous or {}).items()
        if (generated.get(key) or {}).get("source") != "officer"
    }


def view(
    field: Any,
    generated: dict[str, Any] | None,
    entry: dict[str, Any] | None,
    now: datetime,
    *,
    error: str | None = None,
    posted: list[str] | None = None,
    present: Present = as_is,
) -> dict[str, Any]:
    """Everything a template needs to draw one field, on screen and on paper."""
    generated = generated or {}
    gen_value = generated.get("value")
    shown = present(gen_value)
    value = entry.get("value") if entry is not None else shown
    if posted is not None:  # a refused save: show what was typed
        typed = [v for v in posted if v.strip()]
        current = (
            MULTI_SEPARATOR.join(typed) if field.control.kind == "multi" else (typed or [""])[0]
        )
    else:
        current = shown_value(field, shown, entry, now) or ""
    selected = set(_options_in(field, current)) if field.control.kind in CHOICES else set()
    # The generated value the control cannot hold (free text over a choice, "... (first
    # reported)" over a date): shown beside the empty control, never lost.
    unmatched = (
        shown
        if entry is None and gen_value is not None and fits(field, shown, now) is None
        else None
    )
    moved = None
    if entry is not None and entry.get("base") != gen_value:
        moved = {"was": present(entry.get("base")), "now": shown}
    return {
        "key": field.key,
        "field": field,
        "control": field.control,
        "options": list(field.options),
        "personal": is_personal(field),
        "editable": field.editable,
        "storable": storable(field),
        "ask": field.ask,
        "hint": field.hint,
        "help": field.help,
        "help_source": field.help_source,
        "name": posted_name(field),
        "generated": generated,
        "entry": entry,
        "entry_at": _entry_time(entry),
        "value": value,
        "printed": _options_in(field, value),
        "current": current,
        "selected": selected,
        "unmatched": unmatched,
        "moved": moved,
        "error": error,
        "filled": bool(value),
        "from_reports": gen_value is not None,
        "number": getattr(field, "number", field.key),
        "label": field.label,
        "sic": getattr(field, "sic", False),
        "required": getattr(field, "required", False),
        # Worth a word before printing when empty: what the office must answer, or the form
        # requires, and Ulat keeps (a personal field is a ruled line to write on by design).
        "needed": storable(field) and (field.ask or getattr(field, "required", False)),
    }


def _options_in(field: Field, value: str | None) -> list[str]:
    """The form's options a value names: the one answer, or each of a multi-choice's."""
    if not value or not field.options:
        return []
    named = parts(field, value)  # beside a number or text: the boxes its text names
    return [o for o in field.options if o in named]


def _entry_time(entry: dict[str, Any] | None) -> str | None:
    if not entry or not entry.get("at"):
        return None
    at = datetime.fromisoformat(entry["at"]).replace(tzinfo=UTC).astimezone(MANILA)
    return at.strftime("%Y-%m-%d %H:%M")


def progress(views: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Done and total of the fields the office must answer and Ulat keeps."""
    asked = [v for v in views if v["ask"] and v["storable"]]
    return sum(v["filled"] for v in asked), len(asked)

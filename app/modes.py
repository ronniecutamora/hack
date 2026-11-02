"""The dashboard's two presentation modes, and the plain words Simple mode uses.

Simple mode (the default) is for the officer: a to-do list, plain language, no model names,
costs, ids or agent internals. Technical mode is for developers, judges and the demo's "show the
agent" beat: everything the agent recorded. The mode is decided on the server for each request
(a cookie the browser keeps, or `?mode=technical` on any link) and only changes what a page
shows: every officer action, form and permission is the same in both.

The mode lives in a context variable for the request, like the signed-in officer, so every
template and macro can ask `technical()` without passing it around.
"""

from collections.abc import Callable
from contextvars import ContextVar
from typing import Literal, get_args

from app.deadlines import deadline
from app.nol_form import COMMODITIES, Commodity
from app.rdana import RDANA_DEADLINE

Mode = Literal["simple", "technical"]
MODES: tuple[str, ...] = get_args(Mode)
MODE_COOKIE = "ulat_mode"
MODE_PARAM = "mode"
MODE_MAX_AGE_S = 365 * 24 * 3600  # a preference of this browser, not personal data
GUIDE_COOKIE = "ulat_guide"  # "hidden" once the first-run guide is dismissed

current_mode: ContextVar[str] = ContextVar("current_mode", default="simple")


def technical() -> bool:
    """The page shows the agent's internals (Technical mode)."""
    return current_mode.get() == "technical"


def resolve(asked: str | None, stored: str | None, default: str) -> str:
    """The mode for a request: a `?mode=` link first, then the browser's cookie, then the
    configured default. Unknown values are ignored."""
    for value in (asked, stored):
        if value in MODES:
            return value
    return default


def _people(commodities: list[Commodity]) -> list[str]:
    return [c.claimants for c in commodities]


def _possessive(people: str) -> str:
    """The possessive of a plural or collective noun: fisherfolk's, rice farmers'."""
    return people + ("'" if people.endswith("s") else "'s")


def _and(words: list[str]) -> str:
    """Words joined as a list: a; a and b; a, b and c."""
    return " and ".join(filter(None, [", ".join(words[:-1]), *words[-1:]]))


def guide_steps(commodities: list[Commodity]) -> tuple[tuple[str, str], ...]:
    """The "How Ulat works" steps the first-run guide on Home and the Help page show, naming
    the PCIC claims this office drafts (app.pcic_lines)."""
    claims = [f"the {_possessive(c.claimants)} {c.documents}" for c in commodities]
    return (
        (
            "Reports come in",
            f"{_and(['Residents', 'barangay officials', *_people(commodities)])} send photos, "
            "voice notes and texts to the Ulat Telegram bot.",
        ),
        (
            "Ulat sorts them and drafts the forms",
            "It reads each report, puts reports about the same flood together and fills in the "
            f"{_and(['RDANA form', *claims])}.",
        ),
        (
            "You check and approve",
            "Home lists what needs you. Nothing is sent or filed by itself: a form counts only "
            "after you approve it.",
        ),
    )


def drafts_help(commodities: list[Commodity]) -> str:
    """The Help page's line on Drafts, with the claims this office drafts."""
    documents = [f"the {c.documents}" for c in commodities]
    outputs = _and(["The RDANA forms", "the damaged-houses table", *documents])
    text = f"{outputs}. Nothing is sent or filed until you approve it"
    if not commodities:
        return text + "."
    own = commodities[0].document if len(commodities) == 1 else "claims"
    return f"{text}, and the {_and(_people(commodities))} file their own {own}."


def _claim_terms() -> dict[str, tuple[str, str]]:
    """Each PCIC claim form's "What's this?" entry, its filing window from the deadlines table."""
    return {
        c.glossary[0]: (
            c.glossary[1],
            c.glossary[2].format(window=deadline(c.deadline).window_text),
        )
        for c in COMMODITIES.values()
        if c.glossary
    }


# Official terms, explained once where they appear ("What's this?"); templates ask by key.
GLOSSARY: dict[str, tuple[str, str]] = {
    "rdana": (
        "RDANA",
        "Rapid Damage Assessment and Needs Analysis: the form a city fills in after a disaster "
        "to say what was damaged and what people need. Ulat drafts Form 1 for each barangay; "
        f"completed forms go to the Emergency Operations Center within "
        f"{deadline(RDANA_DEADLINE).window_text}.",
    ),
    "dromic": (
        "DROMIC",
        "The DSWD's disaster report (Disaster Response Operations Monitoring and Information "
        "Center). Ulat's damaged-houses table uses its columns: houses totally and partially "
        "damaged, per barangay.",
    ),
    **_claim_terms(),
    "incident": (
        "Incident",
        "Reports about the same flood or damage in one barangay, put together by Ulat so one "
        "situation is counted once. If Ulat is not sure two reports belong together, it asks "
        "you on Review.",
    ),
    "outline": (
        "Outline",
        "The outline shows exactly what Ulat measured in the photo, so you can check it in a "
        "second: roofs in orange, floodwater in blue. It is evidence, not the loss figure.",
    ),
}

CLAIM_TERMS = {c.glossary[0] for c in COMMODITIES.values() if c.glossary}


def glossary(commodities: list[Commodity]) -> dict[str, tuple[str, str]]:
    """The Help page's terms: every term, but only the claim forms this office drafts."""
    shown = {c.glossary[0] for c in commodities if c.glossary}
    return {k: v for k, v in GLOSSARY.items() if k not in CLAIM_TERMS or k in shown}


# Internal statuses, said plainly (Simple mode); Technical mode shows the raw value.
PLAIN_STATUS: dict[str, str] = {
    "needs_officer": "Needs your check",
    "open": "Up to date",
    "new": "Just arrived",
    "assessing": "Being read",
    "assessed": "Read",
    "assessment_failed": "Could not be read fully",
    "transcription_failed": "Voice note could not be read",
    "held": "On hold",
    "not_a_report": "Set aside",
    "draft": "Waiting for your approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "superseded": "Replaced by a newer version",
}

# Why a report waits on Review, said plainly (keys are app.review's reasons).
PLAIN_REASONS: dict[str, str] = {
    "grouping uncertain": "May be the same flood as another report",
    "duplicate check failed": "Ulat could not check if it repeats another report",
    "place unknown": "Barangay not known",
    "fallen tree to confirm": "Fallen tree to confirm",
}


def plain_status(status: str) -> str:
    return PLAIN_STATUS.get(status, status.replace("_", " "))


def plain_reason(reason: str) -> str:
    if reason.endswith("to check") and "trust signal" in reason:
        count = reason.split(" ", 1)[0]
        return "Something to double-check" if count == "1" else f"{count} things to double-check"
    return PLAIN_REASONS.get(reason, reason)


# Trust signals (app.trust codes), said plainly: what the officer should double-check.
PLAIN_SIGNALS: dict[str, str] = {
    "photo_reused": "Someone else already sent this same photo",
    "photo_resent": "The same person sent this photo before",
    "photo_old": "The photo may be older than this flood",
    "pin_outside_binan": "The map pin is outside Biñan",
    "pin_text_mismatch": "The map pin and the place in the message do not match",
    # Spark and SAM disagree on a photo (app.crosscheck)
    "floodwater_not_outlined": "The photo check sees floodwater, but the outline found none",
    "roof_not_outlined": "The photo check sees a damaged roof, but the outline found no roof",
}

# What an officer's review action did, for the confirmation after it (Simple mode); Technical
# mode names the report and incident (app.main.DONE_MESSAGES).
PLAIN_DONE: dict[str, str] = {
    "merge": "Done: the report is now part of that incident.",
    "keep": "Done: the report stays as its own incident.",
    "place": "Done: barangay saved, and Ulat grouped the report again.",
    "signal": "Done: your check is saved.",
    "tree": "Done: your answer is saved.",
    "block": "Done: the sender is blocked. Their new messages are put on hold.",
    "unblock": "Done: the sender is unblocked.",
    "restore": "Done: restored as a report. Ulat reads it again.",
}


def plain_signal(signal: dict) -> str:
    return PLAIN_SIGNALS.get(signal.get("code", ""), signal.get("detail") or "Something to check")


# The officer's answer to a trust signal (keys are app.review.SIGNAL_VERDICTS'), as radio labels.
PLAIN_VERDICTS: dict[str, str] = {"genuine": "It is genuine", "problem": "There is a problem"}

# Model names inside drafted text (notes and caveats the drafting code writes), said plainly in
# Simple mode. Longest first; Technical mode shows the text as written.
PLAIN_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ('SAM 3.1 is prompted with "floodwater", which', 'The "floodwater" measurement'),
    ("the agent's judgments and SAM 3.1 measurements", "Ulat's photo judgments and measurements"),
    ("SAM 3.1 measurements", "photo measurements"),
    ("SAM 3.1", "the photo measurement"),
)


def plain(text: str | None) -> str:
    """Drafted text for the current mode: model names replaced in Simple mode."""
    text = text or ""
    return text if technical() else plain_words(text)


def plain_words(text: str) -> str:
    """Drafted text in Simple mode's words, whatever the current mode."""
    for old, new in PLAIN_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def presenter() -> Callable[[str | None], str | None]:
    """How the current mode words generated text, as a function that can run anywhere (a
    worker thread does not see the request's mode)."""
    if technical():
        return lambda text: text
    return lambda text: None if text is None else plain_words(text)

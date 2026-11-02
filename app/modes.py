"""The dashboard's two presentation modes, and the plain words Simple mode uses.

Simple mode (the default) is for the officer: a to-do list, plain language, no model names,
costs, ids or agent internals. Technical mode is for developers, judges and the demo's "show the
agent" beat: everything the agent recorded. The mode is decided on the server for each request
(a cookie the browser keeps, or `?mode=technical` on any link) and only changes what a page
shows: every officer action, form and permission is the same in both.

The mode lives in a context variable for the request, like the signed-in officer, so every
template and macro can ask `technical()` without passing it around.
"""

from contextvars import ContextVar
from typing import Literal, get_args

from app.deadlines import deadline
from app.nol_form import COMMODITIES
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


# "How Ulat works": the first-run guide on Home and the Help page show these same steps.
GUIDE_STEPS: tuple[tuple[str, str], ...] = (
    (
        "Reports come in",
        "Residents, barangay officials and fisherfolk send photos, voice notes and texts to "
        "the Ulat Telegram bot.",
    ),
    (
        "Ulat sorts them and drafts the forms",
        "It reads each report, puts reports about the same flood together and fills in the "
        "RDANA form and the fisherfolk's Notices of Loss.",
    ),
    (
        "You check and approve",
        "Home lists what needs you. Nothing is sent or filed by itself: a form counts only "
        "after you approve it.",
    ),
)


def _nol_window() -> str:
    """The Notice of Loss filing window from the deadlines table, for every commodity."""
    windows = sorted({deadline(c.deadline).window_text for c in COMMODITIES.values()})
    return " or ".join(windows)


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
    "nol": (
        "Notice of Loss",
        "The form an insured fisherfolk sends to PCIC (the Philippine Crop Insurance "
        "Corporation) to say their fish cage or pen was damaged. It is due within "
        f"{_nol_window()} of the damage, so Ulat counts down the time. Ulat drafts it with the "
        "photos as evidence; the fisherfolk files it.",
    ),
    "incident": (
        "Incident",
        "Reports about the same flood or damage in one barangay, put together by Ulat so one "
        "situation is counted once. If Ulat is not sure two reports belong together, it asks "
        "you on Review.",
    ),
}

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
    if technical():
        return text
    for old, new in PLAIN_REPLACEMENTS:
        text = text.replace(old, new)
    return text

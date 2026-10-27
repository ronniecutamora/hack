"""Remove Voice Transcribe's repeated-word artifacts from a transcript.

Measured on Taglish test notes (2026-10-27): the model sometimes repeats a word or phrase
("totally totally", "puno sa puno sa") or a word's ending ("Luntian,ian"), at different
spots on identical requests. Collapsing immediate repeats lowered WER from 0.285 to 0.253.

Tagalog repeats words on purpose (unti-unti, araw-araw). Written with a hyphen they are one
token and never touched; the allow-list below protects the common ones when the hyphen is lost.
The raw transcript is always kept as evidence; this cleaned text is used for extraction.
"""

import re

MAX_PHRASE_WORDS = 4
# Bases of common Tagalog full reduplication ("unti unti" = gradually, "isa isa" = one by
# one, "araw araw" = every day). "umaga umaga" is deliberately absent: it appeared as an
# artifact in the test notes, and "tuwing umaga" is the usual phrasing in reports.
REDUPLICATED = frozenset(
    {
        "agad",
        "ano",
        "araw",
        "bahay",
        "buwan",
        "dahan",
        "gabi",
        "halo",
        "iba",
        "isa",
        "kani",
        "linggo",
        "paru",
        "saan",
        "sabay",
        "sari",
        "sino",
        "taon",
        "tig",
        "unti",
    }
)
_WORD = re.compile(r"[^\w]+")
# "Luntian,ian": a word followed by a comma and a repeat of its own ending.
_ECHOED_SUFFIX = re.compile(r"^([^\W\d_]{3,}),([^\W\d_]{2,})(\W*)$")


def _norm(token: str) -> str:
    return _WORD.sub("", token.lower())


def _trailing_punct(token: str) -> str:
    match = re.search(r"\W*$", token)
    return match.group(0) if match else ""


def _fix_echoed_suffix(token: str) -> str:
    match = _ECHOED_SUFFIX.match(token)
    if match and match[1].lower().endswith(match[2].lower()):
        return match[1] + match[3]
    return token


def clean_transcript(text: str) -> str:
    """Collapse immediately repeated words and phrases (1 to 4 words), keeping the first copy."""
    tokens = [_fix_echoed_suffix(t) for t in text.split()]
    changed = True
    while changed:
        changed = False
        for size in range(MAX_PHRASE_WORDS, 0, -1):
            i = 0
            while i + 2 * size <= len(tokens):
                first = [_norm(t) for t in tokens[i : i + size]]
                second = [_norm(t) for t in tokens[i + size : i + 2 * size]]
                protected = size == 1 and first[0] in REDUPLICATED
                if first == second and all(first) and not protected:
                    # Keep the first copy's words but the second copy's closing punctuation.
                    last = tokens[i + size - 1]
                    bare = last[: len(last) - len(_trailing_punct(last))]
                    tokens[i + size - 1] = bare + _trailing_punct(tokens[i + 2 * size - 1])
                    del tokens[i + size : i + 2 * size]
                    changed = True
                else:
                    i += 1
    return " ".join(tokens)

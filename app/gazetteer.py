"""Biñan's 24 barangays: the single source of truth for place names.

Used for Voice Transcribe keyword biasing, for matching barangay names in texts and transcripts,
and (with app.geo) for mapping a GPS point to a barangay.

Names and PSGC-based codes come from the OCHA COD-AB Philippines dataset (PSA and NAMRIA sources,
admin level 4, version v03, valid on 2025-02-13), downloaded 2026-10-27 from
https://data.humdata.org/dataset/cod-ab-phl (license CC BY-IGO). The 24 names match PhilAtlas'
list for Biñan (https://www.philatlas.com/luzon/r04a/laguna/binan.html).
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass
from functools import cache

CITY_PCODE = "PH0403403"


@dataclass(frozen=True)
class Barangay:
    pcode: str
    name: str  # display name
    aliases: tuple[str, ...] = ()  # other spellings people use, including former names
    # Names that are also common words ("malamig" = cold, "tubigan" = irrigated field) or the
    # city's own name: matched in text only after "barangay"/"brgy".
    prefix_only: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


BARANGAYS: tuple[Barangay, ...] = (
    Barangay("PH0403403001", "Biñan", ("Biñan Poblacion",), ("Biñan", "Biñan Poblacion")),
    Barangay("PH0403403002", "Bungahan"),
    Barangay("PH0403403003", "Santo Tomas", ("Sto. Tomas", "Calabuso")),
    Barangay("PH0403403004", "Canlalay"),
    Barangay("PH0403403005", "Casile"),
    Barangay("PH0403403006", "De La Paz", ("Dela Paz",)),
    Barangay("PH0403403007", "Ganado"),
    Barangay("PH0403403008", "San Francisco", ("Halang",), ("Halang",)),
    Barangay("PH0403403009", "Langkiwa"),
    Barangay("PH0403403011", "Loma", prefix_only=("Loma",)),
    Barangay("PH0403403012", "Malaban"),
    Barangay("PH0403403013", "Malamig", prefix_only=("Malamig",)),
    Barangay("PH0403403014", "Mamplasan", ("Mampalasan",)),
    Barangay("PH0403403015", "Platero"),
    Barangay("PH0403403016", "Poblacion"),
    Barangay("PH0403403018", "Santo Niño", ("Sto. Niño",)),
    Barangay("PH0403403019", "San Antonio"),
    Barangay("PH0403403020", "San Jose"),
    Barangay("PH0403403021", "San Vicente"),
    Barangay("PH0403403022", "Soro-soro"),
    Barangay("PH0403403023", "Santo Domingo", ("Sto. Domingo",)),
    Barangay("PH0403403024", "Timbao"),
    Barangay("PH0403403025", "Tubigan", prefix_only=("Tubigan",)),
    Barangay("PH0403403026", "Zapote"),
)
BY_PCODE = {b.pcode: b for b in BARANGAYS}
BY_NAME = {b.name: b for b in BARANGAYS}

# Words that introduce a barangay name in Filipino reports ("Brgy. Malamig", "barangay Loma").
_PREFIXES = ("barangay", "baranggay", "brgy", "bgy", "brg")
# Fuzzy matches are only tried right after a prefix, and must be this similar (0..1).
FUZZY_CUTOFF = 0.82
_NON_WORD = re.compile(r"[^a-z0-9]+")
_ABBREVIATIONS = {"sto": "santo", "sta": "santa"}


def normalize(text: str) -> str:
    """Lowercase, drop accents (ñ -> n), expand Sto./Sta., turn punctuation into spaces."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    words = _NON_WORD.sub(" ", text).split()
    return " ".join(_ABBREVIATIONS.get(w, w) for w in words)


def transcription_keywords() -> list[str]:
    """Names to bias Voice Transcribe toward, with their usual spelling (accents kept)."""
    keywords: list[str] = []
    for barangay in BARANGAYS:
        for name in barangay.names:
            if not name.startswith(("Sto.", "Sta.")) and name not in keywords:
                keywords.append(name)
    return keywords


@dataclass(frozen=True)
class TextMatch:
    """Result of looking for barangay names in free text."""

    barangays: tuple[Barangay, ...]  # distinct matches, in order of first mention
    matched: tuple[str, ...]  # the words in the text that matched
    fuzzy: bool = False  # True if any match was approximate (likely a transcription slip)

    @property
    def barangay(self) -> Barangay | None:
        """The match, only when exactly one barangay was named."""
        return self.barangays[0] if len(self.barangays) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.barangays) > 1


@cache
def _patterns() -> list[tuple[re.Pattern[str], Barangay]]:
    prefix = r"(?:" + "|".join(_PREFIXES) + r") "
    patterns = []
    # Longest names first so "san francisco" wins over a shorter overlapping alias.
    for barangay, name in sorted(
        ((b, normalize(n)) for b in BARANGAYS for n in b.names), key=lambda x: -len(x[1])
    ):
        lead = prefix if name in {normalize(n) for n in barangay.prefix_only} else ""
        patterns.append((re.compile(rf"\b{lead}({re.escape(name)})\b"), barangay))
    return patterns


def find_in_text(text: str | None) -> TextMatch:
    """Find barangay names in a text or transcript.

    Exact matches (accent- and abbreviation-insensitive) are tried first. Only if there are
    none, the one to three words after "barangay"/"brgy" are fuzzy-matched, which catches
    transcription slips like "Mamplasan" -> "Mamplason" without matching ordinary words.
    """
    if not text:
        return TextMatch((), ())
    norm = normalize(text)
    hits: list[tuple[int, Barangay, str]] = []
    taken: list[range] = []
    for pattern, barangay in _patterns():
        for m in pattern.finditer(norm):
            span = range(m.start(1), m.end(1))
            if any(span.start < t.stop and t.start < span.stop for t in taken):
                continue  # already covered by a longer name
            taken.append(span)
            hits.append((m.start(1), barangay, m.group(1)))
    if hits:
        return _as_match(hits, fuzzy=False)
    return _as_match(_fuzzy_after_prefix(norm), fuzzy=True)


def _fuzzy_after_prefix(norm: str) -> list[tuple[int, Barangay, str]]:
    candidates = {normalize(n): b for b in BARANGAYS for n in b.names}
    hits = []
    words = norm.split()
    for i, word in enumerate(words):
        if word not in _PREFIXES:
            continue
        best: tuple[float, Barangay, str] | None = None
        for size in (1, 2, 3):
            phrase = " ".join(words[i + 1 : i + 1 + size])
            if not phrase:
                break
            for name, barangay in candidates.items():
                score = difflib.SequenceMatcher(None, phrase, name).ratio()
                if score >= FUZZY_CUTOFF and (best is None or score > best[0]):
                    best = (score, barangay, phrase)
        if best:
            hits.append((len(" ".join(words[:i])), best[1], best[2]))
    return hits


def _as_match(hits: list[tuple[int, Barangay, str]], *, fuzzy: bool) -> TextMatch:
    barangays: list[Barangay] = []
    matched: list[str] = []
    for _, barangay, text in sorted(hits, key=lambda h: h[0]):
        if barangay not in barangays:
            barangays.append(barangay)
        matched.append(text)
    return TextMatch(tuple(barangays), tuple(matched), fuzzy=fuzzy and bool(barangays))

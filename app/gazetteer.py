"""Biñan's 24 barangays: the single source of truth for place names.

Used for Voice Transcribe keyword biasing, for matching barangay names in texts and transcripts,
and (with app.geo) for mapping a GPS point to a barangay.

Names and PSGC-based codes come from the OCHA COD-AB Philippines dataset (PSA and NAMRIA sources,
admin level 4, version v03, valid on 2025-02-13), downloaded 2026-10-27 from
https://data.humdata.org/dataset/cod-ab-phl (license CC BY-IGO). The 24 names match PhilAtlas'
list for Biñan (https://www.philatlas.com/luzon/r04a/laguna/binan.html).

Street, subdivision and landmark names for keyword biasing come from OpenStreetMap
(app/data/binan_places.json, rebuilt by scripts.build_places; © OpenStreetMap contributors,
ODbL 1.0). They are never used to match or place a report: only to bias the transcript.
"""

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

CITY_PCODE = "PH0403403"


@dataclass(frozen=True)
class Barangay:
    pcode: str
    name: str  # display name
    aliases: tuple[str, ...] = ()  # other spellings people use, including former names
    # Names that are also common words ("malamig" = cold, "tubigan" = irrigated field) or the
    # city's own name: matched in text only after "barangay"/"brgy".
    prefix_only: tuple[str, ...] = ()
    # Names that are also surnames, words or saints ("Rosa Dela Paz", "ganado", "Santo Niño
    # chapel"): matched in text only after a place cue ("sa", "taga", "galing", a prefix, ...)
    # and not when an institution word follows ("San Jose church").
    cue_only: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


BARANGAYS: tuple[Barangay, ...] = (
    # "Biñan Poblacion" is unambiguous; "Biñan" alone is usually the city.
    Barangay("PH0403403001", "Biñan", ("Biñan Poblacion",), ("Biñan",)),
    Barangay("PH0403403002", "Bungahan"),
    Barangay(
        "PH0403403003",
        "Santo Tomas",
        ("Sto. Tomas", "Calabuso"),
        cue_only=("Santo Tomas", "Sto. Tomas"),
    ),
    Barangay("PH0403403004", "Canlalay"),
    Barangay("PH0403403005", "Casile"),
    Barangay("PH0403403006", "De La Paz", ("Dela Paz",), cue_only=("De La Paz", "Dela Paz")),
    Barangay("PH0403403007", "Ganado", cue_only=("Ganado",)),
    Barangay("PH0403403008", "San Francisco", ("Halang",), ("Halang",), ("San Francisco",)),
    Barangay("PH0403403009", "Langkiwa"),
    Barangay("PH0403403011", "Loma", prefix_only=("Loma",)),
    Barangay("PH0403403012", "Malaban"),
    Barangay("PH0403403013", "Malamig", prefix_only=("Malamig",)),
    Barangay("PH0403403014", "Mamplasan", ("Mampalasan",)),
    Barangay("PH0403403015", "Platero", cue_only=("Platero",)),
    Barangay("PH0403403016", "Poblacion"),
    Barangay("PH0403403018", "Santo Niño", ("Sto. Niño",), cue_only=("Santo Niño", "Sto. Niño")),
    Barangay("PH0403403019", "San Antonio", cue_only=("San Antonio",)),
    Barangay("PH0403403020", "San Jose", cue_only=("San Jose",)),
    Barangay("PH0403403021", "San Vicente", cue_only=("San Vicente",)),
    Barangay("PH0403403022", "Soro-soro"),
    Barangay(
        "PH0403403023",
        "Santo Domingo",
        ("Sto. Domingo",),
        cue_only=("Santo Domingo", "Sto. Domingo"),
    ),
    Barangay("PH0403403024", "Timbao"),
    Barangay("PH0403403025", "Tubigan", prefix_only=("Tubigan",)),
    Barangay("PH0403403026", "Zapote"),
)
BY_PCODE = {b.pcode: b for b in BARANGAYS}
BY_NAME = {b.name: b for b in BARANGAYS}

# Words that introduce a barangay name in Filipino reports ("Brgy. Malamig", "barangay Loma").
_PREFIXES = ("barangay", "baranggay", "brgy", "bgy", "brg")
# Words that mark a place in Taglish or English ("sa Dela Paz", "taga San Jose", "galing Ganado").
_CUES = (
    *_PREFIXES,
    *("sa", "taga", "tiga", "dito", "diyan", "dyan", "doon", "mula", "galing", "ng"),
    *("papunta", "papuntang", "hanggang", "at", "in", "from", "to", "near"),
)
# A saint's or family name followed by these is a building, not the barangay.
_INSTITUTIONS = (
    *("chapel", "kapilya", "church", "simbahan", "parish", "parokya", "school", "elementary"),
    *("high", "college", "hospital", "street", "st", "avenue", "ave", "subdivision", "subd"),
)
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


PLACES_FILE = Path(__file__).parent / "data" / "binan_places.json"
# Voice Transcribe's keyword limits. The docs give none; the API answers HTTP 400 "Too many ASR
# keywords: received 501, maximum is 500" and "ASR keyword 67 exceeds the maximum length of 30
# characters" (tested 2026-11-02).
KEYWORD_LIMIT = 500
KEYWORD_MAX_CHARS = 30
# The OpenStreetMap names in keyword order when not all of them fit: the subdivisions, sitios and
# puroks people give as an address, then landmarks, then streets by their length inside Biñan.
PLACE_KIND_ORDER = ("area", "landmark", "street")
# How many of those names the bot sends after the barangay names (0: none; None: as many as fit,
# 471 of the 522 names of at most 30 characters today). Chosen on voice-notes-8
# (data/eval/results.json, 2026-11-02): the full list did not make WER (19.1% vs 20.3% with the
# barangays only) or false insertions (0 vs 0) worse.
# The notes name no Biñan street, so this measured the cost of the long list, not its benefit.
BOT_PLACE_KEYWORDS: int | None = None


@cache
def place_keywords() -> tuple[str, ...]:
    """Biñan's OpenStreetMap names that fit a keyword, in PLACE_KIND_ORDER (no barangay name)."""
    places = json.loads(PLACES_FILE.read_text(encoding="utf-8"))["places"]
    order = {kind: i for i, kind in enumerate(PLACE_KIND_ORDER)}
    ranked = sorted(  # stable: each kind keeps the file's own ranking
        (p for p in places if len(p["name"]) <= KEYWORD_MAX_CHARS), key=lambda p: order[p["kind"]]
    )
    taken = {normalize(n) for b in BARANGAYS for n in b.names}
    names = []
    for place in ranked:
        if (folded := normalize(place["name"])) not in taken:
            taken.add(folded)
            names.append(place["name"])
    return tuple(names)


def transcription_keywords(places: int | None = BOT_PLACE_KEYWORDS) -> list[str]:
    """Names to bias Voice Transcribe toward, with their usual spelling (accents kept): every
    barangay name, then up to `places` OpenStreetMap names (None: as many as the API takes)."""
    keywords: list[str] = []
    for barangay in BARANGAYS:
        for name in barangay.names:
            if not name.startswith(("Sto.", "Sta.")) and name not in keywords:
                keywords.append(name)
    room = KEYWORD_LIMIT - len(keywords)
    return keywords + list(place_keywords()[: room if places is None else min(places, room)])


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


def _any(words: tuple[str, ...]) -> str:
    return "(?:" + "|".join(words) + ")"


@cache
def _patterns(require_cues: bool) -> list[tuple[re.Pattern[str], Barangay]]:
    prefix = _any(_PREFIXES) + " "
    cue = rf"\b{_any(_CUES)} "
    not_institution = rf"(?! {_any(_INSTITUTIONS)}\b)"
    patterns = []
    # Longest names first so "san francisco" wins over a shorter overlapping alias.
    for barangay, name in sorted(
        ((b, normalize(n)) for b in BARANGAYS for n in b.names), key=lambda x: -len(x[1])
    ):
        lead, tail = r"\b", ""
        if name in {normalize(n) for n in barangay.prefix_only}:
            lead = rf"\b{prefix}"
        elif require_cues and name in {normalize(n) for n in barangay.cue_only}:
            lead, tail = cue, not_institution
        patterns.append((re.compile(rf"{lead}({re.escape(name)})\b{tail}"), barangay))
    return patterns


def find_in_text(text: str | None, *, require_cues: bool = True) -> TextMatch:
    """Find barangay names in a text or transcript.

    Exact matches (accent- and abbreviation-insensitive) are tried first. Only if there are
    none, the one to three words after "barangay"/"brgy" are fuzzy-matched, which catches
    transcription slips like "Mamplasan" -> "Mamplason" without matching ordinary words.
    Pass require_cues=False for a value that is already just a place name (the LLM's
    barangay_mentioned), so surname-like names need no "sa"/"taga" in front.
    """
    if not text:
        return TextMatch((), ())
    norm = normalize(text)
    hits: list[tuple[int, Barangay, str]] = []
    taken: list[range] = []
    for pattern, barangay in _patterns(require_cues):
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

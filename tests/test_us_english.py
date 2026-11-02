"""US English guard: no British spelling from a short, data-driven list anywhere in the repo's text.

Ronnie, 2026-11-02: "use U.S. English, not British English". Every page, bot reply, form label,
help text, README line, comment and identifier we write follows Merriam-Webster / AP. A word is
added to BRITISH (British form -> US form) when it slips in once; ALLOWED_TOKENS and ALLOWED_FILES
list the few places where the British form is someone else's name or stored text we must not
change (quoted official text keeps its source's spelling too: allow it here, with the source).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Word starts whose British -ise forms (-ise, -ised, -ises, -ising, -isation) are US -ize forms.
STEMS_ISE = (
    *("recogn", "organ", "summar", "normal", "serial", "local", "harmon", "capital", "priorit"),
    *("optim", "minim", "maxim", "custom", "standard", "visual", "util", "final", "categor"),
    *("author", "initial", "apolog", "real", "emphas"),
)
# British form (a word start; any ending matches) -> US form, for the failure message.
BRITISH = {
    "colour": "color",
    "centre": "center",
    "labelled": "labeled",
    "labelling": "labeling",
    "labeller": "labeler",
    "cancelled": "canceled",
    "cancelling": "canceling",
    "modelled": "modeled",
    "modelling": "modeling",
    "travelled": "traveled",
    "grey": "gray",
    "licence": "license",
    "neighbour": "neighbor",
    "behaviour": "behavior",
    "favour": "favor",
    "honour": "honor",
    "labour": "labor",
    "acknowledgement": "acknowledgment",
    "judgement": "judgment",
    "catalogue": "catalog",
    "programme": "program",
    "metre": "meter",
    "kilometre": "kilometer",
    "litre": "liter",
    "fibre": "fiber",
    "defence": "defense",
    "towards": "toward",
    "afterwards": "afterward",
    "whilst": "while",
    "amongst": "among",
    "storey": "story",
    "analysed": "analyzed",  # not "analyse": "analyses" is the US plural of analysis
    "analysing": "analyzing",
    **{
        f"{stem}is{end}": f"{stem}iz{end}"
        for stem in STEMS_ISE
        for end in ("e", "ed", "es", "ing", "ation")
    },
}
# Exact tokens that keep a British form, and why.
ALLOWED_TOKENS = {
    "aria-labelledby": "the ARIA attribute's name (WAI-ARIA spec)",
    "CancelledError": "asyncio's exception name",
    ".cancelled()": "asyncio's Task and Future method",
    "DamageJudgement": "the JSON schema name inside cached Spark photo requests",
}
# Files whose British text is stored data we must not change.
ALLOWED_FILES = {
    "data/demo/scenario.json": "Muse Image prompts; the prompt's hash names the demo photo",
    "data/eval/vision_scenes.json": "Muse Image prompts; the prompt's hash names the eval photo",
    "tests/test_us_english.py": "this list",
}
SCANNED = ("*.md", "*.py", "*.html", "*.js", "*.css", "*.json", "*.geojson", "*.toml", "*.sh")
SKIPPED_DIRS = {".git", ".venv", "var", ".dev", "vendor", "__pycache__", ".pytest_cache"}
SKIPPED_DIRS |= {".ruff_cache", "node_modules"}

_WORDS = "|".join(sorted(map(re.escape, BRITISH), key=len, reverse=True))
_PATTERN = re.compile(rf"(?<![A-Za-z])(?:un)?({_WORDS})", re.IGNORECASE)


def _files():
    for pattern in SCANNED:
        for path in ROOT.rglob(pattern):
            relative = path.relative_to(ROOT)
            if SKIPPED_DIRS.isdisjoint(relative.parts[:-1]) and str(relative) not in ALLOWED_FILES:
                yield relative


def british_spellings(text: str) -> list[str]:
    """The British forms in `text`, after removing the allowed tokens."""
    for token in ALLOWED_TOKENS:
        text = text.replace(token, " ")
    return [match.group(1) for match in _PATTERN.finditer(text)]


def test_the_list_catches_british_forms_and_spares_us_forms_and_allowed_tokens():
    assert british_spellings("the barangay centre, Colours and LABELLED rows") == [
        "centre",
        "Colour",
        "LABELLED",
    ]
    found = british_spellings("unlabelled, unrecognised, _COLOURS")
    assert found == ["labelled", "recognised", "COLOUR"]
    assert british_spellings("center, color, labeled, canceled, gray, toward, recognize") == []
    assert british_spellings('aria-labelledby="t" asyncio.CancelledError task.cancelled()') == []
    assert british_spellings("raise, otherwise, promise, noise, rise, hours, four") == []


def test_no_british_spelling_in_the_repository():
    files = list(_files())
    assert any(f.parts[0] == "app" and f.suffix == ".html" for f in files)  # templates scanned
    assert Path("README.md") in files
    found = [
        f"{relative}:{number}: {word} -> {BRITISH[word.lower()]}"
        for relative in files
        for number, line in enumerate((ROOT / relative).read_text("utf-8").splitlines(), 1)
        for word in british_spellings(line)
    ]
    assert found == [], "\n".join(found)

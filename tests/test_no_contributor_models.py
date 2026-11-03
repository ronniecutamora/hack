"""No contributor tier, ever (AGENTS.md, Ronnie 2026-11-02): contributor-tier models and
contributor mode let Meta train on what we send, so no model id ending in the tier's suffix and
no contributor-mode setting or flag may appear in the code, the deploy, the tests or the config."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("app", "scripts", "deploy", "tests", "docs")
SCANNED_FILES = ("pyproject.toml", ".env.example", "README.md", "CREDITS.md")
SUFFIXES = {
    ".py",
    ".toml",
    ".json",
    ".sh",
    ".html",
    ".js",
    ".yml",
    ".yaml",
    ".cfg",
    ".ini",
    ".env",
    ".md",
}
SKIPPED = {"vendor", "__pycache__"}
# A model id with the tier's suffix ("muse-spark-1.3-contributor"), and the mode as a setting,
# an env var or a CLI flag ("contributor_mode", "CONTRIBUTOR_MODE", "--contributor-mode on").
FORBIDDEN = re.compile(r"[\w.]-contributor\b|contributor[_-]mode(?!l)", re.IGNORECASE)


def _files():
    for name in SCANNED_DIRS:
        for path in sorted((ROOT / name).rglob("*")):
            if path.is_file() and path.suffix in SUFFIXES and SKIPPED.isdisjoint(path.parts):
                yield path
    yield from (ROOT / name for name in SCANNED_FILES if (ROOT / name).is_file())


def found(text: str) -> list[str]:
    return [m.group(0) for m in FORBIDDEN.finditer(text)]


def test_the_pattern_catches_the_tier_and_the_mode_and_spares_other_words():
    model = "muse-spark-1.3" + "-contributor"
    assert len(found(f'model="{model}"')) == 1
    assert len(found("CONTRIBUTOR" + "_MODE=on; --contributor" + "-mode on")) == 2
    assert len(found("contributor" + "_modeEnabled")) == 1
    assert found("© OpenStreetMap contributors; each Contributor grants") == []
    assert found("tests/test_no_contributor_models.py") == []


def test_no_contributor_model_or_mode_anywhere():
    me = Path(__file__).resolve()
    assert ROOT / "docs/developer.md" in list(_files())
    hits = [
        f"{path.relative_to(ROOT)}:{n}: {hit}"
        for path in _files()
        if path.resolve() != me
        for n, line in enumerate(path.read_text("utf-8", errors="ignore").splitlines(), 1)
        for hit in found(line)
    ]
    assert hits == [], "\n".join(hits)

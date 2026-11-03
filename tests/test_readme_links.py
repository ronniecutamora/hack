"""The short judge guide and its moved reference material stay connected."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CREDITS.md",
    ROOT / "docs/how-it-works.md",
    ROOT / "docs/evaluation.md",
    ROOT / "docs/developer.md",
)


def anchors(path: Path) -> set[str]:
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^#{1,6} ", line):
            title = re.sub(r"^#+ +", "", line).lower().replace("`", "")
            found.add(re.sub(r"[^\w\- ]", "", title).strip().replace(" ", "-"))
    return found


def test_local_markdown_links_and_section_anchors_exist():
    broken = []
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^\s)]+)\)", text):
            if target.startswith(("https:", "http:", "mailto:")):
                continue
            name, _, anchor = target.partition("#")
            destination = (document.parent / name).resolve() if name else document
            if not destination.exists() or (
                anchor and destination.suffix == ".md" and anchor not in anchors(destination)
            ):
                broken.append(f"{document.relative_to(ROOT)}: {target}")
    assert broken == []


def test_judge_section_defers_official_terms_to_the_official_names_table():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    judge_section, official = text.split("## Official names", 1)
    assert "## More details" in official
    assert not re.search(r"RDANA|PCIC|DROMIC|DSWD|kappa|CDRRMO|LGU", judge_section, re.I)

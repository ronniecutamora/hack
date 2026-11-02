"""Build the dashboard's icon sprite from Google's Material Symbols (Rounded, 24 px, Apache-2.0).

    uv run python scripts/build_icons.py

Downloads each symbol's SVG once from the google/material-design-icons repository and writes
app/static/vendor/material-symbols/icons.svg, one <symbol> per icon (the filled variant as
"<name>-fill"). The page references them with <use href="…/icons.svg#name">, so only the icons
listed here ship: a few kilobytes instead of a 3 MB icon font, with no ligature text flashing
while a font loads. To add an icon, add its name below and run this again.
"""

import re
import urllib.request
from pathlib import Path

SOURCE = (
    "https://raw.githubusercontent.com/google/material-design-icons/master/"
    "symbols/web/{name}/materialsymbolsrounded/{name}{variant}_24px.svg"
)
# Navigation destinations: outlined, and filled for the current page (Material 3 nav icons).
NAV_ICONS = (
    "home",
    "fact_check",
    "flood",
    "description",
    "more_horiz",
    "forum",
    "map",
    "house",
    "query_stats",
    "help",
)
# Everything else: outlined only.
ICONS = (
    "account_circle",
    "arrow_back",
    "check",
    "check_circle",
    "close",
    "download",
    "error",
    "filter_alt_off",
    "info",
    "logout",
    "print",
    "refresh",
    "schedule",
    "task_alt",
    "warning",
)
OUT = Path(__file__).resolve().parent.parent / "app/static/vendor/material-symbols/icons.svg"


def path_data(name: str, variant: str = "") -> str:
    with urllib.request.urlopen(SOURCE.format(name=name, variant=variant), timeout=30) as reply:
        svg = reply.read().decode()
    paths = re.findall(r'<path d="([^"]+)"', svg)
    if not paths:
        raise SystemExit(f"{name}{variant}: no path in the downloaded SVG")
    return "".join(f'<path d="{d}"/>' for d in paths)


def main() -> None:
    symbols = []
    for name in sorted(NAV_ICONS + ICONS):
        symbols.append(f'<symbol id="{name}" viewBox="0 -960 960 960">{path_data(name)}</symbol>')
        if name in NAV_ICONS:
            filled = path_data(name, "_fill1")
            symbols.append(f'<symbol id="{name}-fill" viewBox="0 -960 960 960">{filled}</symbol>')
    header = (
        "<!-- Material Symbols Rounded (Google, Apache-2.0, see LICENSE), built by "
        "scripts/build_icons.py -->"
    )
    body = "\n".join(symbols)
    OUT.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">\n{header}\n{body}\n</svg>\n')
    print(f"{OUT}: {len(symbols)} symbols, {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()

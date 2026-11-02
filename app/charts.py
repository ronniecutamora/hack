"""Accessible inline SVG charts, drawn on the server: no JavaScript, no chart library.

Each drawing function returns one ``<svg>`` as Markup with role="img", a ``<title>`` naming it
and a ``<desc>`` summing it up, and a viewBox 320 units wide, so it scales to its box (the CSS
sets width 100%). Every mark carries its own ``<title>``, the browser's hover tooltip, on a hit
area larger than the mark. Colors are CSS classes, never hex values: ``chart__fill--<role>``
paints an SVG mark and an HTML legend swatch alike, and maps to a Material 3 color role in
the "Accuracy charts" block of app/static/css/app.css, so light and dark themes both work.

A chart repeats numbers, it never holds the only copy: each one ships with an HTML table of the
same numbers (``Chart.tables``, or the page's own table). Text is always escaped here.
"""

import math
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from markupsafe import Markup, escape

WIDTH = 320  # viewBox units; at phone width one unit is about one CSS pixel
CHAR = 6.6  # average advance of a 12-unit Roboto character, to fit and wrap labels
LINE = 16  # one line of 12-unit label text
BAR = 12  # bar thickness (the mark spec caps bars at 24)
GAP = 2  # surface gap between touching marks
ROW_GAP = 12
RADIUS = 4  # rounded data end; the baseline end stays square
VALUE_ROOM = 72  # right of the plot: the value label and a "weak" chip
CHIP_W, CHIP_H = 34, 15
HIT = 24  # minimum hover target height
MARGIN = 10  # left of a bar chart's baseline: room for half of the first tick label


@dataclass(frozen=True)
class Bar:
    value: float | None  # None: not scored, said in words instead of drawn
    text: str  # the label at the bar's end
    fill: str = "primary"  # chart__fill--<fill>
    weak: bool = False


@dataclass(frozen=True)
class BarRow:
    label: str
    bars: list[Bar]
    tip: str  # the hover text for the whole row: every bar's value, in words


@dataclass(frozen=True)
class Segment:
    count: int
    fill: str
    tip: str


@dataclass(frozen=True)
class StackRow:
    label: str
    segments: list[Segment]


@dataclass(frozen=True)
class Swatch:
    fill: str
    label: str


@dataclass(frozen=True)
class NumberTable:
    caption: str
    head: list[str]
    rows: list[list[str]]  # the first cell of each row is its header


@dataclass(frozen=True)
class Panel:
    heading: str
    svg: Markup
    detail: str | None = None
    weak: bool = False


@dataclass(frozen=True)
class Chart:
    id: str
    title: str
    description: str
    panels: list[Panel]
    legend: list[Swatch] = field(default_factory=list)
    # The numbers as a table; empty when another table on the page holds them (``note`` says which).
    tables: list[NumberTable] = field(default_factory=list)
    note: str = "The numbers are in the table below."


def _n(value: float) -> str:
    return f"{round(value, 2):g}"


def _text(x: float, y: float, text: str, cls: str = "", anchor: str = "start") -> str:
    klass = f' class="{cls}"' if cls else ""
    anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return f'<text x="{_n(x)}" y="{_n(y)}"{klass}{anchor_attr}>{escape(text)}</text>'


def _bar_path(x: float, y: float, w: float, h: float, cls: str, rounded: bool = True) -> str:
    """A bar from its baseline at x: square at the baseline, a 4-unit round at the data end."""
    r = min(RADIUS, w, h / 2) if rounded else 0
    if r <= 0:
        return f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" class="{cls}"/>'
    d = (
        f"M{_n(x)} {_n(y)}h{_n(w - r)}a{_n(r)} {_n(r)} 0 0 1 {_n(r)} {_n(r)}v{_n(h - 2 * r)}"
        f"a{_n(r)} {_n(r)} 0 0 1 {_n(-r)} {_n(r)}h{_n(r - w)}z"
    )
    return f'<path d="{d}" class="{cls}"/>'


def _hit(x: float, y: float, w: float, h: float, tip: str) -> str:
    """A transparent hover target with the mark's tooltip."""
    return (
        f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" class="chart__hit">'
        f"<title>{escape(tip)}</title></rect>"
    )


def _wrap(label: str, width: float = WIDTH) -> list[str]:
    return textwrap.wrap(label, max(8, int(width / CHAR))) or [""]


def _svg(chart_id: str, title: str, desc: str, height: float, body: list[str]) -> Markup:
    return Markup(
        f'<svg class="chart__svg" role="img" viewBox="0 0 {WIDTH} {_n(math.ceil(height))}" '
        f'width="100%" aria-labelledby="{chart_id}-t {chart_id}-d" focusable="false">'
        f'<title id="{chart_id}-t">{escape(title)}</title>'
        f'<desc id="{chart_id}-d">{escape(desc)}</desc>{"".join(body)}</svg>'
    )


def bar_chart(
    chart_id: str,
    title: str,
    desc: str,
    rows: Sequence[BarRow],
    *,
    domain: float = 1.0,
    ticks: Sequence[float] = (0, 0.2, 0.4, 0.6, 0.8, 1.0),
    tick_text: Callable[[float], str] = _n,
    threshold: float | None = None,
    threshold_text: str = "",
) -> Markup:
    """Horizontal bars on one shared 0..domain axis; each row's label sits on its own line above
    its bars so long labels wrap instead of squeezing the plot. A ``threshold`` draws a rule
    (e.g. the weak-score line) with its words under the axis."""
    inner = WIDTH - MARGIN
    plot = inner - VALUE_ROOM

    def x(value: float) -> float:
        return max(0.0, min(value, domain)) / domain * plot

    marks: list[str] = []
    y = 0.0
    for row in rows:
        top = y
        for line in _wrap(row.label, inner):
            marks.append(_text(0, y + 12, line, "chart__label chart__halo"))
            y += LINE
        y += GAP
        for bar in row.bars:
            if bar.value is None:
                marks.append(_text(0, y + BAR - 2, bar.text, "chart__muted chart__halo"))
            else:
                w = x(bar.value)
                if w > 0:
                    marks.append(_bar_path(0, y, w, BAR, f"chart__fill--{bar.fill}"))
                end = min(w + 4, inner - len(bar.text) * CHAR - (CHIP_W + 4 if bar.weak else 0))
                marks.append(_text(end, y + BAR - 2, bar.text, "chart__value chart__halo"))
                if bar.weak:
                    chip_x = end + len(bar.text) * CHAR + 4
                    marks.append(
                        f'<rect x="{_n(chip_x)}" y="{_n(y - 1.5)}" width="{CHIP_W}" '
                        f'height="{CHIP_H}" rx="7.5" class="chart__weak-chip"/>'
                        + _text(chip_x + CHIP_W / 2, y + BAR - 2.5, "weak", "chart__weak", "middle")
                    )
            y += BAR + GAP
        y += ROW_GAP - GAP
        marks.append(_hit(0, top, inner, max(y - top, HIT), row.tip))
    bottom = y - ROW_GAP / 2
    grid = [
        f'<line x1="{_n(x(t))}" y1="0" x2="{_n(x(t))}" y2="{_n(bottom)}" '
        f'class="{"chart__axis" if t == 0 else "chart__grid"}"/>'
        for t in ticks
    ]
    axis = [_text(x(t), bottom + 14, tick_text(t), "chart__tick", "middle") for t in ticks]
    height = bottom + 18
    if threshold is not None:
        tx = x(threshold)
        grid.append(
            f'<line x1="{_n(tx)}" y1="0" x2="{_n(tx)}" y2="{_n(bottom)}" class="chart__threshold"/>'
        )
        axis.append(_text(tx, bottom + 30, threshold_text, "chart__tick chart__strong", "middle"))
        height = bottom + 34
    # The grid goes first so the bars and their labels sit on top; the first tick label hangs
    # half left of the baseline, so the drawing is shifted in by a margin.
    body = [f'<g transform="translate({MARGIN} 0)">{"".join(grid + marks + axis)}</g>']
    return _svg(chart_id, title, desc, height, body)


def matrix_chart(
    chart_id: str,
    title: str,
    desc: str,
    levels: Sequence[tuple[str, str]],
    counts: dict[tuple[str, str], int],
    *,
    row_title: str,
    col_title: str,
    tip: Callable[[str, str, int], str],
) -> Markup:
    """A confusion matrix: rows are the reference label, columns the model's answer. The
    diagonal (agreement) is shaded in the primary hue, the rest (disagreement) in the error
    hue, darker for a larger count; every non-zero cell prints its count."""
    k = len(levels)
    left, top = 80.0, 40.0
    cell = min(64.0, (WIDTH - left) / k)
    biggest = max(counts.values(), default=0) or 1
    parts = [
        _text(left + k * cell / 2, 12, col_title, "chart__label chart__strong", "middle"),
        _text(left - 8, top - 10, row_title, "chart__label chart__strong", "end"),
    ]
    for j, (_, label) in enumerate(levels):
        parts.append(_text(left + (j + 0.5) * cell, top - 10, label, "chart__tick", "middle"))
    for i, (row, row_label) in enumerate(levels):
        cy = top + i * cell
        parts.append(_text(left - 8, cy + cell / 2 + 4, row_label, "chart__tick", "end"))
        for j, (col, _) in enumerate(levels):
            count = counts.get((row, col), 0)
            cx = left + j * cell
            # Four steps of one hue: the two light steps take dark ink, the two strong ones the
            # surface color, so every count clears 4.5:1 in both themes.
            step = min(4, math.ceil(count / biggest * 4)) if count else 0
            kind = "agree" if row == col else "disagree"
            fill = f"chart__fill--{kind}-{step}" if step else "chart__fill--empty"
            parts.append(
                f'<rect x="{_n(cx + GAP / 2)}" y="{_n(cy + GAP / 2)}" width="{_n(cell - GAP)}" '
                f'height="{_n(cell - GAP)}" rx="{RADIUS}" class="{fill}"/>'
            )
            if count:
                ink = "chart__count chart__ink--strong" if step >= 3 else "chart__count"
                parts.append(_text(cx + cell / 2, cy + cell / 2 + 5, str(count), ink, "middle"))
            parts.append(_hit(cx, cy, cell, cell, tip(row_label, levels[j][1], count)))
    return _svg(chart_id, title, desc, top + k * cell + 2, parts)


def stack_chart(chart_id: str, title: str, desc: str, rows: Sequence[StackRow]) -> Markup:
    """One horizontal stacked bar per row, on a shared count scale, a 2-unit surface gap between
    segments; a count is printed inside a segment only when it fits, and the row total at the
    end. The legend and the table carry any count that does not fit."""
    plot = WIDTH - 40
    scale = max((sum(s.count for s in r.segments) for r in rows), default=0) or 1
    thick = 20
    parts: list[str] = []
    y = 0.0
    for row in rows:
        for line in _wrap(row.label):
            parts.append(_text(0, y + 12, line, "chart__label"))
            y += LINE
        y += GAP
        shown = [s for s in row.segments if s.count > 0]
        x = 0.0
        for index, seg in enumerate(shown):
            w = seg.count / scale * plot
            last = index == len(shown) - 1
            drawn = w if last else max(w - GAP, 0.5)
            parts.append(_bar_path(x, y, drawn, thick, f"chart__fill--{seg.fill}", rounded=last))
            label = str(seg.count)
            if len(label) * CHAR + 8 <= drawn:
                parts.append(
                    _text(
                        x + drawn / 2,
                        y + 14,
                        label,
                        f"chart__count chart__ink--{seg.fill}",
                        "middle",
                    )  # fmt: skip
                )
            parts.append(_hit(x, y - 2, w, thick + 4, seg.tip))
            x += w
        total = sum(s.count for s in row.segments)
        parts.append(_text(x + 6, y + 14, str(total), "chart__value"))
        y += thick + ROW_GAP
    return _svg(chart_id, title, desc, y - ROW_GAP + 2, parts)

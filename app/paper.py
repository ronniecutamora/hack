"""The printout of the RDANA form, laid out as Annex I prints it (app/templates/_paper.html).

app.rdana_form says where each field sits on the paper (PAPER_BOXES) and how a field prints
where its control alone does not (PAPER_FIELDS). This module joins those with a version's field
views (app.fillable.view) into the boxes, rows and items the template draws, so the template
names no field. Values are the version's: the officer's entry, else the generated value.
"""

import re
from typing import Any

from app.fillable import CHOICES
from app.rdana_form import (
    ANNEX_PAGES,
    BLANK,
    FORM_ANNEX,
    FORM_SUBTITLE,
    FORM_TITLE,
    NOTE,
    OTHERS,
    PAPER_BOXES,
    PAPER_FIELDS,
    SECTIONS,
    Paper,
    paper_rows,
)

View = dict[str, Any]
STAMP_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ,.:-·")


class Stamp(str):
    """The printout's status line (app.drafts.print_stamp), also as a CSS string for the page
    margin box that prints it: every character outside a small safe set is a CSS escape, so
    no typed name can end the string or the style element."""

    @property
    def css(self) -> str:
        return "".join(c if c in STAMP_SAFE else f"\\{ord(c):06x}" for c in self)


AMOUNT = re.compile(r"^\s*(?P<name>[^:]+?)\s*:\s*(?P<amount>.+?)\s*$")


def pieces(text: str, values: list[str] | None = None) -> list[dict[str, Any]]:
    """Printed text as words and ruled blanks ("___"); `values` fill the blanks in order."""
    values = list(values or [])
    out: list[dict[str, Any]] = []
    for i, part in enumerate(text.split(BLANK)):
        if i:
            out.append({"blank": True, "value": values.pop(0) if values else ""})
        if part.strip():
            out.append({"blank": False, "text": part.strip()})
    return out


def amounts(value: str | None, options: list[str]) -> tuple[dict[str, str], list[str]]:
    """An "Age 0-2: 4; Age 3-5: 6" or "Generators: 2; Diesel: 40 L" value as each option's
    amount (an option is named by its start), and the parts that name no single option."""
    found: dict[str, str] = {}
    rest: list[str] = []
    for part in re.split(r"[;\n]", value or ""):
        if not part.strip():
            continue
        match = AMOUNT.match(part)
        name = match["name"].casefold() if match else ""
        named = [o for o in options if name and o.casefold().startswith(name)]
        if match and len(named) == 1 and named[0] not in found:
            found[named[0]] = match["amount"]
        else:
            rest.append(part.strip())
    return found, rest


def _grid(options: list[dict[str, Any]], cols: int | tuple[int, ...] | None) -> dict[str, Any]:
    """Option boxes as the paper sets them: after the label (cols None), or under it in
    columns that line up from row to row, n to a row or the given number in each row (the
    last box of a shorter row runs from its column to the row's end)."""
    if cols is None:
        return {"inline": True, "cols": len(options) or 1, "options": options}
    lengths = [cols] * -(-len(options) // cols) if isinstance(cols, int) else list(cols)
    width, laid, at = max(lengths), [], 0
    for length in lengths:
        row = options[at : at + length]
        short = len(row) < width
        laid += [o | {"col": i + 1, "end": short and i == len(row) - 1} for i, o in enumerate(row)]
        at += length
    laid += [o | {"col": 0, "end": False} for o in options[at:]]
    return {"inline": False, "cols": width, "options": laid}


def _style(view: View, paper: Paper) -> str:
    if paper.style:
        return paper.style
    if view["control"].kind == "long":
        return "area"
    return "boxes" if view["control"].kind in CHOICES else "line"


def field_item(view: View) -> dict[str, Any]:
    """One field as it prints: its number, the paper's label and how its value is drawn."""
    field = view["field"]
    paper = PAPER_FIELDS.get(field.key, Paper())
    style = _style(view, paper)
    label = field.paper_label or field.label
    printed = dict(OTHERS) | dict(paper.printed)
    found, rest = (
        amounts(view["value"], view["options"]) if style in ("amounts", "cells") else ({}, [])
    )
    options = [
        {
            "option": o,
            "checked": o in view["printed"] or o in found,
            "amount": found.get(o, ""),
            "pieces": pieces(printed.get(o, o), [found[o]] if o in found else None),
        }
        for o in view["options"]
    ]
    groups = [options[: paper.split], options[paper.split :]] if paper.split else [options]
    item: dict[str, Any] = {
        "kind": "field",
        "view": view,
        "style": style,
        "number": view["number"],
        "label": label,
        "sep": "" if label.endswith(("?", ":")) else paper.sep,
        "mark": paper.mark,
        "required": view["required"],
        "options": options,
        "groups": [_grid(g, paper.cols) for g in groups],
        # A value the boxes cannot show (free text over a choice): printed beside them.
        "text": view["value"] if view["value"] and options and not view["printed"] else None,
        "lines": paper.lines,
    }
    if style in ("amounts", "cells"):
        item["text"] = "; ".join(rest) or None
    if style == "table":
        filled = [line for line in (view["value"] or "").splitlines() if line.strip()]
        item["head"] = paper.head
        item["width"] = sum(max(len(sub), 1) for _, sub in paper.head)
        item["rows"] = filled + [""] * max(paper.rows - len(filled), 0)
    if style == "matrix":
        item["matrix"] = [
            {
                "text": o["option"] + ":",
                "answers": [
                    {"text": a, "checked": o["checked"] and a == paper.answers[0]}
                    for a in paper.answers
                ],
            }
            for o in options
        ]
    return item


def rdana_paper(by_key: dict[str, View]) -> dict[str, Any]:
    """The RDANA printout: Annex I's title, then each section's boxes in the paper's layout,
    with the page breaks the paper has (before the first field of each later page)."""
    page_starts = {key for key, _page in ANNEX_PAGES[1:]}
    sections = []
    for section in SECTIONS:
        headings = {f.key: f for f in section.fields if f.heading}
        boxes = []
        for box in PAPER_BOXES[section.number]:
            cells = paper_rows(box)
            items = [
                [
                    [
                        {"kind": "note", "text": section.note}
                        if key == NOTE
                        else {
                            "kind": "heading",
                            "number": key,
                            "label": headings[key].label,
                        }
                        if key in headings
                        else field_item(by_key[key])
                        for key in row
                    ]
                    for row in cell
                ]
                for cell in cells
            ]
            keys = {key for cell in cells for row in cell for key in row}
            boxes.append({"cells": items, "page_break": bool(keys & page_starts)})
        sections.append(
            {
                "number": section.number,
                "title": section.title,
                "title_note": section.title_note,
                "boxes": boxes,
            }
        )
    return {
        "annex": FORM_ANNEX,
        "title": FORM_TITLE,
        "subtitle": FORM_SUBTITLE,
        "sections": sections,
    }

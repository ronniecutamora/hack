"""The "How accurate is the agent?" page, read from data/eval/results.json (app.eval_results).

One table per task and dataset, one column per model (Spark 1.3 first, so it reads side by side
with Spark Open), one row per measured score, each with a plain-language caption for a city
officer: what the number means and what it does not. Every number comes from the results file;
nothing here is typed in by hand. A missing, empty or invalid file is an empty state, not an error.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import charts
from app.charts import Bar, BarRow, Chart, NumberTable, Panel, Segment, StackRow, Swatch
from app.eval_results import FLAGS, Dataset, Entry, Results, load
from app.trace import ModelFamily, model_family

# Below this a score is shown as weak: kappa 0.6 is the usual floor of "substantial" agreement.
WEAK_BELOW = 0.6
MODEL_ORDER = ("muse-spark-1.3", "muse-spark-open")  # Spark 1.3 first, then the open weights
TASK_ORDER = ("photo_judge", "pair_judge", "screen")
PROVENANCE_ORDER = ("real", "synthetic")

TASKS = {
    "photo_judge": (
        "Photo judge",
        "The agent LLM looks at each photo and says how badly roofs are damaged and whether it "
        "sees floodwater, debris, a tarpaulin or a fallen tree; SAM 3.1 outlines roofs and "
        "floodwater on the photo. Scores compare them with labels a person set by hand.",
    ),
    "pair_judge": (
        "Duplicate check",
        "The agent LLM reads two reports' facts and says whether they describe the same "
        "situation. Only a confident 'same' merges them; anything less goes to the officer.",
    ),
    "screen": (
        "Not-a-report screen",
        "Before a message becomes a report, a free rule drops only greetings, tests, laughter, "
        "emoji and keyboard mash; when the agent finds nothing a report states in the rest, the "
        "agent LLM says whether it is a disaster report. Only a certain 'no' screens a message "
        "out. Scores compare that with labels a person set by hand.",
    ),
}
PROVENANCE = {
    "real": "Real photos, but not from Biñan: past Philippine typhoons, mostly severe damage.",
    "synthetic": "Synthetic data made for testing. A score here is not a score on residents' "
    "own reports, which will be messier.",
}
NOT_MEANING = {
    "photo_judge": "These scores say how often the agent reads a photo the way a person does. "
    "They are not a count of damaged houses: SAM's roof and floodwater outlines are evidence "
    "for the officer and the PCIC adjuster, not the loss figure.",
    "pair_judge": "A merge means two reports describe one situation, so their figures are "
    "counted once instead of added. The officer can split any merged report back out from its "
    "incident page, and its figures then count on their own again.",
    "screen": "A screened-out message is not deleted: the officer can see it and restore it, and "
    "the reporter is asked for the place and what happened. These are short synthetic messages; "
    "residents' real messages will be messier.",
}


@dataclass(frozen=True)
class Cell:
    value: str
    detail: str | None = None
    weak: bool = False


@dataclass(frozen=True)
class Row:
    label: str
    help: str
    cells: list[Cell | None]


@dataclass(frozen=True)
class Column:
    entry: Entry
    family: ModelFamily | None


@dataclass(frozen=True)
class Table:
    task: str
    title: str
    intro: str
    not_meaning: str
    dataset: Dataset
    provenance_note: str
    columns: list[Column]
    rows: list[Row]
    charts: list[Chart] = field(default_factory=list)  # drawn above the table, from its entries


@dataclass(frozen=True)
class VoiceSection:
    """Voice Transcribe on one set of voice notes: charts and a numbers table, no model columns
    (one transcription model, several request settings)."""

    entry: Entry
    title: str
    intro: str
    provenance_note: str
    not_meaning: str
    charts: list[Chart]


@dataclass
class AccuracyPage:
    tables: list[Table] = field(default_factory=list)
    voice: list[VoiceSection] = field(default_factory=list)
    problem: str | None = None  # why the file could not be read; None when it was fine or absent
    spend_usd: float = 0.0
    latest_date: str | None = None


def _scores(scores: dict[str, Any]) -> Cell:
    return Cell(
        f"{scores['f1']:.2f}",
        f"precision {scores['precision']:.2f}, recall {scores['recall']:.2f}: "
        f"{scores['tp']} found, {scores['fp']} false alarm{'s' if scores['fp'] != 1 else ''}, "
        f"{scores['fn']} missed",
        scores["f1"] < WEAK_BELOW,
    )


def _kappa(entry: Entry) -> Cell:
    roof = entry.metrics["roof_damage"]
    return Cell(
        f"{roof['kappa']:.2f}",
        f"{roof['agreement']} of {entry.n} photos given the same level as the hand label",
        roof["kappa"] < WEAK_BELOW,
    )


def _sam(concept: str) -> Callable[[Entry], Cell | None]:
    def cell(entry: Entry) -> Cell | None:
        sam = entry.metrics.get("sam")
        return _scores(sam[concept]) if sam else None

    return cell


def _flag(name: str) -> Callable[[Entry], Cell]:
    return lambda entry: _scores(entry.score("flags", name))


F1_HELP = "1.00 is perfect: it finds every case and raises no false alarm."
PHOTO_ROWS: list[tuple[str, str, Callable[[Entry], Cell | None]]] = [
    (
        "Roof damage level (κ)",
        "Agreement on none, partial or severe roof damage, corrected for lucky guesses: 1 is "
        "perfect, 0 is chance, above 0.6 is usually read as substantial.",
        _kappa,
    ),
    ("Floodwater", f"The agent LLM's judgment, which decides. {F1_HELP}", _flag("floodwater")),
    ("Debris", F1_HELP, _flag("debris")),
    ("Tarpaulin", F1_HELP, _flag("tarpaulin")),
    (
        "Fallen tree",
        "Not reliable: the dashboard asks the reporter or the officer to confirm every fallen "
        "tree before it counts.",
        _flag("fallen_tree"),
    ),
    (
        "SAM 3.1: roofs found",
        "Whether SAM outlines a roof where a person sees one. Its roof count is evidence, "
        "not the number of damaged houses.",
        _sam("roof"),
    ),
    (
        "SAM 3.1: floodwater found",
        "Whether SAM outlines floodwater where a person sees it. By the lake it also outlines "
        "Laguna de Bay and fish-pen water, so the agent LLM's floodwater judgment decides.",
        _sam("floodwater"),
    ),
]
PAIR_ROWS: list[tuple[str, str, Callable[[Entry], Cell | None]]] = [
    ("Same incident (F1)", F1_HELP, lambda e: _scores(e.metrics)),
    (
        "Merges that were right (precision)",
        "Of the pairs it merged, the share that truly were one situation. A wrong merge could "
        "hide a separate emergency, so this matters most.",
        lambda e: Cell(f"{e.metrics['precision']:.2f}", None, e.metrics["precision"] < WEAK_BELOW),
    ),
    (
        "Duplicates caught (recall)",
        "Of the true duplicates, the share it merged. A miss leaves two entries the officer "
        "can merge by hand.",
        lambda e: Cell(f"{e.metrics['recall']:.2f}", None, e.metrics["recall"] < WEAK_BELOW),
    ),
    (
        "Sent to the officer",
        "Pairs below the merge threshold ({threshold}) or judged unsure, left for a person.",
        lambda e: Cell(f"{e.metrics['review_rate']:.0%}", f"{e.metrics['reviews']} of {e.n}"),
    ),
]
SCREEN_ROWS: list[tuple[str, str, Callable[[Entry], Cell | None]]] = [
    (
        "Real reports wrongly screened out",
        "Real reports the screen dropped. A dropped report could hide an emergency, so the "
        "target is 0.",
        lambda e: Cell(
            str(e.metrics["wrongly_filtered"]),
            f"of {e.metrics['tp'] + e.metrics['fn']} real reports",
            e.metrics["wrongly_filtered"] > 0,
        ),
    ),
    (
        "Real reports kept (recall)",
        "Of the real reports, the share that stayed reports.",
        lambda e: Cell(f"{e.metrics['recall']:.2f}", None, e.metrics["recall"] < WEAK_BELOW),
    ),
    (
        "Kept messages that are reports (precision)",
        "Of the messages kept as reports, the share that truly are. Below 1.00, some noise "
        "still reaches the dashboard, where the officer can see it.",
        lambda e: Cell(f"{e.metrics['precision']:.2f}", None, e.metrics["precision"] < WEAK_BELOW),
    ),
    (
        "Non-reports screened out",
        "Of the messages that are not reports, how many the screen dropped, and how many of "
        "those the free rule dropped with no model call.",
        lambda e: Cell(
            f"{e.metrics['non_reports_filtered']} of {e.metrics['tn'] + e.metrics['fp']}",
            f"{e.metrics['rule_filtered']} by the free rule; "
            f"screening calls: {e.metrics['model_calls']}",
        ),
    ),
]
ROWS = {"photo_judge": PHOTO_ROWS, "pair_judge": PAIR_ROWS, "screen": SCREEN_ROWS}


def _model_key(entry: Entry) -> tuple[int, str]:
    order = MODEL_ORDER.index(entry.model) if entry.model in MODEL_ORDER else len(MODEL_ORDER)
    return order, entry.model


def _run_rows(entries: list[Entry]) -> list[Row]:
    """Where each column's numbers come from: n, date, code commit and spend."""
    return [
        Row(
            "Items scored",
            "Labeled items the model answered; failed calls are not scored.",
            [Cell(str(e.n), f"{e.errors} failed" if e.errors else None) for e in entries],
        ),
        Row(
            "Run",
            "The Philippine date of the run and the code commit that produced it.",
            [Cell(e.date, f"commit {e.commit[:12]}") for e in entries],
        ),
        Row(
            "Eval spend",
            "List-price cost of the recorded runs, from the eval call log.",
            [Cell(f"${e.spend_usd:.2f}", e.backend) for e in entries],
        ),
    ]


def build_tables(results: Results) -> list[Table]:
    groups: dict[tuple[str, str], list[Entry]] = {}
    for entry in results.entries:
        if entry.task in ROWS:  # a task this page has no table for yet is left out, not a 500
            groups.setdefault((entry.task, entry.dataset.id), []).append(entry)

    def table_key(key: tuple[str, str]) -> tuple[int, int, str]:
        task, dataset = key
        provenance = results.datasets[dataset].provenance
        return TASK_ORDER.index(task), PROVENANCE_ORDER.index(provenance), dataset

    tables = []
    for task, dataset_id in sorted(groups, key=table_key):
        entries = sorted(groups[(task, dataset_id)], key=_model_key)
        dataset = results.datasets[dataset_id]
        title, intro = TASKS[task]
        threshold = entries[0].metrics.get("threshold")
        captions = {"threshold": f"{threshold:.2f}" if threshold is not None else "set in config"}
        rows = [
            Row(label, help.format(**captions), [cell(e) for e in entries])
            for label, help, cell in ROWS[task]
        ]
        tables.append(
            Table(
                task=task,
                title=title,
                intro=intro,
                not_meaning=NOT_MEANING[task],
                dataset=dataset,
                provenance_note=PROVENANCE[dataset.provenance],
                columns=[Column(e, model_family(e.model_id)) for e in entries],
                rows=[*rows, *_run_rows(entries)],
                charts=CHARTS[task](dataset_id, entries),
            )
        )
    return tables


# ---------- Charts (app.charts draws them; these pick the numbers from the results file) ----------

ROOF_LEVELS = {"none": "None", "partial": "Partial", "severe": "Severe", "unknown": "Unknown"}
F1_TICKS = (0, 0.2, 0.4, 0.6, 0.8, 1.0)
WEAK_TEXT = f"weak below {WEAK_BELOW:g}"


def _name(entry: Entry) -> str:
    family = model_family(entry.model_id)
    return family.name if family else entry.model


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _roof_matrix(dataset_id: str, entries: list[Entry]) -> Chart | None:
    """AI vs hand label for roof damage: one confusion matrix per model, on shared levels."""
    scored = [
        (e, e.metrics["roof_damage"]) for e in entries if e.metrics["roof_damage"]["confusion"]
    ]
    if not scored:
        return None
    keys = {
        k for _, roof in scored for label, row in roof["confusion"].items() for k in (label, *row)
    }
    levels = [(k, ROOF_LEVELS[k]) for k in ROOF_LEVELS if k in keys]
    levels += [(k, k.replace("_", " ").capitalize()) for k in sorted(keys - set(ROOF_LEVELS))]
    chart_id = f"chart-roof-{dataset_id}"
    panels, tables = [], []
    for i, (entry, roof) in enumerate(scored):
        name = _name(entry)
        counts = {(r, c): n for r, row in roof["confusion"].items() for c, n in row.items()}
        total = sum(counts.values())
        same = sum(counts.get((k, k), 0) for k, _ in levels)
        svg = charts.matrix_chart(
            f"{chart_id}-{i}",
            f"{name}: roof damage level, hand label against the AI's answer",
            f"{same} of {_plural(total, 'photo')} given the same level as the hand label. "
            "Rows are the hand label, columns the AI's answer; the diagonal is agreement.",
            levels,
            counts,
            row_title="Hand label",
            col_title="AI said",
            tip=lambda row, col, n: f"Hand label {row}, AI said {col}: {_plural(n, 'photo')}",
        )
        panels.append(
            Panel(
                name,
                svg,
                f"{same} of {total} the same · κ {roof['kappa']:.2f}",
                roof["kappa"] < WEAK_BELOW,
            )  # fmt: skip
        )
        labels = [label for _, label in levels]
        tables.append(
            NumberTable(
                f"{name}: photos per hand label (rows) and AI answer (columns)",
                ["Hand label", *(f"AI: {label}" for label in labels), "Total"],
                [
                    [
                        label,
                        *(str(counts.get((r, c), 0)) for c, _ in levels),
                        str(sum(counts.get((r, c), 0) for c, _ in levels)),
                    ]
                    for r, label in levels
                ],  # fmt: skip
            )
        )
    return Chart(
        chart_id,
        "Roof damage: AI against the hand label",
        "Each square counts photos: the row is the level a person gave, the column the level the "
        "agent LLM gave. Blue squares on the diagonal agree; red squares disagree.",
        panels,
        legend=[Swatch("agree-4", "Same level"), Swatch("disagree-4", "Different level")],
        tables=tables,
    )


def _f1_row(label: str, scores: dict[str, Any] | None) -> BarRow:
    if scores is None:
        return BarRow(label, [Bar(None, "not scored")], f"{label}: not scored")
    f1 = scores["f1"]
    tip = (
        f"{label}: F1 {f1:.2f}, precision {scores['precision']:.2f}, recall "
        f"{scores['recall']:.2f} ({scores['tp']} found, {scores['fp']} false alarms, "
        f"{scores['fn']} missed)"
    )
    return BarRow(label, [Bar(f1, f"{f1:.2f}", weak=f1 < WEAK_BELOW)], tip)


def _flag_bars(dataset_id: str, entries: list[Entry]) -> Chart | None:
    """F1 per flag, one panel per model with the same rows in the same order."""
    names = [f for f in FLAGS if any(f in e.metrics["flags"] for e in entries)]
    names += sorted({f for e in entries for f in e.metrics["flags"]} - set(names))
    if not names:
        return None
    chart_id = f"chart-flags-{dataset_id}"
    panels = []
    for i, entry in enumerate(entries):
        name = _name(entry)
        rows = [_f1_row(f.replace("_", " ").capitalize(), entry.metrics["flags"].get(f))
                for f in names]  # fmt: skip
        svg = charts.bar_chart(
            f"{chart_id}-{i}", f"{name}: F1 per flag", f"F1 from 0 to 1 for {len(rows)} flags; "
            f"{sum(r.bars[0].weak for r in rows)} below {WEAK_BELOW:g}.", rows,
            ticks=F1_TICKS, threshold=WEAK_BELOW, threshold_text=WEAK_TEXT,
        )  # fmt: skip
        panels.append(Panel(name, svg))
    return Chart(
        chart_id,
        "Flags: F1 per model",
        f"How well the agent LLM spots each thing in a photo, 1.00 is perfect. The line marks "
        f"{WEAK_BELOW:g}: a bar left of it is weak.",
        panels,
    )


def _photo_charts(dataset_id: str, entries: list[Entry]) -> list[Chart]:
    return [c for c in (_roof_matrix(dataset_id, entries), _flag_bars(dataset_id, entries)) if c]


PAIR_BARS = (
    ("f1", "Same incident (F1)"),
    ("precision", "Merges that were right (precision)"),
    ("recall", "Duplicates caught (recall)"),
)


def _pair_charts(dataset_id: str, entries: list[Entry]) -> list[Chart]:
    chart_id = f"chart-dedupe-{dataset_id}"
    panels = []
    for i, entry in enumerate(entries):
        name = _name(entry)
        m = entry.metrics
        rows = [
            BarRow(label, [Bar(m[k], f"{m[k]:.2f}", weak=m[k] < WEAK_BELOW)],
                   f"{label}: {m[k]:.2f} ({m['tp']} merged right, {m['fp']} wrong merges, "
                   f"{m['fn']} duplicates missed)")
            for k, label in PAIR_BARS
        ]  # fmt: skip
        svg = charts.bar_chart(
            f"{chart_id}-{i}", f"{name}: duplicate check scores",
            "; ".join(f"{label} {m[k]:.2f}" for k, label in PAIR_BARS) + ".", rows,
            ticks=F1_TICKS, threshold=WEAK_BELOW, threshold_text=WEAK_TEXT,
        )  # fmt: skip
        panels.append(Panel(name, svg, f"{m['tp'] + m['fn']} true duplicates in {entry.n} pairs"))
    return [
        Chart(
            chart_id,
            "Duplicate check: scores per model",
            "1.00 is perfect. Precision matters most: a wrong merge could hide a separate "
            "emergency.",
            panels,
        )
    ]


SCREEN_LEGEND = [
    Swatch("success", "Real report kept"),
    Swatch("error", "Real report screened out"),
    Swatch("primary", "Non-report dropped by the free rule"),
    Swatch("primary-soft", "Non-report dropped by the agent LLM"),
    Swatch("warning", "Non-report kept (the officer sees it)"),
]


def _screen_outcomes(m: dict[str, Any]) -> list[int]:
    """The five outcomes in SCREEN_LEGEND order, from one screen entry's metrics."""
    by_model = max(m["non_reports_filtered"] - m["rule_filtered"], 0)
    return [m["tp"], m["wrongly_filtered"], m["rule_filtered"], by_model, m["fp"]]


def _screen_charts(dataset_id: str, entries: list[Entry]) -> list[Chart]:
    chart_id = f"chart-screen-{dataset_id}"
    panels = []
    for i, entry in enumerate(entries):
        name = _name(entry)
        kept, dropped, rule, by_model, through = _screen_outcomes(entry.metrics)
        rows = [
            StackRow(f"{_plural(kept + dropped, 'real report')}", [
                Segment(kept, "success", f"{_plural(kept, 'real report')} kept"),
                Segment(dropped, "error", f"{_plural(dropped, 'real report')} screened out"),
            ]),
            StackRow(f"{_plural(rule + by_model + through, 'non-report')}", [
                Segment(rule, "primary", f"{_plural(rule, 'non-report')} dropped by the free rule"),
                Segment(by_model, "primary-soft",
                        f"{_plural(by_model, 'non-report')} dropped by the agent LLM"),
                Segment(through, "warning", f"{_plural(through, 'non-report')} kept as reports"),
            ]),
        ]  # fmt: skip
        svg = charts.stack_chart(
            f"{chart_id}-{i}",
            f"{name}: what the screen did with each message",
            f"Of {kept + dropped} real reports, {kept} kept and {dropped} screened out. Of "
            f"{rule + by_model + through} non-reports, {rule} dropped by the free rule, "
            f"{by_model} by the agent LLM and {through} kept.",
            rows,
        )
        panels.append(
            Panel(
                name, svg, f"{_plural(dropped, 'real report')} wrongly screened out", dropped > 0
            )  # fmt: skip
        )
    return [
        Chart(
            chart_id,
            "Not-a-report screen: what happened to each message",
            "Each bar is one kind of message, split by what the screen did with it. Red is the "
            "mistake that matters: a real report screened out.",
            panels,
            legend=SCREEN_LEGEND,
            tables=[
                NumberTable(
                    "Messages per outcome, per model",
                    ["Outcome", *(_name(e) for e in entries)],
                    [
                        [swatch.label, *(str(_screen_outcomes(e.metrics)[k]) for e in entries)]
                        for k, swatch in enumerate(SCREEN_LEGEND)
                    ],
                )
            ],
        )
    ]


CHARTS: dict[str, Callable[[str, list[Entry]], list[Chart]]] = {
    "photo_judge": _photo_charts,
    "pair_judge": _pair_charts,
    "screen": _screen_charts,
}

# ---------- Voice Transcribe (no model columns: one model, several request settings) ----------

VOICE_INTRO = (
    "Muse Voice Transcribe turns each voice note into text before the agent reads it. Words "
    "wrong (word error rate) is the share of the script's words it got wrong, missed or added: "
    "lower is better, 0% is perfect. Place names heard counts the barangay and sitio names it "
    "spelled right, which the agent needs to place a report on the map."
)
VOICE_PROVENANCE = (
    "Real recordings, but read from scripts by one speaker, with no wind, rain or crowd noise."
)
VOICE_NOT_MEANING = (
    "Eight read notes from one speaker are not every resident's voice: field recordings with "
    "wind, rain and several speakers will be harder. Sending the notes' own place names is an "
    "upper bound, since the bot cannot know a report's place names before it hears them; the bot "
    "uses the setting marked as its own. A misheard place name is not lost: the officer sees the "
    "transcript and the reporter is asked where."
)


def _percent(value: float) -> str:
    return f"{value:.0%}" if abs(value * 100 - round(value * 100)) < 1e-9 else f"{value:.1%}"


def _wer_chart(entry: Entry, settings: dict[str, Any], label: Callable[[str], str]) -> Chart:
    chart_id = f"chart-wer-{entry.dataset.id}"
    top = max(max(s["wer"], s["wer_clean"]) for s in settings.values())
    step = 0.1 if top <= 0.5 else 0.2 if top <= 1 else 0.5
    ticks = [round(i * step, 2) for i in range(max(1, math.ceil(top / step - 1e-9)) + 1)]
    rows = [
        BarRow(
            label(key),
            [
                Bar(s["wer"], _percent(s["wer"]), "primary-soft"),
                Bar(s["wer_clean"], _percent(s["wer_clean"]), "primary"),
            ],
            f"{s['label']}: {_percent(s['wer'])} of words wrong, "
            f"{_percent(s['wer_clean'])} after cleanup",
        )  # fmt: skip
        for key, s in settings.items()
    ]
    svg = charts.bar_chart(
        f"{chart_id}-0", "Voice Transcribe: words wrong per request setting",
        "; ".join(f"{s['label']}: {_percent(s['wer'])}, {_percent(s['wer_clean'])} after cleanup"
                  for s in settings.values()) + ".",
        rows, domain=ticks[-1], ticks=ticks, tick_text=_percent,
    )  # fmt: skip
    return Chart(
        chart_id,
        "Words wrong per setting (lower is better)",
        f"On {entry.n} voice notes, {entry.metrics['reference_words']} words of script. The darker "
        "bar is the transcript after the free cleanup step, which is what the agent reads.",
        [Panel(_name(entry), svg)],
        legend=[Swatch("primary-soft", "As transcribed"), Swatch("primary", "After cleanup")],
        tables=[
            NumberTable(
                "Voice Transcribe per request setting",
                ["Setting", "Words wrong", "After cleanup", "Place names heard", "Keyword hints"],
                [
                    [
                        label(key),
                        _percent(s["wer"]),
                        _percent(s["wer_clean"]),
                        f"{s['place_names_heard']} of {s['place_names']}"
                        if s["place_names"]
                        else "not labeled",
                        str(s["keyword_count"]),
                    ]
                    for key, s in settings.items()
                ],  # fmt: skip
            )
        ],
    )


def _place_chart(entry: Entry, settings: dict[str, Any], label: Callable[[str], str]) -> Chart:
    chart_id = f"chart-places-{entry.dataset.id}"
    rows = []
    for key, s in settings.items():
        heard, total = s["place_names_heard"], s["place_names"]
        if not total:
            rows.append(
                BarRow(label(key), [Bar(None, "not labeled")], f"{s['label']}: not labeled")
            )
            continue
        rate = heard / total
        rows.append(
            BarRow(
                label(key),
                [Bar(rate, f"{heard} of {total}", weak=rate < WEAK_BELOW)],
                f"{s['label']}: {heard} of {total} place names heard right",
            )  # fmt: skip
        )
    svg = charts.bar_chart(
        f"{chart_id}-0", "Voice Transcribe: place names heard right per request setting",
        "; ".join(f"{s['label']}: {s['place_names_heard']} of {s['place_names']}"
                  for s in settings.values() if s["place_names"]) + ".",
        rows, ticks=F1_TICKS, tick_text=_percent, threshold=WEAK_BELOW,
        threshold_text=f"weak below {_percent(WEAK_BELOW)}",
    )  # fmt: skip
    return Chart(
        chart_id,
        "Place names heard right per setting",
        "The share of the notes' barangay and sitio names spelled right in the transcript.",
        [Panel(_name(entry), svg)],
        note="The numbers are in the table of the chart above (Show the numbers).",
    )


def _setting_labeler(settings: dict[str, Any], production: str) -> Callable[[str], str]:
    return lambda key: (
        settings[key]["label"] + (" (the bot uses this)" if key == production else "")
    )


def build_voice(results: Results) -> list[VoiceSection]:
    sections = []
    for entry in sorted(results.for_task("transcribe"), key=lambda e: (e.dataset.id, e.model)):
        settings = entry.metrics["settings"]
        label = _setting_labeler(settings, entry.metrics["production"])
        found = [_wer_chart(entry, settings, label)]
        if any(s["place_names"] for s in settings.values()):
            found.append(_place_chart(entry, settings, label))
        sections.append(
            VoiceSection(
                entry,
                f"Voice Transcribe: {entry.dataset.title}",
                VOICE_INTRO,
                VOICE_PROVENANCE,
                VOICE_NOT_MEANING,
                found,
            )  # fmt: skip
        )
    return sections


def accuracy_page(path: Path) -> AccuracyPage:
    """The page's content, or an empty page saying why there is nothing to show."""
    try:
        results = load(path)
    except FileNotFoundError:
        return AccuracyPage()
    except (OSError, ValueError) as exc:  # ResultsError, bad JSON or text: say so, show nothing
        return AccuracyPage(problem=f"{type(exc).__name__}: {exc}"[:300])
    return AccuracyPage(
        tables=build_tables(results),
        voice=build_voice(results),
        spend_usd=sum(e.spend_usd for e in results.entries),
        latest_date=max((e.date for e in results.entries), default=None),
    )

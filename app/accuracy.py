"""The "How accurate is the agent?" page, read from data/eval/results.json (app.eval_results).

One table per task and dataset, one column per model (Spark 1.3 first, so it reads side by side
with Spark Open), one row per measured score, each with a plain-language caption for a city
officer: what the number means and what it does not. Every number comes from the results file;
nothing here is typed in by hand. A missing, empty or invalid file is an empty state, not an error.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.eval_results import Dataset, Entry, Results, load
from app.trace import ModelFamily, model_family

# Below this a score is shown as weak: kappa 0.6 is the usual floor of "substantial" agreement.
WEAK_BELOW = 0.6
MODEL_ORDER = ("muse-spark-1.3", "muse-spark-open")  # Spark 1.3 first, then the open weights
TASK_ORDER = ("photo_judge", "pair_judge")
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


@dataclass
class AccuracyPage:
    tables: list[Table] = field(default_factory=list)
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
ROWS = {"photo_judge": PHOTO_ROWS, "pair_judge": PAIR_ROWS}


def _model_key(entry: Entry) -> tuple[int, str]:
    order = MODEL_ORDER.index(entry.model) if entry.model in MODEL_ORDER else len(MODEL_ORDER)
    return order, entry.model


def _run_rows(entries: list[Entry]) -> list[Row]:
    """Where each column's numbers come from: n, date, code commit and spend."""
    return [
        Row(
            "Items scored",
            "Labelled items the model answered; failed calls are not scored.",
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
            )
        )
    return tables


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
        spend_usd=sum(e.spend_usd for e in results.entries),
        latest_date=max((e.date for e in results.entries), default=None),
    )

"""The measured accuracy of the agent's models: one JSON file, a typed reader and a writer.

``data/eval/results.json`` is the single source of truth for every accuracy number the README
or the dashboard shows. The eval scripts write it with ``--record`` (see ``upsert``); nothing
else edits the numbers by hand. Its shape (``schema`` 1)::

    {
      "schema": 1,
      "datasets": {                      # what was measured, keyed by dataset id
        "<id>": {
          "title": str, "provenance": "real" | "synthetic",
          "source": str,                 # where the items come from, with licence
          "label_source": str,           # who labelled them, how, when
          "n": int,                      # labelled items in the dataset
          "location": str                # where the items live (never inside this repo)
        }
      },
      "entries": [                       # one per (task, dataset, model)
        {
          "task": "photo_judge" | "pair_judge",
          "dataset": "<id>",
          "model": str,                  # backend name, e.g. "muse-spark-1.3"
          "model_id": str,               # the provider's model id
          "backend": str,                # who served it, e.g. "Meta Model API", "Baseten"
          "sam": str | null,             # SAM model id when masks were scored too
          "date": "YYYY-MM-DD",          # Asia/Manila day of the run
          "commit": str,                 # git commit of the code that produced it
          "n": int,                      # items scored (errors excluded)
          "errors": int,                 # items that failed and were not scored
          "spend_usd": float,            # logged list-price spend of the recorded runs
          "metrics": {...}               # see below
        }
      ]
    }

``photo_judge`` metrics: ``roof_damage`` {kappa, agreement, confusion{label{predicted: count}}},
``flags`` {name: scores} for floodwater, debris, fallen_tree and tarpaulin (the LLM's
judgment), and ``sam`` {roof, floodwater: scores} or null. ``pair_judge`` metrics: scores plus
threshold, review_rate, reviews and miss_ids. ``scores`` are precision, recall, f1 and the
tp/fp/fn/tn counts.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from app.evaluation import REPO_ROOT, BinaryScores
from app.models import MANILA

RESULTS_PATH = REPO_ROOT / "data" / "eval" / "results.json"
SCHEMA_VERSION = 1
TASKS = ("photo_judge", "pair_judge")
PROVENANCES = ("real", "synthetic")
FLAGS = ("floodwater", "debris", "tarpaulin", "fallen_tree")
SAM_CONCEPTS = ("roof", "floodwater")
_SCORE_KEYS = ("precision", "recall", "f1", "tp", "fp", "fn", "tn")
_DATASET_KEYS = ("title", "provenance", "source", "label_source", "n", "location")
_ENTRY_KEYS = (
    "task", "dataset", "model", "model_id", "backend", "sam", "date", "commit", "n", "errors",
    "spend_usd", "metrics",
)  # fmt: skip

Task = Literal["photo_judge", "pair_judge"]


@dataclass(frozen=True)
class Dataset:
    id: str
    title: str
    provenance: Literal["real", "synthetic"]
    source: str
    label_source: str
    n: int
    location: str


@dataclass(frozen=True)
class Entry:
    task: Task
    dataset: Dataset
    model: str
    model_id: str
    backend: str
    sam: str | None
    date: str
    commit: str
    n: int
    errors: int
    spend_usd: float
    metrics: dict[str, Any]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.task, self.dataset.id, self.model)

    def score(self, *path: str) -> dict[str, Any]:
        """A nested scores dict, e.g. entry.score("flags", "floodwater")."""
        node: Any = self.metrics
        for part in path:
            node = node[part]
        return node


@dataclass(frozen=True)
class Results:
    datasets: dict[str, Dataset]
    entries: list[Entry]

    def find(self, task: Task, dataset: str, model: str) -> Entry | None:
        return next((e for e in self.entries if e.key == (task, dataset, model)), None)

    def for_task(self, task: Task) -> list[Entry]:
        return [e for e in self.entries if e.task == task]


class ResultsError(ValueError):
    """The results file does not match its schema."""


def scores_dict(scores: BinaryScores) -> dict[str, Any]:
    return {
        "precision": round(scores.precision, 4),
        "recall": round(scores.recall, 4),
        "f1": round(scores.f1, 4),
        "tp": scores.tp,
        "fp": scores.fp,
        "fn": scores.fn,
        "tn": scores.tn,
    }


def validate(raw: Any) -> list[str]:
    """Every way `raw` breaks the schema above (empty when valid)."""
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return [f"top level must be an object with schema {SCHEMA_VERSION}"]
    errors: list[str] = []
    datasets = raw.get("datasets")
    if not isinstance(datasets, dict):
        return ["datasets must be an object"]
    for key, dataset in datasets.items():
        if not isinstance(dataset, dict) or set(dataset) != set(_DATASET_KEYS):
            errors.append(f"dataset {key}: needs exactly {', '.join(_DATASET_KEYS)}")
            continue
        if dataset["provenance"] not in PROVENANCES:
            errors.append(f"dataset {key}: provenance must be one of {PROVENANCES}")
        if not _positive_int(dataset["n"]):
            errors.append(f"dataset {key}: n must be a positive integer")
        if not all(isinstance(dataset[k], str) and dataset[k] for k in _DATASET_KEYS if k != "n"):
            errors.append(f"dataset {key}: text fields must be non-empty")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return [*errors, "entries must be a list"]
    seen = set()
    for i, entry in enumerate(entries):
        where = f"entry {i}"
        if not isinstance(entry, dict) or set(entry) != set(_ENTRY_KEYS):
            errors.append(f"{where}: needs exactly {', '.join(_ENTRY_KEYS)}")
            continue
        where = f"entry {entry['task']}/{entry['dataset']}/{entry['model']}"
        key = (entry["task"], entry["dataset"], entry["model"])
        if key in seen:
            errors.append(f"{where}: duplicate")
        seen.add(key)
        if entry["task"] not in TASKS:
            errors.append(f"{where}: task must be one of {TASKS}")
        if entry["dataset"] not in datasets:
            errors.append(f"{where}: unknown dataset")
        elif _positive_int(entry["n"]) and entry["n"] > datasets[entry["dataset"]]["n"]:
            errors.append(f"{where}: n is larger than the dataset")
        if not _positive_int(entry["n"]) or not isinstance(entry["errors"], int):
            errors.append(f"{where}: n must be a positive integer and errors an integer")
        if not _is_date(entry["date"]):
            errors.append(f"{where}: date must be YYYY-MM-DD")
        if not isinstance(entry["commit"], str) or len(entry["commit"]) < 7:
            errors.append(f"{where}: commit must be a git hash")
        if not isinstance(entry["spend_usd"], int | float) or entry["spend_usd"] < 0:
            errors.append(f"{where}: spend_usd must be a non-negative number")
        errors.extend(f"{where}: {e}" for e in _metric_errors(entry["task"], entry["metrics"]))
    return errors


def _metric_errors(task: str, metrics: Any) -> list[str]:
    if not isinstance(metrics, dict):
        return ["metrics must be an object"]
    if task == "pair_judge":
        missing = [k for k in (*_SCORE_KEYS, "threshold", "review_rate", "reviews", "miss_ids")
                   if k not in metrics]  # fmt: skip
        return [f"metrics miss {', '.join(missing)}"] if missing else []
    errors = []
    roof = metrics.get("roof_damage")
    if not isinstance(roof, dict) or not {"kappa", "agreement", "confusion"} <= set(roof):
        errors.append("metrics.roof_damage needs kappa, agreement and confusion")
    flags = metrics.get("flags")
    if not isinstance(flags, dict) or set(flags) != set(FLAGS):
        errors.append(f"metrics.flags needs exactly {', '.join(FLAGS)}")
    else:
        errors.extend(f"metrics.flags.{f} is not a scores object" for f in FLAGS
                      if not _is_scores(flags[f]))  # fmt: skip
    sam = metrics.get("sam", "missing")
    if sam == "missing":
        errors.append("metrics.sam is required (null when SAM was not scored)")
    elif sam is not None and (
        not isinstance(sam, dict) or not all(_is_scores(sam.get(c)) for c in SAM_CONCEPTS)
    ):
        errors.append(f"metrics.sam needs scores for {', '.join(SAM_CONCEPTS)}")
    return errors


def _is_scores(value: Any) -> bool:
    return isinstance(value, dict) and all(k in value for k in _SCORE_KEYS)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_date(value: Any) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return True


def read_raw(path: Path = RESULTS_PATH) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    problems = validate(raw)
    if problems:
        raise ResultsError(f"{path}: " + "; ".join(problems))
    return raw


def load(path: Path = RESULTS_PATH) -> Results:
    """The results file, validated and typed (raises ResultsError if it breaks the schema)."""
    raw = read_raw(path)
    datasets = {key: Dataset(id=key, **value) for key, value in raw["datasets"].items()}
    entries = [
        Entry(**{**entry, "dataset": datasets[entry["dataset"]]}) for entry in raw["entries"]
    ]
    return Results(datasets, entries)


def dataset_ids(path: Path = RESULTS_PATH) -> list[str]:
    return sorted(read_raw(path)["datasets"])


def upsert(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace the entry for (task, dataset, model); spend accumulates across runs.

    The file stays sorted (task, dataset, model) so diffs are stable. Returns the stored entry.
    """
    raw = read_raw(path)
    key = (entry["task"], entry["dataset"], entry["model"])
    old = next((e for e in raw["entries"] if (e["task"], e["dataset"], e["model"]) == key), None)
    stored = {k: entry[k] for k in _ENTRY_KEYS if k != "spend_usd"}
    stored["spend_usd"] = round(entry["spend_usd"] + (old["spend_usd"] if old else 0.0), 4)
    entries = [e for e in raw["entries"] if e is not old] + [stored]
    entries.sort(key=lambda e: (e["task"], e["dataset"], e["model"]))
    updated = {**raw, "entries": entries}
    problems = validate(updated)
    if problems:
        raise ResultsError("; ".join(problems))
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return stored


# Who serves each client, by its `service` name, for the results file.
BACKEND_NAMES = {"Muse Spark": "Meta Model API"}


def backend_of(client: object) -> str:
    service = getattr(client, "service", "unknown")
    return BACKEND_NAMES.get(service, service)


def manila_today() -> str:
    return datetime.now(MANILA).date().isoformat()


# The code an eval result depends on: a local change here marks the recorded commit "-dirty".
EVAL_CODE = (
    "scripts", "app/meta", "app/photo.py", "app/pair_judge.py", "app/facts.py",
    "app/gazetteer.py", "app/evaluation.py", "app/eval_results.py",
)  # fmt: skip


def git_commit() -> str:
    """The checked-out commit, marked "-dirty" when the eval code (EVAL_CODE) has local edits."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = git("rev-parse", "--short=12", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no", "--", *EVAL_CODE)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}-dirty" if dirty else commit


# --- Markdown for the README (a test keeps the README's copy identical) ---------------------

README_START = "<!-- eval-results:start (generated by: uv run python -m app.eval_results) -->"
README_END = "<!-- eval-results:end -->"


def _f(value: float) -> str:
    return f"{value:.2f}"


def _dataset_cell(dataset: Dataset) -> str:
    return f"{dataset.title} ({dataset.provenance})"


def render_markdown(results: Results) -> str:
    """The README tables, one row per entry, numbers straight from the results file."""
    lines = [README_START, ""]
    photo = results.for_task("photo_judge")
    if photo:
        lines += [
            "**Photo judge** (the agent LLM judges each photo; SAM 3.1 finds roofs and "
            "floodwater):",
            "",
            "| Model | Dataset | n | Roof damage κ | Floodwater F1 | Debris F1 | Tarpaulin F1 "
            "| Fallen tree F1 | SAM roof F1 | SAM floodwater F1 | Date |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for e in photo:
            sam = e.metrics["sam"]
            flags = [_f(e.score("flags", f)["f1"]) for f in FLAGS]
            sam_cells = [_f(sam[c]["f1"]) if sam else "–" for c in SAM_CONCEPTS]
            lines.append(
                f"| {e.model} | {_dataset_cell(e.dataset)} | {e.n} "
                f"| {_f(e.metrics['roof_damage']['kappa'])} | {' | '.join(flags)} "
                f"| {' | '.join(sam_cells)} | {e.date} |"
            )
        lines.append("")
    pairs = results.for_task("pair_judge")
    if pairs:
        lines += [
            '**Incident pair judge** ("is this new report the same incident?", merge threshold '
            f"{pairs[0].metrics['threshold']:.2f}; below it, or unsure, goes to the officer):",
            "",
            "| Model | Dataset | n | Precision | Recall | F1 | Officer review rate | Date |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for e in pairs:
            m = e.metrics
            lines.append(
                f"| {e.model} | {_dataset_cell(e.dataset)} | {e.n} | {_f(m['precision'])} "
                f"| {_f(m['recall'])} | {_f(m['f1'])} | {m['review_rate']:.0%} | {e.date} |"
            )
        lines.append("")
    return "\n".join([*lines, README_END])


def readme_block(readme: str) -> str | None:
    """The generated block currently in the README, or None if it has none."""
    start, end = readme.find(README_START), readme.find(README_END)
    if start < 0 or end < start:
        return None
    return readme[start : end + len(README_END)]


def main() -> int:
    print(render_markdown(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The measured accuracy of the agent's models: one JSON file, a typed reader and a writer.

``data/eval/results.json`` is the single source of truth for every accuracy number the README
or the dashboard shows. The eval scripts write it with ``--record`` (see ``upsert``); nothing
else edits the numbers by hand. Its shape (``schema`` 1)::

    {
      "schema": 1,
      "datasets": {                      # what was measured, keyed by dataset id
        "<id>": {
          "title": str, "provenance": "real" | "synthetic",
          "source": str,                 # where the items come from, with license
          "label_source": str,           # who labeled them, how, when
          "n": int,                      # labeled items in the dataset
          "location": str                # where the items live (never inside this repo)
        }
      },
      "entries": [                       # one per (task, dataset, model)
        {
          "task": "photo_judge" | "crop_photo" | "pair_judge" | "screen" | "transcribe"
                  | "crosscheck",
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
threshold, review_rate, reviews and miss_ids. ``screen`` metrics (the not-a-report screen,
app.screening): scores for the "real report" class (a kept message is a positive), plus
wrongly_filtered (real reports screened out), non_reports_filtered, rule_filtered (dropped by
the free rule), model_calls (screening calls made), unsure and miss_ids. ``scores`` are
precision, recall, f1 and the tp/fp/fn/tn counts. ``transcribe`` metrics (Voice Transcribe on
voice notes, scripts.eval_transcribe): reference_words, production (the settings key the bot
uses) and settings {name: {label, language_bias (list or null), keywords (what was sent:
"none", "barangays", "barangays+places", "barangays+osm" or "barangays+osm:N"), keyword_count,
wer, wer_clean (after app.transcript_cleanup), place_names_heard, place_names (both null when
unlabeled), insertions (keyword-list names the transcript has that the script does not;
absent in runs recorded before 2026-11-02)}}; every setting was run on the same n notes.
``crosscheck`` metrics (Spark and SAM checking each other, app.crosscheck, scored by
scripts.eval_crosscheck from a photo_judge run's outputs): photos, disagreed (photos where
any rule fired), asked (photos where a rule that asks the officer fired) and checks {code:
{asks, fired, spark_right, sam_right}}, where spark_right + sam_right == fired.

``crop_photo`` metrics (rice-field photos, scripts.eval_crop): ``crop_field`` {kappa, agreement,
confusion} like roof_damage, ``damaged`` scores (flooded or lodged is a positive) and ``sam``
{field concept: {found, measured, water_share_mae, water_share_close, close_points}}: how often
SAM outlined the field, and how far its share of the field under water was from the label.
"""

import argparse
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
TASKS = ("photo_judge", "crop_photo", "pair_judge", "screen", "transcribe", "crosscheck")
PROVENANCES = ("real", "synthetic")
FLAGS = ("floodwater", "debris", "tarpaulin", "fallen_tree")
SAM_CONCEPTS = ("roof", "floodwater")
_SCORE_KEYS = ("precision", "recall", "f1", "tp", "fp", "fn", "tn")
_DATASET_KEYS = ("title", "provenance", "source", "label_source", "n", "location")
_ENTRY_KEYS = (
    "task", "dataset", "model", "model_id", "backend", "sam", "date", "commit", "n", "errors",
    "spend_usd", "metrics",
)  # fmt: skip

Task = Literal["photo_judge", "crop_photo", "pair_judge", "screen", "transcribe", "crosscheck"]
CROP_SAM_KEYS = ("found", "measured", "water_share_mae", "water_share_close", "close_points")
SCREEN_KEYS = (
    "wrongly_filtered", "non_reports_filtered", "rule_filtered", "model_calls", "unsure",
    "miss_ids",
)  # fmt: skip
TRANSCRIBE_SETTING_KEYS = (
    "label", "language_bias", "keywords", "keyword_count", "wer", "wer_clean",
    "place_names_heard", "place_names", "insertions",
)  # fmt: skip
CROSSCHECK_KEYS = ("asks", "fired", "spark_right", "sam_right")


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
    if task == "screen":
        missing = [k for k in (*_SCORE_KEYS, *SCREEN_KEYS) if k not in metrics]
        return [f"metrics miss {', '.join(missing)}"] if missing else []
    if task == "transcribe":
        return _transcribe_errors(metrics)
    if task == "crosscheck":
        return _crosscheck_errors(metrics)
    if task == "crop_photo":
        return _crop_errors(metrics)
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


def _crop_errors(metrics: dict[str, Any]) -> list[str]:
    errors = []
    field = metrics.get("crop_field")
    if not isinstance(field, dict) or not {"kappa", "agreement", "confusion"} <= set(field):
        errors.append("metrics.crop_field needs kappa, agreement and confusion")
    if not _is_scores(metrics.get("damaged")):
        errors.append("metrics.damaged is not a scores object")
    sam = metrics.get("sam")
    if not isinstance(sam, dict) or not all(
        isinstance(v, dict) and set(v) == set(CROP_SAM_KEYS) for v in sam.values()
    ):
        errors.append(f"metrics.sam needs {', '.join(CROP_SAM_KEYS)} per concept")
    return errors


def _transcribe_errors(metrics: dict[str, Any]) -> list[str]:
    settings = metrics.get("settings")
    if not _positive_int(metrics.get("reference_words")):
        return ["metrics.reference_words must be a positive integer"]
    if not isinstance(settings, dict) or not settings:
        return ["metrics.settings must be a non-empty object"]
    errors = []
    if metrics.get("production") not in settings:
        errors.append("metrics.production must name one of the settings")
    for name, setting in settings.items():
        where = f"metrics.settings.{name}"
        keys = set(setting) if isinstance(setting, dict) else set()
        if keys not in (
            set(TRANSCRIBE_SETTING_KEYS),
            set(TRANSCRIBE_SETTING_KEYS) - {"insertions"},
        ):
            errors.append(f"{where} needs exactly {', '.join(TRANSCRIBE_SETTING_KEYS)}")
            continue
        if not all(_rate(setting[k]) for k in ("wer", "wer_clean")):
            errors.append(f"{where}: wer and wer_clean must be non-negative numbers")
        heard, total = setting["place_names_heard"], setting["place_names"]
        if not (heard is None and total is None) and not (
            _count(heard) and _positive_int(total) and heard <= total
        ):
            errors.append(f"{where}: place_names_heard must be a count up to place_names")
        if not _count(setting["keyword_count"]) or not _count(setting.get("insertions", 0)):
            errors.append(f"{where}: keyword_count and insertions must be counts")
    return errors


def _crosscheck_errors(metrics: dict[str, Any]) -> list[str]:
    checks = metrics.get("checks")
    if not _positive_int(metrics.get("photos")) or not all(
        _count(metrics.get(k)) for k in ("disagreed", "asked")
    ):
        return ["metrics.photos must be a positive integer, disagreed and asked counts"]
    if not metrics["asked"] <= metrics["disagreed"] <= metrics["photos"]:
        return ["metrics.asked cannot exceed disagreed, nor disagreed photos"]
    if not isinstance(checks, dict) or not checks:
        return ["metrics.checks must be a non-empty object"]
    errors = []
    for code, check in checks.items():
        where = f"metrics.checks.{code}"
        if not isinstance(check, dict) or set(check) != set(CROSSCHECK_KEYS):
            errors.append(f"{where} needs exactly {', '.join(CROSSCHECK_KEYS)}")
        elif not isinstance(check["asks"], bool) or not all(
            _count(check[k]) for k in CROSSCHECK_KEYS[1:]
        ):
            errors.append(f"{where}: asks must be true or false and the rest counts")
        elif check["spark_right"] + check["sam_right"] != check["fired"] or (
            check["fired"] > metrics["photos"]
        ):
            errors.append(f"{where}: spark_right + sam_right must equal fired (at most photos)")
    return errors


def _rate(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
BACKEND_NAMES = {"Muse Spark": "Meta Model API", "Voice Transcribe": "Meta Model API"}


def backend_of(client: object) -> str:
    service = getattr(client, "service", "unknown")
    return BACKEND_NAMES.get(service, service)


def manila_today() -> str:
    return datetime.now(MANILA).date().isoformat()


# The code an eval result depends on: a local change here marks the recorded commit "-dirty".
EVAL_CODE = (
    "scripts", "app/meta", "app/photo.py", "app/pair_judge.py", "app/facts.py",
    "app/gazetteer.py", "app/evaluation.py", "app/eval_results.py", "app/screening.py",
    "app/intake.py", "app/audio.py", "app/transcript_cleanup.py", "app/crosscheck.py",
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


# --- Markdown for the README and evaluation guide (tests keep the copies identical) -------

README_START = "<!-- eval-results:start (generated by: uv run python -m app.eval_results) -->"
README_END = "<!-- eval-results:end -->"
HEADLINES_START = "<!-- eval-headlines:start -->"
HEADLINES_END = "<!-- eval-headlines:end -->"
MODELS_START = "<!-- eval-models:start -->"
MODELS_END = "<!-- eval-models:end -->"
COMPARISON_START = "<!-- eval-comparison:start -->"
COMPARISON_END = "<!-- eval-comparison:end -->"


def _required(results: Results, *key: str) -> Entry:
    entry = results.find(*key)
    if entry is None:
        raise ResultsError(f"A recorded run needed for the judge-facing results is missing: {key}")
    return entry


def render_headlines(results: Results) -> str:
    """The judge-facing figures, including denominators, from the same recorded runs."""
    keys = (
        ("photo_judge", "kill-test-25", "muse-spark-1.3"),
        ("pair_judge", "dedupe-pairs-64", "muse-spark-1.3"),
        ("screen", "screen-messages-60", "muse-spark-1.3"),
        ("transcribe", "voice-notes-8", "muse-voice-transcribe-1.0"),
    )
    entries = [results.find(*key) for key in keys]
    if any(entry is None for entry in entries):
        raise ResultsError("A recorded run needed for the README headlines is missing")
    photo, pairs, screen, voice = entries
    assert photo is not None and pairs is not None and screen is not None and voice is not None
    production = voice.metrics["settings"][voice.metrics["production"]]
    return "\n".join(
        [
            HEADLINES_START,
            f"- **{photo.metrics['roof_damage']['kappa']:.2f} agreement score** for roof damage on "
            f"{photo.n} real typhoon photos (Spark 1.3).",
            f"- **{pairs.metrics['f1']:.2f} matching score** on {pairs.n} synthetic report pairs "
            "(Spark 1.3; a small, easy set).",
            f"- **{screen.metrics['wrongly_filtered']} of "
            f"{screen.metrics['tp'] + screen.metrics['fn']}** synthetic disaster reports "
            f"wrongly filtered among {screen.n} test messages.",
            f"- **{production['wer']:.1%} word error rate** on {voice.n} voice notes in "
            "Tagalog and English, read by one speaker (the bot's setting).",
            HEADLINES_END,
        ]
    )


def render_models(results: Results) -> str:
    """The model jobs and one recorded result for each, in plain language."""
    photo = _required(results, "photo_judge", "kill-test-25", "muse-spark-1.3")
    opened = _required(results, "photo_judge", "kill-test-25", "muse-spark-open")
    voice = _required(results, "transcribe", "voice-notes-8", "muse-voice-transcribe-1.0")
    setting = voice.metrics["settings"][voice.metrics["production"]]
    roof = photo.metrics["sam"]["roof"]
    return "\n".join(
        [
            MODELS_START,
            "| Meta model | Its job in Ulat | One recorded result |",
            "|---|---|---|",
            "| Muse Spark 1.3 | Reads reports, judges photos, groups reports and drafts a summary. "
            f"| {photo.metrics['roof_damage']['agreement']} of {photo.n} "
            "roof judgments matched the labels. |",
            "| Muse Spark Open | Runs the same agent with open weights on Baseten. "
            f"| {opened.metrics['roof_damage']['agreement']} of {opened.n} "
            "roof judgments matched the labels. |",
            "| Muse Voice Transcribe | Turns spoken reports into text. "
            f"| {setting['wer']:.1%} word error rate across {voice.n} voice notes, one speaker. |",
            "| SAM 3.1 | Outlines roofs and floodwater for an officer to check. "
            f"| Found roofs in {roof['tp']} of {roof['tp'] + roof['fn']} labeled roof photos. |",
            MODELS_END,
        ]
    )


def render_comparison(results: Results) -> str:
    """Chance-adjusted agreement for both agents on the same real photo sets."""
    roof = [
        _required(results, "photo_judge", "kill-test-25", model)
        for model in ("muse-spark-1.3", "muse-spark-open")
    ]
    crop = [
        _required(results, "crop_photo", "rice-field-19", model)
        for model in ("muse-spark-1.3", "muse-spark-open")
    ]
    return "\n".join(
        [
            COMPARISON_START,
            "| Test on real photos | Spark 1.3 | Spark Open |",
            "|---|---:|---:|",
            f"| Roof damage | {roof[0].metrics['roof_damage']['kappa']:.2f} ({roof[0].n} photos) "
            f"| {roof[1].metrics['roof_damage']['kappa']:.2f} ({roof[1].n} photos) |",
            f"| Rice field condition | {crop[0].metrics['crop_field']['kappa']:.2f} "
            f"({crop[0].n} photos) | {crop[1].metrics['crop_field']['kappa']:.2f} "
            f"({crop[1].n} photos) |",
            COMPARISON_END,
        ]
    )


def marked_block(text: str, start_marker: str, end_marker: str) -> str | None:
    start, end = text.find(start_marker), text.find(end_marker)
    if start < 0 or end < start:
        return None
    return text[start : end + len(end_marker)]


def headline_block(readme: str) -> str | None:
    return marked_block(readme, HEADLINES_START, HEADLINES_END)


def _f(value: float) -> str:
    return f"{value:.2f}"


def _dataset_cell(dataset: Dataset) -> str:
    return f"{dataset.title} ({dataset.provenance})"


def render_markdown(results: Results) -> str:
    """The evaluation guide's tables, one row per entry, from the results file."""
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
    crops = results.for_task("crop_photo")
    if crops:
        lines += [
            "**Rice-field judge** (the same photo judge says whether a rice field is flooded, "
            'lodged or not visibly damaged; SAM 3.1 is asked for "rice field" and "floodwater" '
            "to see whether it can measure the share of a field under water):",
            "",
            "| Model | Dataset | n | Field condition κ | Same as label | Flooded or lodged: "
            "precision / recall | SAM field outlined | SAM water share within "
            "20 points | Date |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for e in crops:
            m, damaged = e.metrics, e.metrics["damaged"]
            sams = [f'"{c}" {s["found"]} of {e.n}' for c, s in m["sam"].items()]
            close = [
                f'"{c}" {s["water_share_close"]} of {s["measured"]}' for c, s in m["sam"].items()
            ]
            lines.append(
                f"| {e.model} | {_dataset_cell(e.dataset)} | {e.n} "
                f"| {_f(m['crop_field']['kappa'])} | {m['crop_field']['agreement']} of {e.n} "
                f"| {_f(damaged['precision'])} / {_f(damaged['recall'])} | {'; '.join(sams)} "
                f"| {'; '.join(close)} | {e.date} |"
            )
        lines.append("")
    lines += _crosscheck_markdown(results.for_task("crosscheck"))
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
    screens = results.for_task("screen")
    if screens:
        lines += [
            '**Not-a-report screen** (keeps real reports, screens out "test", "hahaha", mash and '
            "spam; a kept real report is a positive):",
            "",
            "| Model | Dataset | n | Precision | Recall | Real reports wrongly filtered "
            "| Non-reports filtered | By the free rule | Screening calls | Date |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for e in screens:
            m = e.metrics
            non_reports = m["tn"] + m["fp"]
            lines.append(
                f"| {e.model} | {_dataset_cell(e.dataset)} | {e.n} | {_f(m['precision'])} "
                f"| {_f(m['recall'])} | {m['wrongly_filtered']} "
                f"| {m['non_reports_filtered']} of {non_reports} | {m['rule_filtered']} "
                f"| {m['model_calls']} | {e.date} |"
            )
        lines.append("")
    transcribes = results.for_task("transcribe")
    if transcribes:
        lines += [
            "**Voice Transcribe** (Taglish voice notes through the bot's own audio path; WER "
            'counts every word that differs, so "seventy" for "70" is an error):',
            "",
            "| Model | Dataset | n | Setting | Keywords | WER | WER after cleanup "
            "| Place names heard | False insertions | Date |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for e in transcribes:
            for name, s in e.metrics["settings"].items():
                label = s["label"] + (" **(the bot)**" if name == e.metrics["production"] else "")
                places = (
                    f"{s['place_names_heard']} of {s['place_names']}" if s["place_names"] else "–"
                )
                lines.append(
                    f"| {e.model} | {_dataset_cell(e.dataset)} | {e.n} | {label} "
                    f"| {s['keyword_count']} | {s['wer']:.1%} | {s['wer_clean']:.1%} | {places} "
                    f"| {s.get('insertions', '–')} | {e.date} |"
                )
        lines.append("")
    return "\n".join([*lines, README_END])


def _crosscheck_markdown(entries: list[Entry]) -> list[str]:
    """Spark and SAM checking each other: how often each rule fired, whom the label sided with."""
    from app.crosscheck import CHECKS  # the rules; imported here to keep this module light

    if not entries:
        return []
    totals: dict[str, list[int]] = {}
    for e in entries:
        total = totals.setdefault(e.model, [0, 0, 0])
        for i, key in enumerate(("photos", "disagreed", "asked")):
            total[i] += e.metrics[key]
    lines = [
        "**Spark and SAM 3.1 check each other** on every photo. When Spark sees floodwater or a "
        "partly damaged roof that SAM cannot outline, the report gets a trust signal and Review "
        "asks the officer; the other disagreements are only measured (each cell: times it fired, "
        "then whose answer matched the hand label). "
        + "; ".join(
            f"{model}: they disagreed on {d} of {n} photos, and Review asked about {a}"
            for model, (n, d, a) in totals.items()
        )
        + ".",
        "",
        "| Model | Dataset | n | Disagreed | Asked the officer | "
        + " | ".join(f"{c.label} ({'asks' if c.asks else 'measured only'})" for c in CHECKS)
        + " | Date |",
        "|---|---|---|---|---|" + "---|" * len(CHECKS) + "---|",
    ]
    for e in entries:
        cells = []
        for check in CHECKS:
            m = e.metrics["checks"].get(check.code)
            cells.append(
                f"{m['fired']} (Spark {m['spark_right']}, SAM {m['sam_right']})" if m else "–"
            )
        lines.append(
            f"| {e.model} | {_dataset_cell(e.dataset)} | {e.n} | {e.metrics['disagreed']} "
            f"| {e.metrics['asked']} of {e.metrics['photos']} | {' | '.join(cells)} | {e.date} |"
        )
    return [*lines, ""]


def readme_block(readme: str) -> str | None:
    """The generated evaluation block in a document, or None if it has none."""
    return marked_block(readme, README_START, README_END)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the recorded evaluation results")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headlines", action="store_true", help="render the judge-facing figures")
    group.add_argument("--models", action="store_true", help="render the judge-facing model table")
    group.add_argument("--comparison", action="store_true", help="render the model comparison")
    args = parser.parse_args()
    if args.headlines:
        render = render_headlines
    elif args.models:
        render = render_models
    elif args.comparison:
        render = render_comparison
    else:
        render = render_markdown
    print(render(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

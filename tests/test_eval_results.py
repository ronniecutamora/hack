"""The results file: schema, typed loader, upsert and the README tables generated from it."""

import copy
import json

import pytest

from app import eval_results
from app.evaluation import BinaryScores

SCORES = eval_results.scores_dict(BinaryScores(tp=3, fp=1, fn=1, tn=5))


def photo_entry(**changes):
    entry = {
        "task": "photo_judge",
        "dataset": "kill-test-25",
        "model": "muse-spark-1.3",
        "model_id": "muse-spark-1.3",
        "backend": "Meta Model API",
        "sam": "sam-3.1",
        "date": "2026-10-29",
        "commit": "0123456789ab",
        "n": 25,
        "errors": 0,
        "spend_usd": 0.4,
        "metrics": {
            "roof_damage": {"kappa": 0.7, "agreement": 20, "confusion": {"none": {"none": 5}}},
            "flags": {flag: SCORES for flag in eval_results.FLAGS},
            "sam": {concept: SCORES for concept in eval_results.SAM_CONCEPTS},
        },
    }
    return {**entry, **changes}


def pair_entry(**changes):
    metrics = {**SCORES, "threshold": 0.85, "review_rate": 0.1, "reviews": 6, "miss_ids": ["p1"]}
    entry = photo_entry(
        task="pair_judge", dataset="dedupe-pairs-64", n=64, sam=None, metrics=metrics
    )
    return {**entry, **changes}


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "results.json"
    target.write_text(json.dumps({**eval_results.read_raw(), "entries": []}))
    return target


def test_the_repository_results_file_matches_its_schema():
    results = eval_results.load()
    assert set(results.datasets) >= {"kill-test-25", "synthetic-40", "dedupe-pairs-64"}
    assert {d.provenance for d in results.datasets.values()} == {"real", "synthetic"}
    for entry in results.entries:
        assert entry.n <= entry.dataset.n


def test_upsert_replaces_by_task_dataset_and_model_and_sums_spend(path):
    eval_results.upsert(path, photo_entry())
    eval_results.upsert(path, pair_entry())
    stored = eval_results.upsert(path, photo_entry(n=24, errors=1, spend_usd=0.1))
    assert stored["spend_usd"] == pytest.approx(0.5)
    results = eval_results.load(path)
    assert [e.key for e in results.entries] == [
        ("pair_judge", "dedupe-pairs-64", "muse-spark-1.3"),
        ("photo_judge", "kill-test-25", "muse-spark-1.3"),
    ]
    photo = results.find("photo_judge", "kill-test-25", "muse-spark-1.3")
    assert photo is not None and (photo.n, photo.errors) == (24, 1)
    assert photo.score("sam", "roof")["f1"] == SCORES["f1"]
    assert results.find("photo_judge", "kill-test-25", "muse-spark-open") is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"dataset": "unknown"}, "unknown dataset"),
        ({"task": "guess"}, "task must be one of"),
        ({"n": 26}, "larger than the dataset"),
        ({"n": 0}, "positive integer"),
        ({"date": "29/10/2026"}, "YYYY-MM-DD"),
        ({"commit": "abc"}, "git hash"),
        ({"spend_usd": -1}, "non-negative"),
        ({"metrics": {"flags": {}}}, "roof_damage"),
    ],
)
def test_upsert_refuses_entries_that_break_the_schema(path, change, message):
    before = path.read_text()
    with pytest.raises(eval_results.ResultsError, match=message):
        eval_results.upsert(path, photo_entry(**change))
    assert path.read_text() == before


def test_validate_reports_dataset_and_entry_problems():
    raw = copy.deepcopy(eval_results.read_raw())
    raw["datasets"]["kill-test-25"]["provenance"] = "scraped"
    raw["entries"] = [photo_entry(), photo_entry()]
    del raw["entries"][1]["metrics"]["sam"]
    problems = eval_results.validate(raw)
    assert any("provenance must be one of" in p for p in problems)
    assert any("duplicate" in p for p in problems)
    assert any("metrics.sam is required" in p for p in problems)
    assert eval_results.validate({"schema": 2}) != []


def test_markdown_has_one_row_per_entry_with_n_provenance_and_date(path):
    eval_results.upsert(path, photo_entry())
    eval_results.upsert(path, photo_entry(model="muse-spark-open", sam=None, metrics={
        **photo_entry()["metrics"], "sam": None}))  # fmt: skip
    eval_results.upsert(path, pair_entry())
    text = eval_results.render_markdown(eval_results.load(path))
    rows = [line for line in text.splitlines() if line.startswith("| muse-")]
    assert len(rows) == 3
    assert "| muse-spark-1.3 | Real typhoon photos (real) | 25 | 0.70 |" in text
    assert "| – | – | 2026-10-29 |" in text  # no SAM scores for that run
    assert "| muse-spark-1.3 | Synthetic report pairs (synthetic) | 64 | 0.75 |" in text
    assert "10% | 2026-10-29 |" in text
    assert eval_results.readme_block(f"intro\n{text}\nmore") == text
    assert eval_results.readme_block("no block") is None


def test_git_commit_names_the_checkout():
    commit = eval_results.git_commit()
    assert commit == "unknown" or len(commit.removesuffix("-dirty")) == 12

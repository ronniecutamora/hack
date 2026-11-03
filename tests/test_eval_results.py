"""The results file: schema, loader, upsert and the documents generated from it."""

import copy
import json
from dataclasses import replace

import pytest

from app import eval_results
from app.evaluation import REPO_ROOT, BinaryScores

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


def transcribe_entry(**changes):
    def setting(label, wer, heard):
        return {
            "label": label, "language_bias": ["Tagalog", "English"], "keywords": "barangays",
            "keyword_count": 31, "wer": wer, "wer_clean": wer - 0.02,
            "place_names_heard": heard, "place_names": 10 if heard is not None else None,
        }  # fmt: skip

    metrics = {
        "reference_words": 380,
        "production": "barangays",
        "settings": {
            "language_hint": setting("Language hint", 0.198, None),
            "barangays": setting("Hint + barangay names", 0.163, 9),
        },
    }
    entry = photo_entry(
        task="transcribe", dataset="voice-test-8", model="muse-voice-transcribe-1.0",
        model_id="muse-voice-transcribe-1.0", sam=None, n=8, metrics=metrics,
    )  # fmt: skip
    return {**entry, **changes}


VOICE_DATASET = {
    "title": "Taglish voice notes", "provenance": "real", "source": "fixture",
    "label_source": "fixture", "n": 8, "location": "outside the repo",
}  # fmt: skip


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "results.json"
    raw = eval_results.read_raw()
    datasets = {**raw["datasets"], "voice-test-8": VOICE_DATASET}
    target.write_text(json.dumps({**raw, "datasets": datasets, "entries": []}))
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


def test_a_transcribe_entry_has_one_row_per_setting_and_marks_the_bots(path):
    eval_results.upsert(path, transcribe_entry())
    entry = eval_results.load(path).for_task("transcribe")[0]
    assert entry.metrics["settings"]["barangays"]["wer"] == 0.163
    text = eval_results.render_markdown(eval_results.load(path))
    voice = "| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 |"
    assert f"{voice} Language hint | 31 | 19.8% | 17.8% | – | – |" in text  # unlabeled: no places
    assert f"{voice} Hint + barangay names **(the bot)** | 31 | 16.3% | 14.3% | 9 of 10 |" in text


def _broken(change):
    entry = transcribe_entry()
    change(entry["metrics"])
    return entry


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_broken(lambda m: m.update(production="none")), "production must name"),
        (_broken(lambda m: m.update(settings={})), "non-empty"),
        (_broken(lambda m: m.pop("reference_words")), "reference_words"),
        (_broken(lambda m: m["settings"]["barangays"].pop("wer")), "needs exactly"),
        (_broken(lambda m: m["settings"]["barangays"].update(wer=-1)), "non-negative"),
        (_broken(lambda m: m["settings"]["barangays"].update(place_names_heard=11)), "up to"),
        (_broken(lambda m: m["settings"]["barangays"].update(place_names=None)), "up to"),
    ],
)
def test_upsert_refuses_broken_transcribe_metrics(path, entry, message):
    with pytest.raises(eval_results.ResultsError, match=message):
        eval_results.upsert(path, entry)


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


# --- the recorded runs ----------------------------------------------------------------------


def test_the_evaluation_tables_are_generated_from_the_results_file():
    evaluation = (REPO_ROOT / "docs/evaluation.md").read_text(encoding="utf-8")
    assert eval_results.readme_block(evaluation) == eval_results.render_markdown(
        eval_results.load()
    ), "Evaluation tables are stale: paste the output of `uv run python -m app.eval_results`"


def test_the_readme_headlines_are_generated_from_the_results_file():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert eval_results.headline_block(readme) == eval_results.render_headlines(
        eval_results.load()
    ), "README headlines are stale: run `uv run python -m app.eval_results --headlines`"


@pytest.mark.parametrize(
    ("start", "end", "render"),
    [
        (eval_results.MODELS_START, eval_results.MODELS_END, eval_results.render_models),
        (
            eval_results.COMPARISON_START,
            eval_results.COMPARISON_END,
            eval_results.render_comparison,
        ),
    ],
)
def test_the_readme_model_evidence_is_generated_from_the_results_file(start, end, render):
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert eval_results.marked_block(readme, start, end) == render(eval_results.load())


def test_judge_model_tables_follow_changed_results():
    results = copy.deepcopy(eval_results.load())
    photo = results.find("photo_judge", "kill-test-25", "muse-spark-open")
    crop = results.find("crop_photo", "rice-field-19", "muse-spark-open")
    assert photo is not None and crop is not None
    photo.metrics["roof_damage"].update(agreement=15, kappa=0.42)
    crop.metrics["crop_field"]["kappa"] = 0.71
    results.entries[results.entries.index(photo)] = replace(photo, n=photo.n - 1)
    assert f"15 of {photo.n - 1} roof judgments" in eval_results.render_models(results)
    comparison = eval_results.render_comparison(results)
    assert f"0.42 ({photo.n - 1} photos)" in comparison
    assert f"0.71 ({crop.n} photos)" in comparison


@pytest.mark.parametrize("model", ["muse-spark-1.3", "muse-spark-open"])
def test_both_llms_are_measured_on_every_set(model):
    results = eval_results.load()
    for dataset in ("kill-test-25", "synthetic-40"):
        entry = results.find("photo_judge", dataset, model)
        assert entry is not None and entry.sam == "sam-3.1"
        assert (entry.n, entry.errors) == (entry.dataset.n, 0)
    pairs = results.find("pair_judge", "dedupe-pairs-64", model)
    assert pairs is not None and pairs.n == 64

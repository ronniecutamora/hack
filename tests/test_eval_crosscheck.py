"""The Spark-vs-SAM cross-check eval: the free script, its results entry, the README table and
/accuracy. No model is called anywhere here."""

import csv
import json

import pytest

from app import crosscheck, eval_results
from app.accuracy import accuracy_page
from scripts import eval_crosscheck
from tests.test_eval_results import photo_entry

THRESHOLDS = eval_crosscheck.THRESHOLDS
LABEL_COLUMNS = ["file", "floodwater", "roof_damage"]


def result(file, *, flood, sam_flood, roof="none", roofs=2):
    return {
        "file": file,
        "judgment": {"floodwater": flood, "roof_damage": roof},
        "measurement": {
            "roof_count": roofs,
            "floodwater_present": sam_flood,
            "floodwater_fraction": 0.2 if sam_flood else 0.0,
            "thresholds": THRESHOLDS,
        },
    }


def label(file, flood, roof="none"):
    return {"file": file, "floodwater": flood, "roof_damage": roof}


# (label row, the photo's judgment and measurement)
PHOTOS = [
    (label("a.jpg", "1"), dict(flood=True, sam_flood=True)),  # agree
    (label("b.jpg", "0"), dict(flood=False, sam_flood=True)),  # SAM outlines the lake
    (label("c.jpg", "1"), dict(flood=False, sam_flood=True)),  # Spark missed a flood
    (label("d.jpg", "0"), dict(flood=True, sam_flood=False)),  # Spark saw a flood that is not there
    (label("e.jpg", "0", "partial"), dict(flood=False, sam_flood=False, roof="partial", roofs=0)),
    (label("f.jpg", "0", "severe"), dict(flood=False, sam_flood=False, roof="severe", roofs=0)),
]


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "set"
    folder.mkdir()
    with (folder / "labels.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        writer.writerows(row for row, _ in PHOTOS)
    out = tmp_path / "out.json"
    out.write_text(json.dumps([result(row["file"], **r) for row, r in PHOTOS]), encoding="utf-8")
    return folder, out


def test_each_rule_is_counted_with_whose_answer_matched_the_label(folder):
    metrics = eval_crosscheck.score(eval_crosscheck.load(*folder))
    assert (metrics["photos"], metrics["disagreed"]) == (6, 5)  # all but a.jpg
    assert metrics["asked"] == 2  # d (floodwater not outlined) and e (roof not outlined)
    assert metrics["checks"] == {
        "floodwater_not_outlined": {"asks": True, "fired": 1, "spark_right": 0, "sam_right": 1},
        "floodwater_not_judged": {"asks": False, "fired": 2, "spark_right": 1, "sam_right": 1},
        "roof_not_outlined": {"asks": True, "fired": 1, "spark_right": 1, "sam_right": 0},
        "roof_gone": {"asks": False, "fired": 1, "spark_right": 1, "sam_right": 0},
    }
    assert set(metrics["checks"]) == {c.code for c in crosscheck.CHECKS}


def test_outputs_measured_at_other_thresholds_or_without_sam_are_refused(folder, tmp_path):
    labels, out = folder
    rows = json.loads(out.read_text())
    rows[0]["measurement"]["thresholds"] = {**THRESHOLDS, "floodwater": 0.5}
    out.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="thresholds"):
        eval_crosscheck.load(labels, out)
    del rows[0]["measurement"]
    out.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="--sam"):
        eval_crosscheck.load(labels, out)


def test_record_reuses_the_photo_judge_run_and_costs_nothing(folder, tmp_path, capsys):
    labels, out = folder
    target = tmp_path / "results.json"
    raw = eval_results.read_raw()
    datasets = {**raw["datasets"], "six": {**raw["datasets"]["synthetic-40"], "n": 6}}
    source = photo_entry(dataset="six", n=6, spend_usd=0.06)
    target.write_text(json.dumps({**raw, "datasets": datasets, "entries": [source]}))
    args = [str(labels), "--results", str(out), "--model", "muse-spark-1.3"]

    assert eval_crosscheck.main([*args, "--record", str(target)]) == 2  # needs --dataset
    assert eval_crosscheck.main([*args, "--record", str(target), "--dataset", "six"]) == 0
    stored = eval_results.load(target).find("crosscheck", "six", "muse-spark-1.3")
    assert (stored.model_id, stored.sam) == ("muse-spark-1.3", "sam-3.1")
    assert stored.backend == source["backend"]
    assert (stored.n, stored.spend_usd, stored.metrics["asked"]) == (6, 0.0, 2)
    assert "asked the officer about 2" in capsys.readouterr().out

    open_args = [*args[:-1], "muse-spark-open", "--record", str(target), "--dataset", "six"]
    assert eval_crosscheck.main(open_args) == 2  # no Spark Open photo_judge run to reuse


def test_the_folder_must_live_outside_the_repo(folder):
    _labels, out = folder
    inside = str(eval_results.RESULTS_PATH.parent)
    assert eval_crosscheck.main([inside, "--results", str(out), "--model", "muse-spark-1.3"]) == 2


def crosscheck_entry(**metrics_changes):
    metrics = {
        "photos": 25,
        "disagreed": 3,
        "asked": 1,
        "checks": {
            "floodwater_not_outlined": {"asks": True, "fired": 1, "spark_right": 0, "sam_right": 1},
            "roof_gone": {"asks": False, "fired": 2, "spark_right": 2, "sam_right": 0},
        },
    }
    return photo_entry(task="crosscheck", spend_usd=0.0, metrics={**metrics, **metrics_changes})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"asked": 4}, "cannot exceed"),
        ({"disagreed": 26}, "cannot exceed"),
        ({"disagreed": None}, "counts"),
        ({"checks": {}}, "non-empty"),
        ({"checks": {"x": {"asks": True, "fired": 2, "spark_right": 1, "sam_right": 0}}}, "equal"),
        ({"checks": {"x": {"asks": "yes", "fired": 0, "spark_right": 0, "sam_right": 0}}}, "true"),
        ({"checks": {"x": {"fired": 0}}}, "needs exactly"),
    ],
)
def test_broken_crosscheck_metrics_are_refused(changes, message):
    raw = eval_results.read_raw()
    problems = eval_results.validate({**raw, "entries": [crosscheck_entry(**changes)]})
    assert any(message in p for p in problems), problems


def test_the_readme_table_and_accuracy_page_read_the_entry(tmp_path):
    raw = eval_results.read_raw()
    target = tmp_path / "results.json"
    entries = [photo_entry(), crosscheck_entry()]
    target.write_text(json.dumps({**raw, "entries": entries}))
    text = eval_results.render_markdown(eval_results.load(target))
    assert "muse-spark-1.3: they disagreed on 3 of 25 photos, and Review asked about 1." in text
    row = next(line for line in text.splitlines() if "| 1 of 25 |" in line)
    assert "| 3 | 1 of 25 | 1 (Spark 0, SAM 1) | – | – | 2 (Spark 2, SAM 0) |" in row
    assert text.index("SAM 3.1 check each other") > text.index("**Photo judge**")

    page = accuracy_page(target)
    (table,) = [t for t in page.tables if t.task == "crosscheck"]
    cells = {row.label: row.cells[0] for row in table.rows}
    assert cells["Photos where they disagree"].value == "3 of 25"
    assert cells["Photos the officer is asked about"].value == "1 of 25"
    flood = cells["Floodwater: Spark sees it, SAM outlines none (asked)"]
    assert (flood.value, flood.detail) == (
        "1 of 25",
        "the hand label agreed with the agent LLM 0 times, with SAM 1",
    )
    assert cells["Partly damaged roof: SAM outlines no roof (asked)"] is None  # not in that run
    assert [t.task for t in page.tables] == ["photo_judge", "crosscheck"]

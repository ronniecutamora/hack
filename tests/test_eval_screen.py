"""The not-a-report screen's eval: the bundled labeled set, the script and the results entry.
Every model call is a fake."""

import json

import pytest

from app import eval_results
from app.accuracy import accuracy_page
from scripts import eval_screen
from tests.test_screening import ScreenLLM, verdict


def test_the_bundled_set_is_60_labeled_synthetic_messages():
    messages = eval_screen.load_messages(eval_screen.DEFAULT_MESSAGES)
    labels = [m["label"] for m in messages]
    assert (len(messages), labels.count("report"), labels.count("not_report")) == (60, 30, 30)
    results = json.loads(eval_results.RESULTS_PATH.read_text("utf-8"))
    assert results["datasets"][eval_screen.DEFAULT_DATASET]["n"] == 60


def test_messages_outside_the_bundled_set_must_live_outside_the_repo(tmp_path):
    inside = eval_screen.DEFAULT_MESSAGES.parent / "other.json"
    with pytest.raises(ValueError):
        eval_screen.load_messages(inside)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"messages": [{"id": "a", "label": "spam", "text": "x"}]}))
    with pytest.raises(ValueError, match="label"):
        eval_screen.load_messages(bad)


async def test_the_metrics_count_the_real_report_class_and_every_miss():
    messages = [
        {"id": "r1", "label": "report", "text": "baha dito"},
        {"id": "r2", "label": "report", "text": "nasa labas kami"},
        {"id": "n1", "label": "not_report", "text": "hahaha"},
        {"id": "n2", "label": "not_report", "text": "Bili na kayo"},
    ]
    llm = ScreenLLM(screen_reply=verdict("no"))  # every message states nothing: r2 is dropped
    code, metrics, skipped = await eval_screen.evaluate_metrics(messages, llm)
    assert (code, skipped) == (0, 0)
    assert (metrics["tp"], metrics["fn"], metrics["tn"], metrics["fp"]) == (0, 2, 2, 0)
    assert metrics["wrongly_filtered"] == 2 and metrics["rule_filtered"] == 1
    assert metrics["model_calls"] == 3 and metrics["miss_ids"] == ["r1", "r2"]
    # At the spend cap only the free rule's message is still scored; the rest are skipped.
    code, metrics, skipped = await eval_screen.evaluate_metrics(messages, llm, stop=lambda: True)
    assert (code, skipped, metrics["tn"]) == (0, 3, 1)
    # Cache-only: a message whose call missed the cache is skipped, not scored as kept.
    misses = iter([0, 0, 0, 1, 1, 1, 1, 1])
    _, metrics, skipped = await eval_screen.evaluate_metrics(
        messages, llm, misses=lambda: next(misses)
    )
    assert skipped == 1 and metrics["tp"] + metrics["fn"] + metrics["tn"] + metrics["fp"] == 3


def test_the_recorded_spend_counts_every_logged_run_of_the_script(tmp_path):
    from scripts import spend

    log = tmp_path / spend.LOG_NAME
    lines = [
        {"script": "eval_screen", "run_spend_usd": 0.017},
        {"script": "eval_screen", "estimate_usd": 0.2},
        {"script": "eval_dedupe", "run_spend_usd": 0.5},
        {"script": "eval_screen", "run_spend_usd": 0.2913},
    ]
    log.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert spend.script_spend(tmp_path, "eval_screen") == 0.3083
    assert spend.script_spend(tmp_path / "none", "eval_screen") == 0.0


def test_the_screen_eval_entry_is_validated_and_rendered(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(eval_results.RESULTS_PATH.read_text("utf-8"), "utf-8")
    metrics = {
        **{"precision": 0.9, "recall": 1.0, "f1": 0.95, "tp": 30, "fp": 3, "fn": 0, "tn": 27},
        **{"wrongly_filtered": 0, "non_reports_filtered": 27, "rule_filtered": 20},
        **{"model_calls": 9, "unsure": 1, "miss_ids": ["n17"]},
    }
    entry = {
        "task": "screen",
        "dataset": "screen-messages-60",
        "model": "muse-spark-1.3",
        "model_id": "muse-spark-1.3",
        "backend": "Meta Model API",
        "sam": None,
        "date": "2026-11-02",
        "commit": "abcdef123456",
        "n": 60,
        "errors": 0,
        "spend_usd": 0.25,
        "metrics": metrics,
    }
    eval_results.upsert(path, entry)
    with pytest.raises(eval_results.ResultsError, match="metrics miss"):
        eval_results.upsert(path, {**entry, "metrics": {"precision": 1.0}})
    results = eval_results.load(path)
    assert "| 0 | 27 of 30 | 20 | 9 | 2026-11-02 |" in eval_results.render_markdown(results)
    table = next(t for t in accuracy_page(path).tables if t.task == "screen")
    assert table.rows[0].label == "Real reports wrongly screened out"
    assert table.rows[0].cells[0].value == "0" and not table.rows[0].cells[0].weak

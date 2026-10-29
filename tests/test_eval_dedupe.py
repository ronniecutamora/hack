import json

import pytest

from app.evaluation import binary_scores
from scripts import eval_dedupe
from tests.test_incidents import FakeJudge


def test_synthetic_fixture_balance_and_hard_cases():
    pairs = eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS)
    assert len(pairs) == 64
    assert sum(p["label"] == "same" for p in pairs) == 24
    corrections = [p for p in pairs if p["new_report"]["facts"]["is_correction"]]
    assert len(corrections) == 8 and all(p["label"] == "same" for p in corrections)
    assert len({p["id"] for p in pairs}) == 64
    assert sum("unrelated hazard" in p["scenario"] for p in pairs) == 12
    assert sum("different purok" in p["scenario"] for p in pairs) == 12
    near_identical = [p for p in pairs if "near-identical wording" in p["scenario"]]
    assert len(near_identical) == 4 and all(p["label"] == "different" for p in near_identical)
    for pair in near_identical:
        old = pair["incident_context"][0]
        new = pair["new_report"]
        assert old["barangay"] == new["barangay"]
        assert old["facts"]["hazards"] == new["facts"]["hazards"]
        assert (
            sum(
                a != b
                for a, b in zip(
                    old["facts"]["place_detail"].split(),
                    new["facts"]["place_detail"].split(),
                    strict=True,
                )
            )
            == 1
        )


def test_exact_match_baseline_is_imperfect_on_synthetic_fixture():
    pairs = eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS)
    labels, predictions = [], []
    for pair in pairs:
        facts = pair["new_report"]["facts"]
        labels.append(pair["label"] == "same")
        predictions.append(
            any(
                c["barangay"] == pair["new_report"]["barangay"]
                and c["facts"]["place_detail"] == facts["place_detail"]
                and c["facts"]["hazards"] == facts["hazards"]
                for c in pair["incident_context"]
            )
        )
    assert sum(a == b for a, b in zip(labels, predictions, strict=True)) < len(pairs)
    assert binary_scores(labels, predictions).f1 < 1.0
    assert all(
        c["facts"]["place_detail"] != p["new_report"]["facts"]["place_detail"]
        for p in pairs
        if p["label"] == "same"
        for c in p["incident_context"]
    )


async def test_low_confidence_different_is_not_counted_as_officer_review(capsys):
    pair = next(
        p for p in eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS) if p["label"] == "different"
    )
    judge = FakeJudge({"decision": "different", "confidence": 0.8, "reason": "Different puroks."})
    assert await eval_dedupe.evaluate([pair], judge, threshold=0.85) == 0
    output = capsys.readouterr().out
    assert "0.0% (0/1)" in output
    assert "miss" not in output


async def test_cli_fixture_with_fake_judge_prints_precision_recall_and_f1(
    monkeypatch, capsys, settings
):
    monkeypatch.setattr(eval_dedupe, "get_settings", lambda: settings)  # spend ledger in tmp
    pairs = eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS)
    judge = FakeJudge(
        *[
            {"decision": p["label"], "confidence": 0.99, "reason": "Synthetic expected answer."}
            for p in pairs
        ]
    )
    monkeypatch.setattr(eval_dedupe, "make_pair_judge", lambda settings: judge)
    assert await eval_dedupe.run(eval_dedupe.parse_args(["--judge", "muse-glimmer", "--yes"])) == 0
    output = capsys.readouterr().out
    assert "Precision: 1.000  Recall: 1.000  F1: 1.000" in output
    assert "TP=24 FP=0 FN=0 TN=40" in output
    assert "Unsure rate" in output and "0/64" in output
    assert "unverified" in output
    assert len(judge.calls) == 64 and judge.closed


async def test_eval_reports_misses_and_unsure_and_applies_merge_threshold(capsys):
    pairs = eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS)[:3]
    judge = FakeJudge(
        {"decision": "same", "confidence": 0.99, "reason": "Same place."},
        {"decision": "unsure", "confidence": 0.5, "reason": "Place is unclear."},
        {"decision": "same", "confidence": 0.6, "reason": "Low certainty."},
    )
    assert await eval_dedupe.evaluate(pairs, judge, threshold=0.85) == 0
    output = capsys.readouterr().out
    assert "Precision: 1.000  Recall: 0.333  F1: 0.500" in output
    assert "66.7%" in output and "miss pair-02" in output and "miss pair-03" in output


async def test_declining_cost_confirmation_does_not_construct_a_client(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    def unexpected(settings):
        pytest.fail("no client should be created before confirmation")

    monkeypatch.setattr(eval_dedupe, "make_pair_judge", unexpected)
    assert await eval_dedupe.run(eval_dedupe.parse_args([])) == 1


async def test_eval_reports_model_failure_without_switching_models(capsys):
    pairs = eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS)[:1]
    assert (
        await eval_dedupe.evaluate(pairs, FakeJudge(RuntimeError("not deployed")), threshold=0.85)
        == 1
    )
    assert "not deployed" in capsys.readouterr().out


def test_private_data_must_live_outside_repo_and_bad_labels_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS.with_name("private_pairs.json"))
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps([{"label": "invalid"}]))
    with pytest.raises(ValueError, match="same/different"):
        eval_dedupe.load_pairs(path)
    assert eval_dedupe.parse_args(["--llm", "muse-spark-open"]).judge == "muse-spark-open"

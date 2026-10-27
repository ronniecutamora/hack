import json

import pytest

from app.evaluation import REPO_ROOT
from scripts import eval_facts, eval_vision
from tests.test_assessment import FakeLLM, FakeSam, facts, jpeg


class Closable:
    async def aclose(self):
        pass


@pytest.fixture
def photo_folder(tmp_path):
    (tmp_path / "images").mkdir()
    rows = ["file,roof,fallen_tree,floodwater,debris,tarpaulin,roof_damage"]
    for name, flood, damage in (("a.jpg", 1, "partial"), ("b.jpg", 0, "severe")):
        (tmp_path / "images" / name).write_bytes(jpeg())
        rows.append(f"{name},1,0,{flood},1,0,{damage}")
    (tmp_path / "labels.csv").write_text("\n".join(rows) + "\n")
    return tmp_path


def fake_llm_factory(llm):
    class Llm(FakeLLM, Closable):
        pass

    instance = Llm(llm.facts_reply)
    return lambda settings, recorder=None: instance


def test_vision_eval_scores_judge_and_sam(photo_folder, monkeypatch, settings, capsys):
    monkeypatch.setattr(eval_vision, "get_settings", lambda: settings)
    monkeypatch.setattr(eval_vision, "make_agent_llm", fake_llm_factory(FakeLLM()))

    class Sam(FakeSam, Closable):
        pass

    monkeypatch.setattr(eval_vision, "make_sam", lambda settings: Sam())
    out_file = photo_folder / "out.json"

    code = eval_vision.main([str(photo_folder), "--sam", "--yes", "--out", str(out_file)])

    out = capsys.readouterr().out
    assert code == 0
    assert "scored 2 of 2 photos with fake-llm" in out
    assert "agreement 1/2" in out  # partial right, severe wrong
    assert "floodwater         F1 0.67" in out  # predicted flood on both, one is labelled
    assert "SAM roof" in out
    assert len(json.loads(out_file.read_text())) == 2


def test_vision_eval_asks_before_spending(photo_folder, monkeypatch, settings):
    monkeypatch.setattr(eval_vision, "get_settings", lambda: settings)
    monkeypatch.setattr(eval_vision, "make_agent_llm", fake_llm_factory(FakeLLM()))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert eval_vision.main([str(photo_folder)]) == 1


@pytest.mark.parametrize("script", [eval_vision, eval_facts])
def test_eval_scripts_refuse_data_inside_the_repo(script, capsys):
    assert script.main([str(REPO_ROOT / "tests"), "--yes"]) == 2
    assert "outside the repository" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "expected", "got", "ok"),
    [
        ("barangay_mentioned", "Daplisaya", "Barangay Daplisaya", True),
        ("barangay_mentioned", "Daplisaya", "Datplisaya", False),
        ("barangay_mentioned", "Sto. Niño", "santo nino", True),
        ("crop_area_hectares", 2.5, 2.5, True),
        ("crop_area_hectares", 2.5, None, False),
        ("houses_flooded", 23, 23, True),
        ("injured", 0, None, False),
        ("infrastructure", ["road"], ["road"], True),
        ("infrastructure", ["road"], ["road", "school"], False),
        ("is_correction", True, True, True),
        ("crop", "corn", "rice", False),
    ],
)
def test_fact_matching_rules(field, expected, got, ok):
    assert eval_facts.matches(field, expected, got) is ok


def test_facts_eval_end_to_end(tmp_path, monkeypatch, settings, capsys):
    (tmp_path / "3.ogg").write_bytes(b"OggS")
    labels = {"3": {"houses_flooded": 23, "flood_depth": "knee", "barangay_mentioned": "Casile"}}
    (tmp_path / "facts.json").write_text(json.dumps(labels))
    reply = facts(houses_flooded=23, flood_depth="waist", barangay_mentioned="Casile")
    monkeypatch.setattr(eval_facts, "get_settings", lambda: settings)
    monkeypatch.setattr(eval_facts, "make_agent_llm", fake_llm_factory(FakeLLM(reply)))
    monkeypatch.setattr(eval_facts, "make_transcriber", lambda settings: Closable())
    seen = {}

    async def fake_transcribe(transcriber, audio, *, language_bias, keywords):
        seen["keywords"] = keywords
        return "may twenty three twenty three bahay sa Casile"

    monkeypatch.setattr(eval_facts, "transcribe_file", fake_transcribe)

    assert eval_facts.main([str(tmp_path), "--keywords", "barangays", "--yes"]) == 0

    out = capsys.readouterr().out
    assert "note 3: 2/3" in out
    assert "flood_depth: expected 'knee', got 'waist'" in out
    assert "fake-llm: 2/3 facts right (67%)" in out
    assert "Casile" in seen["keywords"]

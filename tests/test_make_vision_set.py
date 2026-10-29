"""The synthetic vision set generator: scene table, idempotent generation, label review."""

import base64
import csv
import io
import json
from collections import Counter

import httpx
import pytest
import respx
from PIL import Image

from app.evaluation import REPO_ROOT
from app.meta.image import URL as IMAGE_URL
from scripts import make_vision_set, spend
from scripts.eval_vision import load_labels


def image_reply():
    buffer = io.BytesIO()
    Image.new("RGB", (320, 200), (90, 120, 140)).save(buffer, "JPEG")
    return {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}


@pytest.fixture
def small_table(tmp_path, monkeypatch):
    scenes = make_vision_set.load_scenes()[:3]
    path = tmp_path / "scenes.json"
    path.write_text(json.dumps(scenes))
    monkeypatch.setattr(make_vision_set, "SCENES_PATH", path)
    monkeypatch.setattr(make_vision_set, "load_scenes", lambda: scenes)
    return scenes


@pytest.fixture
def use_settings(monkeypatch, settings):
    monkeypatch.setattr(make_vision_set, "get_settings", lambda: settings)
    return settings


def read_rows(folder):
    with (folder / "labels.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_the_scene_table_covers_every_label_both_ways():
    scenes = make_vision_set.load_scenes()
    assert 35 <= len(scenes) <= 45
    categories = Counter(s["category"] for s in scenes)
    for category in ("negative", "roof_partial", "roof_severe", "flood", "debris", "fallen_tree",
                     "tarpaulin", "fish_pens", "hard_case"):  # fmt: skip
        assert categories[category] >= 2, category
    for label in ("roof", "fallen_tree", "floodwater", "debris", "tarpaulin", "fish_cage"):
        values = {s["labels"][label] for s in scenes}
        assert values == {0, 1}, label
    assert {s["labels"]["roof_damage"] for s in scenes} == {"none", "partial", "severe", "unknown"}
    # Hard negatives: water in view that is not a flood, tarps on roofs that are not damaged.
    assert scenes_by_id(scenes)["hard-wet-road"]["labels"]["floodwater"] == 0
    tarp = scenes_by_id(scenes)["tarp-intact-roof"]["labels"]
    assert (tarp["tarpaulin"], tarp["roof_damage"]) == (1, "none")


def scenes_by_id(scenes):
    return {s["id"]: s for s in scenes}


def test_scene_labels_are_checked(tmp_path):
    bad = make_vision_set.load_scenes()[:1]
    bad[0]["labels"]["roof_damage"] = "total"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="roof_damage"):
        make_vision_set.load_scenes(path)


@respx.mock
def test_generation_is_idempotent_and_keeps_reviewed_labels(
    tmp_path, small_table, use_settings, capsys
):
    route = respx.post(IMAGE_URL).mock(return_value=httpx.Response(200, json=image_reply()))
    folder = tmp_path / "vision"
    assert make_vision_set.main([str(folder), "--yes"]) == 0
    assert route.call_count == 3
    rows = read_rows(folder)
    assert [r["file"] for r in rows] == [make_vision_set.image_name(s) for s in small_table]
    assert all(r["label_notes"] == "" for r in rows)
    assert len(load_labels(folder, None)) == 3  # eval_vision reads the set as written
    for scene, row in zip(small_table, rows, strict=True):
        assert row["roof_damage"] == scene["labels"]["roof_damage"]
        with Image.open(folder / "images" / row["file"]) as image:
            assert image.size == (320, 200)  # stamped, re-encoded JPEG
    assert spend.SpendLedger(use_settings.data_dir, 5, script="t").spent() == pytest.approx(0.03)

    # A reviewer corrects a label; a re-run generates nothing and keeps the correction.
    rows[0]["debris"], rows[0]["label_notes"] = "1", "debris: broken sheets visible in the yard"
    make_vision_set.write_labels(folder, rows)

    def no_questions(prompt):
        pytest.fail("nothing to generate, so nothing to confirm")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("builtins.input", no_questions)
        assert make_vision_set.main([str(folder)]) == 0
    assert route.call_count == 3
    assert read_rows(folder)[0]["label_notes"].startswith("debris:")
    assert "1 with review notes" in capsys.readouterr().out


@respx.mock
def test_a_changed_scene_gets_a_new_image_and_fresh_labels(tmp_path, small_table, use_settings):
    respx.post(IMAGE_URL).mock(return_value=httpx.Response(200, json=image_reply()))
    folder = tmp_path / "vision"
    assert make_vision_set.main([str(folder), "--yes"]) == 0
    old = make_vision_set.image_name(small_table[0])
    small_table[0]["scene"] += " Late afternoon."
    assert make_vision_set.main([str(folder), "--yes"]) == 0
    files = [r["file"] for r in read_rows(folder)]
    assert old not in files and make_vision_set.image_name(small_table[0]) in files


def test_generation_refuses_a_folder_inside_the_repo(capsys):
    assert make_vision_set.main([str(REPO_ROOT / "var" / "vision"), "--yes"]) == 2
    assert "outside the repository" in capsys.readouterr().err
    assert not (REPO_ROOT / "var" / "vision").exists()


def test_generation_respects_the_spend_cap(tmp_path, small_table, use_settings, capsys):
    with respx.mock:
        route = respx.post(IMAGE_URL)
        assert make_vision_set.main([str(tmp_path / "v"), "--yes", "--spend-cap", "0.02"]) == 1
        assert not route.called
    assert "Refusing" in capsys.readouterr().out

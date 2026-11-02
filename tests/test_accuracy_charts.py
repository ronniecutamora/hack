"""The /accuracy page's charts (app.charts, drawn by app.accuracy from the results file)."""

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.accuracy import WEAK_BELOW, accuracy_page
from app.charts import MARGIN, VALUE_ROOM, WIDTH
from app.eval_results import RESULTS_PATH, load
from app.main import create_app

REAL = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def client(settings):
    # The Accuracy page belongs to Technical mode (app.modes).
    technical = settings.model_copy(update={"dashboard_mode": "technical"})
    with TestClient(create_app(technical), base_url="http://localhost") as client:
        yield client


def page(client, path) -> str:
    client.app.state.results_path = path
    response = client.get("/accuracy")
    assert response.status_code == 200
    return response.text


def write(tmp_path, raw) -> Path:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    load(path)  # still a valid results file
    return path


def figure(html: str, chart_id: str) -> str:
    """The <figure> of one chart, from its id to its end."""
    start = html.index(f'<figure class="chart" id="{chart_id}"')
    return html[start : html.index("</figure>", start)]


def entry(raw, task, dataset, model):
    return next(
        e for e in raw["entries"]
        if (e["task"], e["dataset"], e["model"]) == (task, dataset, model)
    )  # fmt: skip


def svgs(html: str) -> list[str]:
    return re.findall(r"<svg class=\"chart__svg\".*?</svg>", html, re.S)


def test_every_chart_renders_from_the_committed_results_file(client):
    html = page(client, RESULTS_PATH)
    results = load(RESULTS_PATH)

    for e in results.for_task("photo_judge"):
        assert f'id="chart-roof-{e.dataset.id}"' in html
        assert f'id="chart-flags-{e.dataset.id}"' in html
    for task, kind in (("pair_judge", "dedupe"), ("screen", "screen")):
        for e in results.for_task(task):
            assert f'id="chart-{kind}-{e.dataset.id}"' in html
    for e in results.for_task("transcribe"):
        assert f'id="voice-{e.dataset.id}"' in html
        assert f'id="chart-wer-{e.dataset.id}"' in html
        assert f'id="chart-places-{e.dataset.id}"' in html

    # Every chart is an accessible image, scales with its box and has its numbers in a table.
    drawn = svgs(html)
    assert drawn
    for svg in drawn:
        assert 'role="img"' in svg and "viewBox=" in svg and 'width="100%"' in svg
        name = re.search(r'aria-labelledby="([^" ]+)', svg).group(1)
        assert f'<title id="{name}">' in svg
    assert "<script" not in "".join(drawn)
    assert html.count("Show the numbers") >= 3
    # The real-photo matrix of each Spark model, side by side, Spark 1.3 first.
    roof = figure(html, "chart-roof-kill-test-25")
    assert roof.index("Muse Spark 1.3") < roof.index("Muse Spark Open")
    assert roof.count('<svg class="chart__svg"') == 2


def test_the_confusion_matrix_follows_the_counts_in_the_file(client, tmp_path):
    raw = copy.deepcopy(REAL)
    spark = entry(raw, "photo_judge", "kill-test-25", "muse-spark-1.3")
    severe = spark["metrics"]["roof_damage"]["confusion"]["severe"]
    before = figure(page(client, write(tmp_path, raw)), "chart-roof-kill-test-25")
    assert f"Hand label Severe, AI said Severe: {severe['severe']} photos" in before

    severe["severe"], severe["partial"] = 5, 2
    after = figure(page(client, write(tmp_path, raw)), "chart-roof-kill-test-25")

    assert "Hand label Severe, AI said Severe: 5 photos" in after
    assert "Hand label Severe, AI said Partial: 2 photos" in after
    assert "Hand label Severe, AI said Severe: 7 photos" not in after.split("Muse Spark Open")[0]
    assert after != before


def test_a_changed_f1_moves_its_bar_and_a_weak_one_is_marked(client, tmp_path):
    raw = copy.deepcopy(REAL)
    spark = entry(raw, "photo_judge", "kill-test-25", "muse-spark-1.3")
    spark["metrics"]["flags"]["debris"]["f1"] = 0.42
    html = page(client, write(tmp_path, raw))

    flags = figure(html, "chart-flags-kill-test-25")
    spark_panel = flags.split("Muse Spark Open")[0]
    assert "Debris: F1 0.42" in spark_panel
    assert re.search(
        r'class="chart__value[^"]*">0\.42</text><rect[^>]*class="chart__weak-chip"', flags
    )
    assert WEAK_BELOW > 0.42
    # The bar is 0.42 of the plot: a path of that width from the baseline.
    plot = WIDTH - MARGIN - VALUE_ROOM
    width = round(0.42 * plot - 4, 2)
    assert f"h{width:g}a4 4" in spark_panel


def test_the_screen_and_voice_charts_follow_the_file(client, tmp_path):
    raw = copy.deepcopy(REAL)
    screen = entry(raw, "screen", "screen-messages-60", "muse-spark-1.3")["metrics"]
    screen.update(tp=29, fn=1, wrongly_filtered=1, recall=0.9667)
    voice = entry(raw, "transcribe", "voice-notes-8", "muse-voice-transcribe-1.0")["metrics"]
    voice["settings"]["barangays"].update(wer=0.31, place_names_heard=7)
    html = page(client, write(tmp_path, raw))

    chart = figure(html, "chart-screen-screen-messages-60")
    assert "1 real report screened out" in chart and "29 real reports kept" in chart
    assert '<span class="chip warn">weak</span>' in chart  # a real report was dropped
    wer = figure(html, "chart-wer-voice-notes-8")
    assert "Hint + Biñan&#39;s barangay names: 31% of words wrong" in wer
    assert "Hint + Biñan&#39;s barangay names (the bot uses this)" in wer
    assert ">40%</text>" in wer  # the axis grows to fit the larger error rate
    places = figure(html, "chart-places-voice-notes-8")
    assert "7 of 10 place names heard right" in places


@pytest.mark.parametrize(
    ("drop", "gone", "kept"),
    [
        (("screen", None), "chart-screen-screen-messages-60", "chart-dedupe-dedupe-pairs-64"),
        (("transcribe", None), 'id="voice-voice-notes-8"', "chart-screen-screen-messages-60"),
        (("pair_judge", None), "chart-dedupe-dedupe-pairs-64", "chart-roof-kill-test-25"),
        (("photo_judge", "kill-test-25"), "chart-roof-kill-test-25", "chart-roof-synthetic-40"),
    ],
)
def test_a_missing_entry_hides_its_chart_without_an_error(client, tmp_path, drop, gone, kept):
    raw = copy.deepcopy(REAL)
    task, dataset = drop
    raw["entries"] = [
        e for e in raw["entries"]
        if not (e["task"] == task and dataset in (None, e["dataset"]))
    ]  # fmt: skip
    html = page(client, write(tmp_path, raw))

    assert gone not in html and kept in html


def test_an_empty_confusion_or_place_labels_hide_only_that_chart(client, tmp_path):
    raw = copy.deepcopy(REAL)
    for e in raw["entries"]:
        if e["task"] == "photo_judge" and e["dataset"] == "kill-test-25":
            e["metrics"]["roof_damage"]["confusion"] = {}
        if e["task"] == "transcribe":
            for setting in e["metrics"]["settings"].values():
                setting.update(place_names_heard=None, place_names=None)
    path = write(tmp_path, raw)
    html = page(client, path)

    assert "chart-roof-kill-test-25" not in html and "chart-flags-kill-test-25" in html
    assert "chart-places-voice-notes-8" not in html and "chart-wer-voice-notes-8" in html
    assert accuracy_page(path).problem is None


def test_no_results_means_no_charts(client, tmp_path):
    html = page(client, tmp_path / "missing.json")
    assert "chart__svg" not in html and "No accuracy results are recorded yet" in html

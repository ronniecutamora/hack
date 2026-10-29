"""The /accuracy page, rendered from a results file (app.accuracy, app.eval_results)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.accuracy import accuracy_page
from app.eval_results import RESULTS_PATH, load
from app.main import create_app
from tests.test_auth import HOST, HTML, public_settings, sign_in  # noqa: F401 (a fixture)


def scores(f1, tp=5, fp=1, fn=1, tn=10):
    return {"precision": f1, "recall": f1, "f1": f1, "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def photo_metrics(kappa, fallen_tree_f1, sam=True):
    return {
        "roof_damage": {"kappa": kappa, "agreement": 17, "confusion": {"none": {"none": 3}}},
        "flags": {
            "floodwater": scores(0.91),
            "debris": scores(0.81),
            "tarpaulin": scores(0.71),
            "fallen_tree": scores(fallen_tree_f1),
        },
        "sam": {"roof": scores(0.97), "floodwater": scores(0.44)} if sam else None,
    }


def entry(task, dataset, model, n, metrics, *, commit="abc1234def", spend=0.25):
    model_ids = {
        "muse-spark-1.3": "muse-spark-1.3",
        "muse-spark-open": "meta-models/muse-spark-open",
    }
    return {
        "task": task,
        "dataset": dataset,
        "model": model,
        "model_id": model_ids[model],
        "backend": "Meta Model API" if model == "muse-spark-1.3" else "Baseten",
        "sam": "sam-3.1" if task == "photo_judge" and metrics.get("sam") else None,
        "date": "2026-10-29" if model == "muse-spark-open" else "2026-10-30",
        "commit": commit,
        "n": n,
        "errors": 1 if model == "muse-spark-open" else 0,
        "spend_usd": spend,
        "metrics": metrics,
    }


PAIR = {**scores(0.98), "threshold": 0.85, "review_rate": 0.0625, "reviews": 4, "miss_ids": []}
FIXTURE = {
    "schema": 1,
    "datasets": {
        "real-photos": {
            "title": "Real typhoon photos",
            "provenance": "real",
            "source": "20 CC BY photos of typhoon Haiyan (fixture source text)",
            "label_source": "hand labels, fixture",
            "n": 20,
            "location": "outside the repo",
        },
        "made-photos": {
            "title": "Synthetic photos",
            "provenance": "synthetic",
            "source": "12 Muse Image renders (fixture source text)",
            "label_source": "designed labels, fixture",
            "n": 12,
            "location": "outside the repo",
        },
        "pairs": {
            "title": "Synthetic report pairs",
            "provenance": "synthetic",
            "source": "64 hand-written pairs (fixture source text)",
            "label_source": "written with each pair, fixture",
            "n": 64,
            "location": "tests/fixtures",
        },
    },
    "entries": [
        entry("pair_judge", "pairs", "muse-spark-open", 63, PAIR, spend=0.31),
        entry("pair_judge", "pairs", "muse-spark-1.3", 64, PAIR, spend=0.25),
        entry("photo_judge", "made-photos", "muse-spark-1.3", 12, photo_metrics(0.84, 1.0)),
        entry("photo_judge", "real-photos", "muse-spark-open", 19, photo_metrics(0.56, 0.33)),
        entry("photo_judge", "real-photos", "muse-spark-1.3", 20, photo_metrics(0.66, 0.57, False)),
    ],
}


@pytest.fixture
def results_file(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return path


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client


def page(client, path) -> str:
    client.app.state.results_path = path
    response = client.get("/accuracy")
    assert response.status_code == 200
    return response.text


def test_every_entry_is_shown_with_its_n_provenance_date_commit_and_spend(client, results_file):
    html = page(client, results_file)

    for dataset in FIXTURE["datasets"].values():
        assert dataset["source"] in html and dataset["label_source"] in html
    assert html.count("Real data</span>") == 1 and html.count("Synthetic data</span>") == 2
    for e in FIXTURE["entries"]:
        section = html[html.index(f'id="{e["task"]}-{e["dataset"]}"') :]
        section = section[: section.index("</section>")]
        assert f"<code>{e['model_id']}</code>" in section
        assert f'<strong class="accuracy__value">{e["n"]}</strong>' in section
        assert e["date"] in section and f"commit {e['commit']}" in section
        assert f"${e['spend_usd']:.2f}" in section and e["backend"] in section
    assert "1 failed" in html  # Spark Open's errors are reported next to its n
    assert "$1.31 spent on the recorded runs" in html and "latest run 2026-10-30" in html


def test_tables_go_real_before_synthetic_and_spark_13_before_spark_open(client, results_file):
    html = page(client, results_file)

    order = [html.index(f'id="{key}"') for key in (
        "photo_judge-real-photos", "photo_judge-made-photos", "pair_judge-pairs"
    )]  # fmt: skip
    assert order == sorted(order)
    real = html[order[0] : order[1]]
    assert real.index("Muse Spark 1.3") < real.index("Muse Spark Open")
    pairs = html[order[2] :]
    assert pairs.index("<code>muse-spark-1.3</code>") < pairs.index(
        "<code>meta-models/muse-spark-open</code>"
    )


def test_scores_have_plain_captions_and_weak_ones_are_marked(client, results_file):
    html = page(client, results_file)

    real = html[html.index('id="photo_judge-real-photos"') : html.index('id="photo_judge-made')]
    assert (
        '<strong class="accuracy__value">0.56</strong> <span class="chip warn">weak</span>' in real
    )
    assert (
        '<strong class="accuracy__value">0.33</strong> <span class="chip warn">weak</span>' in real
    )
    assert "not scored" in real  # Spark 1.3's real-photo run has no SAM scores
    assert "17 of 20 photos given the same level as the hand label" in real
    assert "precision 0.91, recall 0.91: 5 found, 1 false alarm, 1 missed" in real
    assert "not the loss figure" in real and "confirm every fallen tree" in real
    assert "Laguna de Bay" in real
    assert "Pairs below the merge threshold (0.85)" in html and "6%" in html and "4 of 64" in html


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (None, False),  # no file at all
        (json.dumps({**FIXTURE, "entries": []}), False),
        ("{not json", True),
        (json.dumps({"schema": 99}), True),
    ],
)
def test_a_missing_empty_or_broken_file_is_a_friendly_empty_state(
    client, tmp_path, content, problem
):
    path = tmp_path / "results.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    html = page(client, path)

    assert "No accuracy results are recorded yet" in html
    assert ("could not be read" in html) is problem
    assert "accuracy__table" not in html


def test_the_committed_results_file_renders_every_entry(client):
    results = load(RESULTS_PATH)
    view = accuracy_page(RESULTS_PATH)
    assert view.problem is None
    assert sum(len(t.columns) for t in view.tables) == len(results.entries)

    html = page(client, RESULTS_PATH)

    for e in results.entries:
        assert f'id="{e.task}-{e.dataset.id}"' in html and f"commit {e.commit[:12]}" in html


def test_accuracy_is_in_the_navigation_and_linked_from_the_overview(client, results_file):
    client.app.state.results_path = results_file
    assert '<a href="/accuracy" aria-current="page">Accuracy</a>' in client.get("/accuracy").text
    assert '<a href="/accuracy">How accurate is the agent?</a>' in client.get("/").text


def test_accuracy_is_private_behind_the_officer_login(public_settings):  # noqa: F811
    with TestClient(create_app(public_settings), base_url=f"https://{HOST}") as officer:
        response = officer.get("/accuracy", headers=HTML, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=/accuracy"
        sign_in(officer)
        assert officer.get("/accuracy").status_code == 200


def test_the_fixture_is_a_valid_results_file(results_file):
    assert len(load(results_file).entries) == len(FIXTURE["entries"])

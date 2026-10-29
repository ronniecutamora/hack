"""The eval scripts' spend cap, cache-only cost estimates and --record upserts (all mocked)."""

import json
import shutil

import httpx
import pytest
import respx
from sqlmodel import Session, select

from app import eval_results
from app.cache import ResponseCache
from app.meta.base import CallRecord
from app.meta.spark import URL as SPARK_URL
from app.meta.spark import SparkClient
from app.models import ModelCall
from scripts import eval_dedupe, eval_vision, spend
from tests.test_assessment import JUDGMENT, FakeLLM, FakeSam, jpeg
from tests.test_eval_scripts import Closable, fake_llm_factory
from tests.test_incidents import FakeJudge


def write_photos(folder, count):
    (folder / "images").mkdir(parents=True)
    rows = ["file,roof,fallen_tree,floodwater,debris,tarpaulin,roof_damage"]
    for i in range(count):
        # Different sizes give different bytes, so every photo has its own cache key.
        (folder / "images" / f"p{i}.jpg").write_bytes(jpeg((100 + i, 50)))
        rows.append(f"p{i}.jpg,1,0,1,1,0,partial")
    (folder / "labels.csv").write_text("\n".join(rows) + "\n")
    return folder


def spark_reply(completion_tokens=2000):
    return {
        "choices": [{"message": {"content": json.dumps(JUDGMENT)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": completion_tokens},
    }


@pytest.fixture
def photos(tmp_path):
    return write_photos(tmp_path / "photos", 2)


@pytest.fixture
def results_file(tmp_path):
    path = tmp_path / "results.json"
    raw = eval_results.read_raw()
    path.write_text(json.dumps({**raw, "entries": []}))
    return path


def use_settings(monkeypatch, settings, *modules):
    for module in modules:
        monkeypatch.setattr(module, "get_settings", lambda: settings)


def real_spark(settings, monkeypatch):
    client = SparkClient(api_key="k", cache=ResponseCache(settings.cache_dir), backoff_s=0)
    monkeypatch.setattr(eval_vision, "make_agent_llm", lambda settings, recorder=None: client)
    return client


def prefill(settings, usd):
    ledger = spend.SpendLedger(settings.data_dir, 5.0, script="test")
    ledger.record(CallRecord("earlier", 1, False, usd))
    ledger.close()


# --- the cap --------------------------------------------------------------------------------


def test_the_cap_refuses_a_run_whose_estimate_would_pass_it(photos, monkeypatch, settings, capsys):
    use_settings(monkeypatch, settings, eval_vision)
    llm = FakeLLM()
    monkeypatch.setattr(eval_vision, "make_agent_llm", lambda settings, recorder=None: llm)
    llm.aclose = Closable().aclose
    prefill(settings, 4.99)

    assert eval_vision.main([str(photos), "--yes"]) == 1

    out = capsys.readouterr().out
    assert "Estimated new spend: $0.02" in out and "Refusing" in out
    assert llm.calls == []  # nothing was paid for
    log = (settings.data_dir / spend.LOG_NAME).read_text().splitlines()
    refused = json.loads(log[-2])  # the last line is the run's closing summary
    assert refused["approved"] is False and refused["estimate_usd"] == pytest.approx(0.024)


@respx.mock
def test_a_cached_rerun_is_estimated_free_and_never_asks(photos, monkeypatch, settings, capsys):
    use_settings(monkeypatch, settings, eval_vision)
    route = respx.post(SPARK_URL).mock(return_value=httpx.Response(200, json=spark_reply()))
    real_spark(settings, monkeypatch)
    assert eval_vision.main([str(photos), "--yes"]) == 0
    assert route.call_count == 2
    assert "2 uncached judge call(s)" in capsys.readouterr().out

    # The ledger logged the real spend: 2 calls x (1000 in + 2000 out tokens at $1.25/$4.25).
    assert spend.SpendLedger(settings.data_dir, 5, script="t").spent() == pytest.approx(0.0195)

    def no_questions(prompt):
        pytest.fail("a free run must not ask")

    monkeypatch.setattr("builtins.input", no_questions)
    real_spark(settings, monkeypatch)
    # The spend is already past this cap, but a fully cached run costs nothing, so it may run.
    assert eval_vision.main([str(photos), "--spend-cap", "0.01"]) == 0
    assert route.call_count == 2
    out = capsys.readouterr().out
    assert "Estimated new spend: $0.00 (0 uncached judge call(s)" in out
    assert "scored 2 of 2" in out


@respx.mock
def test_a_run_stops_starting_paid_work_once_the_cap_is_reached(
    tmp_path, monkeypatch, settings, capsys
):
    photos = write_photos(tmp_path / "six", 6)
    use_settings(monkeypatch, settings, eval_vision)
    # Each reply costs $8.50 (2M output tokens), so the first batch of 4 passes a $5 cap.
    respx.post(SPARK_URL).mock(return_value=httpx.Response(200, json=spark_reply(2_000_000)))
    real_spark(settings, monkeypatch)

    assert eval_vision.main([str(photos), "--yes", "--spend-cap", "5"]) == 0

    out = capsys.readouterr().out
    assert out.count("skipped: the spend cap is reached") == 2
    assert "scored 4 of 6" in out


# --- --record -------------------------------------------------------------------------------


def test_record_upserts_one_entry_and_accumulates_spend(
    photos, results_file, monkeypatch, settings
):
    use_settings(monkeypatch, settings, eval_vision)
    monkeypatch.setattr(eval_vision, "make_agent_llm", fake_llm_factory(FakeLLM()))

    class Sam(FakeSam, Closable):
        pass

    monkeypatch.setattr(eval_vision, "make_sam", lambda settings: Sam())
    args = [str(photos), "--sam", "--yes", "--record", str(results_file)]
    for _ in range(2):
        assert eval_vision.main([*args, "--dataset", "kill-test-25"]) == 0

    results = eval_results.load(results_file)
    assert len(results.entries) == 1
    entry = results.find("photo_judge", "kill-test-25", "muse-spark-1.3")
    assert entry is not None and entry.n == 2 and entry.errors == 0
    assert entry.model_id == "fake-llm" and entry.sam == "sam-3.1"
    assert entry.dataset.provenance == "real"
    assert entry.metrics["roof_damage"]["agreement"] == 2
    assert entry.score("flags", "floodwater")["f1"] == 1.0
    assert entry.metrics["sam"]["roof"]["tp"] == 2
    assert len(entry.commit) >= 7 and entry.date == eval_results.manila_today()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--dataset", "kill-test-25", "--limit", "1"], "drop --limit"),
        ([], "needs --dataset"),
        (["--dataset", "nope"], "not described"),
    ],
)
def test_record_refuses_partial_or_undescribed_runs(
    photos, results_file, extra, message, monkeypatch, settings, capsys
):
    use_settings(monkeypatch, settings, eval_vision)
    assert eval_vision.main([str(photos), "--yes", "--record", str(results_file), *extra]) == 2
    assert message in capsys.readouterr().err


async def test_dedupe_record_upserts_without_duplicates(results_file, monkeypatch, settings):
    use_settings(monkeypatch, settings, eval_dedupe)
    pairs = eval_dedupe.load_pairs(eval_dedupe.DEFAULT_PAIRS)
    answers = [{"decision": p["label"], "confidence": 0.99, "reason": "Expected."} for p in pairs]
    for _ in range(2):
        judge = FakeJudge(*answers)
        monkeypatch.setattr(eval_dedupe, "make_pair_judge", lambda settings, j=judge: j)
        args = ["--judge", "muse-spark-1.3", "--yes", "--record", str(results_file)]
        assert await eval_dedupe.run(eval_dedupe.parse_args(args)) == 0

    raw = json.loads(results_file.read_text())
    assert [(e["task"], e["dataset"], e["model"]) for e in raw["entries"]] == [
        ("pair_judge", "dedupe-pairs-64", "muse-spark-1.3")
    ]
    metrics = raw["entries"][0]["metrics"]
    assert (metrics["f1"], metrics["reviews"], metrics["miss_ids"]) == (1.0, 0, [])


async def test_dedupe_cap_refusal_pays_for_nothing(monkeypatch, settings, capsys):
    use_settings(monkeypatch, settings, eval_dedupe)
    judge = FakeJudge()
    monkeypatch.setattr(eval_dedupe, "make_pair_judge", lambda settings: judge)
    prefill(settings, 4.5)
    assert await eval_dedupe.run(eval_dedupe.parse_args(["--yes"])) == 1
    assert "Refusing" in capsys.readouterr().out
    assert judge.calls == [] and judge.closed


def test_ledger_is_separate_from_the_app_database(settings):
    prefill(settings, 1.0)
    assert (settings.data_dir / spend.LEDGER_NAME).is_file()
    assert not settings.db_path.exists()  # eval spend never counts against the bot's cap
    engine = spend.ledger_engine(settings.data_dir)
    with Session(engine) as session:
        assert [c.model for c in session.exec(select(ModelCall)).all()] == ["earlier"]


def test_probe_leaves_fakes_alone_and_restores_real_clients(settings):
    real = SparkClient(api_key="k")
    fake = FakeLLM()
    with spend.probe(real, fake):
        assert real.cache_only and not hasattr(fake, "cache_only")
    assert not real.cache_only
    assert spend.probe_ready(real) and not spend.probe_ready(real, fake)
    shutil.rmtree(settings.data_dir, ignore_errors=True)

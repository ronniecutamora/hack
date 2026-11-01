"""The Voice Transcribe eval: its settings, scoring and the results entry it records.
Every model call is mocked; the audio and references are made up here."""

import json

import httpx
import pytest
import respx

from app import eval_results
from app.evaluation import names_heard
from app.gazetteer import transcription_keywords
from app.intake import LANGUAGE_BIAS
from app.meta.base import MetaError
from app.meta.factory import make_transcriber
from app.meta.transcribe import MODEL, PRICE_PER_AUDIO_HOUR
from scripts import eval_transcribe
from tests.test_intake import FakeTranscriber

TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"
DATASET = {
    "title": "Test voice notes",
    "provenance": "real",
    "source": "two made-up clips (test fixture)",
    "label_source": "fixture",
    "n": 2,
    "location": "outside the repo",
}


class RunsOut(FakeTranscriber):
    """Answers with its texts in order, then fails like an API error."""

    async def transcribe(self, wav, **kwargs):
        if not self.texts:
            raise MetaError("Voice Transcribe: no reply")
        return await super().transcribe(wav, **kwargs)


async def passthrough(source, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunk_000.wav").write_bytes(source.read_bytes())
    return [out_dir / "chunk_000.wav"]


def by_name(name):
    return next(s for s in eval_transcribe.SETTINGS if s.name == name)


def test_the_production_setting_is_what_the_bot_sends():
    # tests/test_intake checks the bot sends LANGUAGE_BIAS and transcription_keywords().
    bot = by_name(eval_transcribe.PRODUCTION)
    assert bot.language_bias == LANGUAGE_BIAS
    assert bot.keyword_list(["Talimusak"]) == transcription_keywords()
    with_places = by_name("barangays_and_places").keyword_list(["Talimusak", "Malaban"])
    assert with_places == [*transcription_keywords(), "Talimusak"]  # Malaban is already listed
    assert by_name("language_hint").keyword_list(["Talimusak"]) is None


def test_note_ids_take_ranges_and_lists():
    assert eval_transcribe.parse_only("3-5") == {"3", "4", "5"}
    assert eval_transcribe.parse_only("3, 7,clip-a") == {"3", "7", "clip-a"}


def test_place_names_count_only_when_spelled_as_labelled():
    text = "Baha na po dito sa barangay Talimúsak, sityo Kawayanan"
    assert names_heard(["Talimusak", "Kawayanan", "Bagumbayan"], text) == 2
    assert names_heard(["Talimusak"], "barangay Talimosak") == 0
    assert names_heard(["Kawa"], "sityo Kawayanan") == 0  # whole words only


async def test_a_note_counts_only_when_every_setting_transcribed_it(tmp_path, monkeypatch):
    monkeypatch.setattr("app.intake.to_wav_chunks", passthrough)
    notes = [("1", "baha sa Talimusak", tmp_path / "1.wav"), ("2", "may baha", tmp_path / "2.wav")]
    for _, _, audio in notes:
        audio.write_bytes(b"RIFF")
    texts = ["baha sa Talimosak", "baha sa Talimusak", "may baha"]
    transcriber = RunsOut(texts=texts)
    settings = (by_name("language_hint"), by_name("barangays_and_places"))
    labels = {"1": ["Talimusak"]}

    scored, failed, skipped = await eval_transcribe.evaluate(notes, settings, labels, transcriber)
    # Note 2's second setting had no reply left: it failed, so neither setting scores it.
    assert (failed, skipped) == (1, 0)
    assert [len(s.pairs) for s in scored] == [1, 1]
    assert [(s.heard, s.places) for s in scored] == [(0, 1), (1, 1)]
    assert transcriber.calls[0]["keywords"] is None
    assert "Talimusak" in transcriber.calls[1]["keywords"]
    metrics = eval_transcribe.metrics_of(scored, "language_hint")
    assert metrics["settings"]["language_hint"]["wer"] == 0.3333  # rounded to 4 places
    assert metrics["settings"]["barangays_and_places"]["wer"] == 0.0
    assert metrics["reference_words"] == 3


@pytest.fixture
def recordings(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(eval_transcribe, "get_settings", lambda: settings)
    monkeypatch.setattr("app.intake.to_wav_chunks", passthrough)
    folder = tmp_path / "notes"
    folder.mkdir()
    for note, reference in (("1", "Baha po sa Talimusak"), ("2", "May landslide sa Kawayanan")):
        (folder / f"{note}.m4a").write_bytes(b"RIFF" + note.encode() * 32_000)  # about 1 s
        (folder / f"{note}.txt").write_text(reference)
    (folder / "facts.json").write_text(
        json.dumps({"1": {"barangay_mentioned": "Talimusak"}, "2": {"place_detail": "Kawayanan"}})
    )
    results = tmp_path / "results.json"
    raw = eval_results.read_raw()
    raw["datasets"]["test-notes-2"] = DATASET
    results.write_text(json.dumps({**raw, "entries": []}))
    return folder, results


def reply(request):
    """The transcript spells the place right only when it was sent as a keyword."""
    line = next(x for x in request.content.split(b"\r\n") if x.startswith(b'{"mode"'))
    keywords = json.loads(line).get("keywords", [])
    place = "Talimusak" if "Talimusak" in keywords else "Talimosak"
    return httpx.Response(200, json={"transcript": f"baha po sa {place}", "audioDurationMs": 1000})


@respx.mock
def test_a_recorded_run_stores_metrics_only(recordings, capsys):
    folder, results = recordings
    respx.post(TRANSCRIBE_URL).mock(side_effect=reply)
    args = [str(folder), "--yes", "--record", str(results), "--dataset", "test-notes-2"]

    assert eval_transcribe.main(args) == 0

    entry = eval_results.load(results).find("transcribe", "test-notes-2", MODEL)
    assert entry is not None and (entry.n, entry.errors) == (2, 0)
    assert entry.backend == "Meta Model API" and entry.spend_usd > 0
    settings = entry.metrics["settings"]
    assert list(settings) == [s.name for s in eval_transcribe.SETTINGS]
    assert entry.metrics["production"] == "barangays"
    assert settings["barangays"]["place_names_heard"] == 0
    assert settings["barangays_and_places"]["place_names_heard"] == 1
    assert settings["barangays_and_places"]["place_names"] == 2
    stored = results.read_text()
    for text in ("Baha po", "landslide", "baha po sa"):  # no reference or transcript text
        assert text not in stored
    out = capsys.readouterr().out
    assert "recorded transcribe/test-notes-2" in out

    # A re-run is answered from the cache: free, and the recorded spend does not grow.
    respx.post(TRANSCRIBE_URL).mock(side_effect=AssertionError("not cached"))
    assert eval_transcribe.main([*args, "--cached-only"]) == 0
    again = eval_results.load(results).find("transcribe", "test-notes-2", MODEL)
    assert again.metrics == entry.metrics and again.spend_usd == entry.spend_usd


@respx.mock
def test_a_cache_only_run_never_calls_the_api(recordings, capsys):
    folder, _ = recordings
    route = respx.post(TRANSCRIBE_URL).mock(side_effect=reply)
    assert eval_transcribe.main([str(folder), "--yes", "--cached-only"]) == 1
    assert route.call_count == 0
    assert "skipped: not cached" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--keywords", "barangays"], "standard settings"),
        (["--only", "1"], "has 2 notes; 1 selected"),
        ([], "needs --dataset"),
    ],
)
def test_a_record_must_match_its_dataset(recordings, capsys, extra, message):
    folder, results = recordings
    dataset = [] if extra == [] else ["--dataset", "test-notes-2"]
    args = [str(folder), "--yes", "--record", str(results), *dataset, *extra]
    assert eval_transcribe.main(args) == 2
    assert message in capsys.readouterr().err


async def test_the_dry_run_prices_every_uncached_call_by_its_audio(recordings, settings):
    folder, _ = recordings
    notes = [(a.stem, r.read_text(), a) for a, r in eval_transcribe.find_pairs(folder)]
    labels = eval_transcribe.place_labels(folder)
    transcriber = make_transcriber(settings)
    try:
        usd, detail = await eval_transcribe.estimate_cost(
            notes, eval_transcribe.SETTINGS, labels, transcriber
        )
    finally:
        await transcriber.aclose()
    # 2 notes x 4 settings, about 1 s of audio each: nothing is called, every miss is priced.
    assert detail == "8 uncached call(s), 8 s of audio"
    assert usd == pytest.approx(8 * 32_004 / 32_000 / 3600 * PRICE_PER_AUDIO_HOUR)

import httpx
import pytest
import respx

from app.evaluation import corpus_word_error_rate, normalize_text, word_error_rate
from scripts import eval_transcribe


def test_normalize_lowercases_and_strips_punctuation():
    assert (
        normalize_text("Bumaha NA po, sa Biñan! Don't panic.") == "bumaha na po sa biñan dont panic"
    )


def test_word_error_rate():
    assert word_error_rate("Baha na po.", "baha na po") == 0.0
    assert word_error_rate("baha na po", "baha na") == pytest.approx(1 / 3)


def test_corpus_wer_weights_by_reference_length():
    pairs = [("a b c d", "a b c d"), ("e", "x")]  # 1 error over 5 words, not mean(0, 1)
    assert corpus_word_error_rate(pairs) == pytest.approx(0.2)


def test_script_refuses_folder_inside_repo(capsys):
    assert eval_transcribe.main([str(eval_transcribe.REPO_ROOT / "tests")]) == 2
    assert "outside the repository" in capsys.readouterr().err


@respx.mock
def test_script_end_to_end_with_mocked_api(tmp_path, monkeypatch, settings, capsys):
    monkeypatch.setattr(eval_transcribe, "get_settings", lambda: settings)

    async def passthrough(source, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "chunk_000.wav").write_bytes(source.read_bytes())
        return [out_dir / "chunk_000.wav"]

    monkeypatch.setattr("app.intake.to_wav_chunks", passthrough)
    (tmp_path / "clip.wav").write_bytes(b"RIFFfake")
    (tmp_path / "clip.txt").write_text("Baha na po")
    (tmp_path / "orphan.wav").write_bytes(b"RIFFnoref")  # no .txt, ignored
    respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(200, json={"transcript": "baha na", "audioDurationMs": 5})
    )

    assert eval_transcribe.main([str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "clip.wav" in out and "orphan" not in out
    assert "33.3%" in out and "OVERALL" in out

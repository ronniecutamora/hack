"""Metrics for the labeled test sets (judges reward verifiable agents)."""

import re
import string
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import jiwer

# Apostrophes are dropped (don't -> dont); every other punctuation mark becomes a space.
_PUNCT = re.compile(f"[{re.escape(string.punctuation.replace(chr(39), ''))}]")


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().replace("'", "").replace("’", "")
    return " ".join(_PUNCT.sub(" ", text).split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER after normalization. The reference must contain at least one word."""
    return jiwer.wer(normalize_text(reference), normalize_text(hypothesis))


def corpus_word_error_rate(pairs: list[tuple[str, str]]) -> float:
    """Overall WER: total word errors over total reference words (not a mean of per-file WERs)."""
    references = [normalize_text(ref) for ref, _ in pairs]
    hypotheses = [normalize_text(hyp) for _, hyp in pairs]
    return jiwer.wer(references, hypotheses)


def cohen_kappa(labels: Sequence[str], predictions: Sequence[str]) -> float:
    """Agreement between two labelings beyond chance (1 = perfect, 0 = chance level)."""
    if len(labels) != len(predictions) or not labels:
        raise ValueError("need two non-empty labelings of the same length")
    n = len(labels)
    observed = sum(a == b for a, b in zip(labels, predictions, strict=True)) / n
    label_counts, prediction_counts = Counter(labels), Counter(predictions)
    expected = sum(label_counts[c] * prediction_counts[c] for c in label_counts) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class BinaryScores:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def binary_scores(labels: Sequence[bool], predictions: Sequence[bool]) -> BinaryScores:
    pairs = list(zip(labels, predictions, strict=True))
    return BinaryScores(
        tp=sum(a and b for a, b in pairs),
        fp=sum(b and not a for a, b in pairs),
        fn=sum(a and not b for a, b in pairs),
        tn=sum(not a and not b for a, b in pairs),
    )


REPO_ROOT = Path(__file__).resolve().parents[1]


def outside_repo(folder: Path) -> Path:
    """Resolve an eval data folder, refusing anything inside this public repository."""
    folder = folder.expanduser().resolve()
    if folder == REPO_ROOT or REPO_ROOT in folder.parents:
        raise ValueError("keep eval data outside the repository; pass a folder elsewhere")
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    return folder

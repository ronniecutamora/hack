"""Metrics for the labeled test sets (judges reward verifiable agents)."""

import re
import string
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import jiwer

from app.gazetteer import normalize as normalize_place

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


def names_heard(names: Iterable[str], text: str) -> int:
    """How many of `names` occur in `text` as whole words, ignoring case, accents and punctuation.

    A name counts only when it was transcribed with its spelling ("Talimusak", not "Talimosak").
    """
    haystack = f" {normalize_place(text)} "
    return sum(bool(n) and f" {n} " in haystack for n in map(normalize_place, names))


def names_inserted(names: Iterable[str], reference: str, hypothesis: str) -> int:
    """How many times the hypothesis has one of `names` beyond the times the reference has it:
    names a keyword list pulled into the transcript that the speaker did not say. Longer names
    are matched first, so "Juan Luna Street" is one name, not also "Luna Street"."""
    ref, hyp = normalize_place(reference), normalize_place(hypothesis)
    inserted = 0
    for name in sorted({n for n in map(normalize_place, names) if n}, key=len, reverse=True):
        word = re.compile(rf"(?<!\S){re.escape(name)}(?!\S)")  # whole words only
        inserted += max(0, len(word.findall(hyp)) - len(word.findall(ref)))
        ref, hyp = word.sub("|", ref), word.sub("|", hyp)
    return inserted


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

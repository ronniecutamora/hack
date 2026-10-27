"""Metrics for the labeled test sets (judges reward verifiable agents)."""

import re
import string

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

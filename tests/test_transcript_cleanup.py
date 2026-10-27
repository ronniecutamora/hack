import pytest

from app.transcript_cleanup import clean_transcript


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        # artifacts seen in the 2026-10-27 test notes
        ("may totally totally damaged na bahay", "may totally damaged na bahay"),
        ("natumba ang puno sa puno sa kalsada", "natumba ang puno sa kalsada"),
        ("kaninang umaga umaga po", "kaninang umaga po"),
        ("barangay Luntian,ian po ito", "barangay Luntian po ito"),
        # case and punctuation differences still count as a repeat
        ("Totally totally.", "Totally."),
        ("baha, baha na po", "baha na po"),
        # repeats of repeats collapse fully
        ("po po po", "po"),
        ("sa may sa may sa may ilog", "sa may ilog"),
    ],
)
def test_repeated_words_and_phrases_collapse(raw, clean):
    assert clean_transcript(raw) == clean


@pytest.mark.parametrize(
    "text",
    [
        "unti unti nang tumataas ang tubig",
        "isa isa naming inilikas ang mga pamilya",
        "araw araw na lang bumabaha",
        "dahan-dahan po, unti-unti",
        "sari sari store sa kanto",
        "may 1,500 na sako",  # a comma inside a number is not an echoed ending
        "twenty three bahay, twenty five pamilya",
        "",
    ],
)
def test_real_tagalog_reduplication_and_normal_text_are_kept(text):
    assert clean_transcript(text) == " ".join(text.split())


def test_echoed_ending_must_really_be_the_ending():
    assert clean_transcript("Malaban,po") == "Malaban,po"

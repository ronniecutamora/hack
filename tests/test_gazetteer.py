import pytest

from app.gazetteer import BARANGAYS, BY_NAME, CITY_PCODE, find_in_text, transcription_keywords


def test_biñan_has_24_barangays_with_unique_city_codes():
    assert len(BARANGAYS) == 24
    assert len({b.pcode for b in BARANGAYS}) == 24
    assert all(b.pcode.startswith(CITY_PCODE) for b in BARANGAYS)


def test_keywords_cover_every_barangay_with_accents():
    keywords = transcription_keywords()
    assert {b.name for b in BARANGAYS} <= set(keywords)
    assert "Santo Niño" in keywords and "Mampalasan" in keywords and "Calabuso" in keywords
    assert len(keywords) == len(set(keywords))
    assert not any(k.startswith("Sto.") for k in keywords)  # abbreviations add nothing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Baha na po hanggang tuhod dito sa Dela Paz", "De La Paz"),
        ("BRGY. STO. NIÑO po ito", "Santo Niño"),
        ("taga sto nino po kami", "Santo Niño"),
        ("Sa Calabuso po, malapit sa ilog", "Santo Tomas"),
        ("barangay Mampalasan, purok 3", "Mamplasan"),
        ("Soro soro po, may landslide", "Soro-soro"),
        ("brgy Halang po", "San Francisco"),
        ("Barangay Biñan po ito", "Biñan"),
    ],
)
def test_exact_matches_ignore_case_accents_and_abbreviations(text, expected):
    match = find_in_text(text)
    assert match.barangay == BY_NAME[expected]
    assert not match.fuzzy


@pytest.mark.parametrize(
    "text",
    [
        "Malamig na po dito, walang kuryente",  # "malamig" = cold
        "Lubog na ang tubigan namin",  # "tubigan" = irrigated field
        "Dito sa Biñan, baha na po",  # the city, not barangay Biñan
        "nakahalang ang puno sa daan",
        "",
        None,
    ],
)
def test_common_words_and_the_city_name_are_not_barangays(text):
    assert find_in_text(text).barangays == ()


def test_prefix_makes_common_words_count():
    assert find_in_text("Brgy. Malamig po ito").barangay == BY_NAME["Malamig"]
    assert find_in_text("barangay Tubigan").barangay == BY_NAME["Tubigan"]


def test_longest_name_wins_over_overlapping_alias():
    match = find_in_text("barangay Biñan Poblacion po")
    assert match.barangays == (BY_NAME["Biñan"],)


def test_two_barangays_are_ambiguous_and_keep_mention_order():
    match = find_in_text("Galing Malaban papuntang Casile, baha sa dalawa")
    assert match.ambiguous
    assert match.barangay is None
    assert match.barangays == (BY_NAME["Malaban"], BY_NAME["Casile"])


def test_transcription_slip_after_prefix_is_a_fuzzy_match():
    match = find_in_text("barangay Mamplason po, lubog ang mga bahay")
    assert match.barangay == BY_NAME["Mamplasan"]
    assert match.fuzzy


def test_fuzzy_match_needs_a_prefix_and_a_close_name():
    assert find_in_text("Mamplason po").barangays == ()
    assert find_in_text("barangay Sinagpulo sa sityo Malarik").barangays == ()


def test_biñan_poblacion_is_barangay_biñan_even_without_a_prefix():
    assert find_in_text("Baha sa Biñan Poblacion").barangays == (BY_NAME["Biñan"],)


@pytest.mark.parametrize(
    "text",
    [
        "Si Rosa Dela Paz po ang nagsabi",  # a surname
        "ganado ang mga tao sa pagtulong",  # "eager"
        "Pakisabi kay Jose Platero",
        "Binaha ang San Jose parish",
    ],
)
def test_surnames_words_and_buildings_are_not_barangays(text):
    assert find_in_text(text).barangays == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Santo Niño chapel sa Brgy. Malaban, lubog", "Malaban"),
        ("Taga Dela Paz po ako", "De La Paz"),
        ("galing Ganado, sira ang tulay", "Ganado"),
        ("flooding in San Jose near the river", "San Jose"),
    ],
)
def test_surname_like_names_count_after_a_place_cue(text, expected):
    assert find_in_text(text).barangay == BY_NAME[expected]


def test_a_bare_name_counts_when_cues_are_not_required():
    assert find_in_text("Dela Paz po ito, baha").barangays == ()  # left to the LLM's reading
    assert find_in_text("Ganado", require_cues=False).barangay == BY_NAME["Ganado"]
    assert find_in_text("Malamig", require_cues=False).barangays == ()  # still a common word

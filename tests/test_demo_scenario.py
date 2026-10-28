from dataclasses import replace

import pytest

from app.demo import SCENARIO_PATH
from app.demo.scenario import (
    LANDFALL_AT,
    STREET_SERIES,
    ScenarioReport,
    load_scenario,
    received_at,
    validate,
)
from app.gazetteer import BY_NAME, find_in_text

SCENARIO = load_scenario(SCENARIO_PATH)
BY_KEY = {r.key: r for r in SCENARIO}


def test_committed_scenario_validates():
    assert SCENARIO_PATH.is_file()
    assert validate(SCENARIO) == []


def test_report_count_reporters_and_window():
    assert 15 <= len(SCENARIO) <= 20
    assert len({r.key for r in SCENARIO}) == len(SCENARIO)
    assert all(r.reporter.startswith("demo-") for r in SCENARIO)
    for r in SCENARIO:
        delta = received_at(r) - LANDFALL_AT
        assert 0 <= delta.days <= 14, r.key


def test_flooded_street_series_rises_then_falls():
    members = sorted((r for r in SCENARIO if r.series == STREET_SERIES), key=received_at)
    assert len(members) >= 3
    assert {r.barangay for r in members} == {"Malaban"}
    assert len({r.reporter for r in members}) >= 3
    depths = [r.flood_cm for r in members]
    assert depths[0] < max(depths) > depths[-1]


def test_correction_comes_after_its_target():
    correction = next(r for r in SCENARIO if r.corrects is not None)
    assert "mali" in correction.text.lower()
    assert received_at(correction) > received_at(BY_KEY[correction.corrects])


def test_two_barangay_report_is_unpinned_and_ambiguous():
    matches = [r for r in SCENARIO if r.barangay is None]
    assert matches, "need unpinned reports"
    assert any(len(find_in_text(r.text).barangays) >= 2 for r in matches)
    report = BY_KEY["r09"]
    assert report.barangay is None and report.place is None
    assert [b.name for b in find_in_text(report.text).barangays] == ["Malaban", "San Antonio"]


def test_placeless_report_names_no_barangay():
    assert any(r.barangay is None and not find_in_text(r.text).barangays for r in SCENARIO)


def test_fisherfolk_and_fake_name_beats():
    assert any("fish cage" in r.text or "fish pen" in r.text for r in SCENARIO)
    assert any("juan dela cruz" in r.text.lower() for r in SCENARIO)


@pytest.mark.parametrize(
    "keys,barangay,purok,landmarks",
    [
        (("r01", "r06", "r10", "r14"), "Malaban", "purok 3", ("kanto", "corner")),
        (("r04", "r11", "r17"), "Casile", "purok 2", ("bahay",)),
        (("r07", "r16"), "Poblacion", "purok 2", ("covered court",)),
    ],
)
def test_repeat_reports_name_one_specific_place_in_varied_text(keys, barangay, purok, landmarks):
    members = [BY_KEY[key] for key in keys]
    assert len({r.place for r in members}) == 1 and members[0].place
    assert len({r.text for r in members}) == len(members)
    for report in members:
        assert report.barangay == barangay
        assert find_in_text(report.text).barangay == BY_NAME[barangay]
        assert purok in report.text.lower()
        assert any(landmark in report.text.lower() for landmark in landmarks)
        if barangay == "Malaban":
            assert "hall" in report.text.lower()
        elif barangay == "Poblacion":
            assert "evacuation center" in report.text.lower()


def test_street_text_keeps_the_ankle_knee_waist_ankle_story():
    for key, depth in zip(
        ("r01", "r06", "r10", "r14"),
        ("bukung-bukong", "tuhod", "bewang", "bukung-bukong"),
        strict=True,
    ):
        assert depth in BY_KEY[key].text


def test_fisherfolk_share_a_specific_de_la_paz_place():
    members = [BY_KEY[key] for key in ("r05", "r15")]
    assert members[0].place == members[1].place and members[0].place
    for report in members:
        assert report.barangay == "De La Paz"
        assert find_in_text(report.text).barangay == BY_NAME["De La Paz"]
        assert "Bungahan" not in report.text
        assert "Purok 1" in report.text and "tabing-lawa" in report.text
        assert "fish pen" in report.text
    assert members[0].photo_prompt is not None
    assert all(b.lower() not in members[0].photo_prompt.lower() for b in BY_NAME)


def test_reports_span_at_least_four_barangays():
    assert len({r.barangay for r in SCENARIO if r.barangay}) >= 4


def test_pins_reference_real_barangays():
    assert all(r.barangay in BY_NAME for r in SCENARIO if r.barangay)


def test_photo_scenes_do_not_ask_for_people_or_text():
    banned = ("person", "people", "face", "crowd", "child", "sign", "billboard", "plate")
    scenes = [r.photo_prompt for r in SCENARIO if r.photo_prompt]
    assert scenes, "the demo needs photos"
    for scene in scenes:
        assert not any(word in scene.lower() for word in banned), scene


def test_validator_catches_broken_scenarios():
    good = ScenarioReport("a", "demo-01", 0, 8, "Baha sa Malaban", barangay="Malaban")
    assert validate([good]) != []  # too few reports
    bad_pin = ScenarioReport("b", "demo-02", 0, 9, "Baha", barangay="Nowhere")
    assert any("Nowhere" in e for e in validate([good] * 16 + [bad_pin]))
    assert any("correction" in e for e in validate(_without_corrections()))


@pytest.mark.parametrize("place", [None, "", "   ", "different-corner"])
def test_every_series_must_share_one_nonempty_place(place):
    changed = [replace(r, place=place) if r.key == "r06" else r for r in SCENARIO]
    assert any("share one nonempty place" in e for e in validate(changed))


def test_place_rule_applies_to_other_series_too():
    changed = [
        replace(r, series="other-series", place=f"place-{r.key}") if r.key in ("r04", "r11") else r
        for r in SCENARIO
    ]
    assert any("series 'other-series'" in e and "place" in e for e in validate(changed))


def test_shared_series_cannot_span_barangays():
    changed = [replace(r, barangay="Casile") if r.key == "r06" else r for r in SCENARIO]
    assert any("pin the same barangay" in e for e in validate(changed))


@pytest.mark.parametrize("place,barangay", [("", "Malaban"), ("  ", "Malaban"), ("corner", None)])
def test_place_requires_a_name_and_a_pin(place, barangay):
    changed = [
        replace(r, place=place, barangay=barangay) if r.key == "r02" else r for r in SCENARIO
    ]
    assert any("r02: place needs" in e for e in validate(changed))


def _without_corrections():
    base = [r for r in SCENARIO if r.corrects is None]
    return [r for r in base if "mali" not in r.text.lower()]

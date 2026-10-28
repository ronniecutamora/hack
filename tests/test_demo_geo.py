from itertools import combinations

import pytest

from app.demo.geo import pick_point
from app.gazetteer import BARANGAYS
from app.geo import barangay_at
from app.incidents import distance_m


def test_reports_without_place_keep_the_original_pin():
    assert pick_point("Malaban", "r01") == (14.35310661115519, 121.08615674189775)
    assert pick_point("Malaban", "r01", place=None) == pick_point("Malaban", "r01")


@pytest.mark.parametrize("barangay", BARANGAYS, ids=lambda b: b.name)
def test_shared_place_pins_are_deterministic_close_and_inside(barangay):
    for place in ("purok3-corner", "covered-court", "lakeshore"):
        anchor = pick_point(barangay.name, place)
        points = [pick_point(barangay.name, f"r{i}", place=place) for i in range(10)]
        assert len(set(points)) == len(points)
        for i, point in enumerate(points):
            assert pick_point(barangay.name, f"r{i}", place=place) == point
            assert barangay_at(*point) == barangay
            assert distance_m(anchor, point) <= 30
        assert all(distance_m(a, b) <= 60 for a, b in combinations(points, 2))


def test_different_places_have_independent_anchors():
    first = pick_point("Malaban", "r01", place="purok3-corner")
    other = pick_point("Malaban", "r01", place="covered-court")
    assert distance_m(first, other) > 60
    for point, place in ((first, "purok3-corner"), (other, "covered-court")):
        assert distance_m(point, pick_point("Malaban", place)) <= 30

"""Biñan's OpenStreetMap names for Voice Transcribe keywords: the builder (scripts.build_places),
the committed data file and the keyword list the bot sends (app.gazetteer)."""

import json

from app import gazetteer
from app.evaluation import names_inserted
from app.gazetteer import (
    BARANGAYS,
    KEYWORD_LIMIT,
    KEYWORD_MAX_CHARS,
    PLACES_FILE,
    place_keywords,
    transcription_keywords,
)
from scripts import build_places
from tests.test_geo import CENTERS

ENGLISH = frozenset({"love", "green", "pearl", "monday"})
INSIDE = CENTERS["Malaban"]
OUTSIDE = (14.10, 121.40)
WEST = (INSIDE[0], INSIDE[1] - 0.006)  # about 650 m inland from Malaban's center


def way(name, *points, **tags):
    geometry = [{"lat": lat, "lon": lon} for lat, lon in points]
    return {"type": "way", "tags": {"highway": "residential", "name": name, **tags},
            "geometry": geometry}  # fmt: skip


def node(name, point, **tags):
    return {"type": "node", "lat": point[0], "lon": point[1], "tags": {"name": name, **tags}}


def near(point, d=0.0005):
    return (point[0] + d, point[1] + d)


def test_names_are_normalized():
    assert build_places.clean_name("J.P. Rizal St.") == "J. P. Rizal Street"
    assert build_places.clean_name("St. Francis Chapel") == "Saint Francis Chapel"
    assert build_places.clean_name("Brgy. Hall ng Casile") == "Barangay Hall ng Casile"
    assert build_places.clean_name("Sto. Tomas Ave.") == "Santo Tomas Avenue"


def test_only_distinctive_names_inside_biñan_are_kept_and_ranked():
    raw = {
        "elements": [
            way("Pedro Paterno St.", INSIDE, WEST),  # long, inside
            way("Pedro Paterno Street", WEST, near(WEST)),  # the same street
            way("Tulay Bato Street", INSIDE, near(INSIDE)),
            way("Love Avenue", INSIDE, near(INSIDE)),  # ordinary words only
            way("11th Street", INSIDE, near(INSIDE)),  # a number
            way("Malaban Road", INSIDE, near(INSIDE)),  # only a barangay name: already listed
            way("Faraway Street", OUTSIDE, near(OUTSIDE)),  # outside Biñan
            node("Joel house", INSIDE, landuse="residential"),  # someone's home
            node("Southville 5", INSIDE, place="neighbourhood"),
            node("Colegio San Agustin", INSIDE, amenity="school"),
        ]
    }
    places = build_places.build(raw, ENGLISH)
    assert [(p["name"], p["kind"]) for p in places] == [
        ("Pedro Paterno Street", "street"),
        ("Tulay Bato Street", "street"),
        ("Southville 5", "area"),
        ("Colegio San Agustin", "landmark"),
    ]
    paterno = places[0]
    assert paterno["features"] == 2 and paterno["length_m"] > 500
    assert "Malaban" in paterno["barangays"]
    assert build_places.build(raw, ENGLISH) == places  # stable


def test_the_query_covers_biñan_and_asks_for_names_only():
    south, west, north, east = build_places.boundary_bbox()
    assert south < INSIDE[0] < north and west < INSIDE[1] < east
    text = build_places.query((south, west, north, east))
    assert 'way["highway"]["name"]' in text and "out tags geom;" in text


def test_the_committed_file_says_where_its_names_come_from():
    data = json.loads(PLACES_FILE.read_text(encoding="utf-8"))
    assert "OpenStreetMap" in data["source"] and "ODbL" in data["license"]
    assert data["query"].startswith("[out:json]") and data["osm_data_as_of"]
    assert data["count"] == len(data["places"]) == sum(data["counts"].values())
    names = [p["name"] for p in data["places"]]
    assert len({gazetteer.normalize(n) for n in names}) == len(names)  # de-duplicated
    assert not any(n.lower().endswith(" house") for n in names)


def test_the_keyword_list_fits_the_api_with_barangays_first():
    barangays = transcription_keywords(places=0)
    assert {b.name for b in BARANGAYS} <= set(barangays)
    bot = transcription_keywords()
    assert bot[: len(barangays)] == barangays  # barangays first, then OSM names
    assert len(bot) == KEYWORD_LIMIT and len(set(bot)) == len(bot)
    assert all(len(k) <= KEYWORD_MAX_CHARS for k in bot)
    assert transcription_keywords(places=3) == [*barangays, *place_keywords()[:3]]
    assert transcription_keywords(places=10_000) == bot  # never past the limit
    kinds = {p["name"]: p["kind"] for p in json.loads(PLACES_FILE.read_text())["places"]}
    order = [gazetteer.PLACE_KIND_ORDER.index(kinds[n]) for n in place_keywords()]
    assert order == sorted(order)  # areas, then landmarks, then streets


def test_insertions_count_names_said_more_often_than_in_the_script():
    names = ["Juan Luna Street", "Luna Street", "Casile"]
    assert names_inserted(names, "baha sa Casile", "baha sa Casile") == 0
    assert names_inserted(names, "baha sa Casile", "baha sa Casile sa Juan Luna Street") == 1
    assert names_inserted(names, "baha", "Casile, Casile, luna street") == 3
    assert names_inserted(names, "Casile at Casile", "Casile") == 0  # a miss is not an insertion

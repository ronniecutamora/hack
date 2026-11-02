"""Rebuild app/data/binan_places.json: Biñan's named streets, areas and landmarks, from OSM.

Usage:
    uv run python -m scripts.build_places [--raw saved-overpass.json] [--save-raw out.json]
        [--words /usr/share/dict/words]

One Overpass API query over the bounding box of app/data/binan_barangays.geojson asks for every
named highway (streets, roads, alleys), named place (neighborhoods, sitios and puroks mapped as
places) and residential area (subdivisions, villages), and named landmarks people give as an
address (markets, schools, colleges, churches and chapels, halls, hospitals). Each is kept only
where it lies inside a Biñan barangay polygon (app.geo): a way by the midpoints of its segments,
so a street is credited with the length it runs inside Biñan; anything else by its center.

Names are normalized ("St." -> "Street", "Brgy." -> "Barangay"), de-duplicated ignoring case
and accents, and dropped when they would only bias a transcript toward ordinary words: a name
is kept only if at least one of its words is not a generic place word (street, road, village,
school, ...), not a number, an initial or an ordinal, not already a barangay name (those are on
the keyword list already) and not in the English word list (`--words`; "Love Avenue", "Green
Street" and "Pearl Street" go, "J. P. Rizal Street" and "Tulay Bato Street" stay), or when it
names someone's home ("... house"). Streets are
ranked by their length inside Biñan, then areas and landmarks by how many OSM features carry
the name. The output is sorted and stable: rebuilding from the same response (`--raw`) writes
the same file. Map data © OpenStreetMap contributors, under the ODbL 1.0 (see the README).
"""

import argparse
import json
import math
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.gazetteer import BARANGAYS, normalize
from app.geo import BOUNDARIES, barangay_at

OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "binan_places.json"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "ulat-binan-places-builder/1.0 (hackathon project; rebuilds a keyword list)"
PLACE_KINDS = ("neighbourhood", "quarter", "suburb", "hamlet", "isolated_dwelling", "village")
LANDMARKS = (
    "marketplace", "school", "college", "university", "place_of_worship", "hospital", "townhall",
    "community_centre",
)  # fmt: skip
ABBREVIATIONS = {
    "st": "Street", "ave": "Avenue", "av": "Avenue", "rd": "Road", "blvd": "Boulevard",
    "hwy": "Highway", "ext": "Extension", "brgy": "Barangay", "bgy": "Barangay",
    "subd": "Subdivision", "sto": "Santo", "sta": "Santa", "natl": "National",
}  # fmt: skip
# Words that say what kind of place it is, not which one: never enough on their own.
_GENERIC_WORDS = """
    street avenue road drive lane loop circle court boulevard highway expressway extension alley
    path way crescent place close parkway square bridge overpass flyover interchange exit entry
    ramp northbound southbound access service bypass diversion phase block lot subdivision
    village homes home estate estates heights hills residences residence town city barangay
    purok sitio compound national provincial old new main north south east west upper lower
    interior kalye daan looban school elementary high integrated senior junior church chapel
    parish market public hall center hospital college university campus of the and de la
    del los las san santa santo saint st sto sta ng sa mga ni kay corner gate phase
    """
GENERIC = frozenset(_GENERIC_WORDS.split())
# A name ending in one of these is someone's home ("Joel house"), not an address: never sent.
PRIVATE_HOMES = frozenset({"house", "crib", "bahay", "residence"})
_INITIAL_OR_NUMBER = re.compile(r"^([a-z]|\d+(st|nd|rd|th)?|[ivx]+)$")


def query(bbox: tuple[float, float, float, float]) -> str:
    """The Overpass QL query over (south, west, north, east)."""
    box = ",".join(f"{v:.6f}" for v in bbox)
    places = "|".join(PLACE_KINDS)
    landmarks = "|".join(LANDMARKS)
    return (
        "[out:json][timeout:180];\n(\n"
        f'  way["highway"]["name"]({box});\n'
        f'  nwr["place"~"^({places})$"]["name"]({box});\n'
        f'  nwr["landuse"="residential"]["name"]({box});\n'
        f'  nwr["amenity"~"^({landmarks})$"]["name"]({box});\n'
        ");\nout tags geom;"
    )


def boundary_bbox() -> tuple[float, float, float, float]:
    lons: list[float] = []
    lats: list[float] = []

    def walk(coords: Any) -> None:
        if isinstance(coords[0], int | float):
            lons.append(coords[0])
            lats.append(coords[1])
        else:
            for c in coords:
                walk(c)

    for feature in json.loads(BOUNDARIES.read_text(encoding="utf-8"))["features"]:
        walk(feature["geometry"]["coordinates"])
    return min(lats), min(lons), max(lats), max(lons)


def fetch(text: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({"data": text}).encode()
    request = urllib.request.Request(OVERPASS, body, {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=240) as response:  # noqa: S310 (fixed https URL)
        return json.load(response)


def clean_name(name: str) -> str:
    """'J.P. Rizal St.' -> 'J. P. Rizal Street'; 'Brgy. Hall' -> 'Barangay Hall'."""
    name = unicodedata.normalize("NFC", name).replace("’", "'")
    name = re.sub(r"\b([A-Z])\.(?=[A-Z])", r"\1. ", name)  # "J.P." -> "J. P."
    words = []
    parts = name.split()
    for i, word in enumerate(parts):
        key = word.lower().rstrip(".,")
        if key == "st" and word.endswith(".") and i < len(parts) - 1:
            words.append("Saint")  # "St. Francis Chapel"; a last "St." is a street
        elif key in ABBREVIATIONS and word.endswith("."):
            words.append(ABBREVIATIONS[key])
        else:
            words.append(word)
    return " ".join(words).strip(" ,;")


def _tokens(name: str) -> list[str]:
    return normalize(name).split()


def distinctive(name: str, english: frozenset[str], barangay_words: frozenset[str]) -> list[str]:
    """The words of a name that could make Voice Transcribe hear a place it would otherwise miss."""
    return [
        t
        for t in _tokens(name)
        if t not in GENERIC
        and not _INITIAL_OR_NUMBER.match(t)
        and t not in barangay_words
        and t not in english
    ]


def _meters(a: dict[str, float], b: dict[str, float]) -> float:
    lat = math.radians((a["lat"] + b["lat"]) / 2)
    dx = math.radians(b["lon"] - a["lon"]) * math.cos(lat)
    dy = math.radians(b["lat"] - a["lat"])
    return 6_371_000 * math.hypot(dx, dy)


def _inside(element: dict[str, Any]) -> tuple[float, set[str]]:
    """(meters inside Biñan, barangays it touches) of one OSM element."""
    geometry = element.get("geometry") or []
    if element["type"] == "node":
        geometry = [{"lat": element["lat"], "lon": element["lon"]}]
    elif element["type"] == "relation":
        geometry = [p for m in element.get("members", []) for p in m.get("geometry") or []]
    if len(geometry) == 1 or (element["type"] != "way" and geometry):
        lat = sum(p["lat"] for p in geometry) / len(geometry)
        lon = sum(p["lon"] for p in geometry) / len(geometry)
        barangay = barangay_at(lat, lon)
        return 0.0, {barangay.name} if barangay else set()
    length, barangays = 0.0, set()
    for a, b in zip(geometry, geometry[1:], strict=False):
        barangay = barangay_at((a["lat"] + b["lat"]) / 2, (a["lon"] + b["lon"]) / 2)
        if barangay is not None:
            length += _meters(a, b)
            barangays.add(barangay.name)
    return length, barangays


def kind_of(tags: dict[str, str]) -> str:
    if "highway" in tags:
        return "street"
    if "amenity" in tags:
        return "landmark"
    return "area"


def build(raw: dict[str, Any], english: frozenset[str]) -> list[dict[str, Any]]:
    """The kept names, ranked: streets by length inside Biñan, then areas, then landmarks."""
    barangay_words = frozenset(t for b in BARANGAYS for n in b.names for t in _tokens(n))
    found: dict[tuple[str, str], dict[str, Any]] = {}
    spelled: dict[tuple[str, str], defaultdict[str, int]] = {}
    for element in raw["elements"]:
        tags = element.get("tags", {})
        name = clean_name(tags.get("name", ""))
        if not name:
            continue
        length, barangays = _inside(element)
        if not barangays:
            continue  # outside Biñan
        kind = kind_of(tags)
        key = (kind, " ".join(_tokens(name)))
        entry = found.setdefault(key, {"kind": kind, "features": 0, "length_m": 0.0, "in": set()})
        entry["features"] += 1
        entry["length_m"] += length
        entry["in"] |= barangays
        spelled.setdefault(key, defaultdict(int))[name] += 1
    kept = []
    for key, entry in found.items():
        # The spelling most features use (ties: alphabetical, so the output is stable).
        name = min(spelled[key].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if not distinctive(name, english, barangay_words) or _tokens(name)[-1] in PRIVATE_HOMES:
            continue
        kept.append(
            {
                "name": name,
                "kind": entry["kind"],
                "barangays": sorted(entry["in"]),
                "features": entry["features"],
                "length_m": round(entry["length_m"]) if entry["kind"] == "street" else None,
            }
        )
    order = {"street": 0, "area": 1, "landmark": 2}
    seen: set[str] = set()
    ranked = []
    for item in sorted(
        kept,
        key=lambda p: (order[p["kind"]], -(p["length_m"] or 0), -p["features"], p["name"]),
    ):
        folded = " ".join(_tokens(item["name"]))
        if folded not in seen:  # one name, one keyword, whatever kinds carry it
            seen.add(folded)
            ranked.append(item)
    return ranked


def load_words(path: Path) -> frozenset[str]:
    """Lowercase English words, minus proper nouns (capitalized entries in the list)."""
    return frozenset(
        w for w in path.read_text(encoding="utf-8", errors="ignore").split() if w.islower()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, help="reuse a saved Overpass response")
    parser.add_argument("--save-raw", type=Path, help="also save the Overpass response here")
    parser.add_argument("--words", type=Path, default=Path("/usr/share/dict/words"))
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    text = query(boundary_bbox())
    try:
        raw = json.loads(args.raw.read_text(encoding="utf-8")) if args.raw else fetch(text)
        english = load_words(args.words)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.save_raw:
        args.save_raw.write_text(json.dumps(raw), encoding="utf-8")
    places = build(raw, english)
    osm_base = raw.get("osm3s", {}).get("timestamp_osm_base", "unknown")
    counts = {k: sum(p["kind"] == k for p in places) for k in ("street", "area", "landmark")}
    document = {
        "source": "OpenStreetMap contributors, via the Overpass API (overpass-api.de)",
        "license": "ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/); "
        "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)",
        "osm_data_as_of": osm_base,
        "built_by": "uv run python -m scripts.build_places",
        "query": text,
        "filter": f"inside a Biñan barangay polygon (app.geo); dropped when no word is "
        f"distinctive (generic place words, numbers, initials, barangay names and the "
        f"{len(english)} lowercase words of {args.words.name} are not)",
        "count": len(places),
        "counts": counts,
        "places": places,
    }
    args.out.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(f"{len(places)} names ({counts}) written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

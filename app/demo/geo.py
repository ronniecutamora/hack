"""Deterministic pins inside barangay polygons (no hand-typed coordinates).

The loader pins a report by barangay name: the point is picked from the barangay's
boundary polygon with a seed derived from the scenario key. Reports sharing a
place jitter around one anchor within 30 m. Boundaries: app/data/binan_barangays.geojson.
"""

import hashlib
import json
import math
import random
from functools import cache
from pathlib import Path

from app.gazetteer import BY_NAME

BOUNDARIES = Path(__file__).parent.parent / "data" / "binan_barangays.geojson"
_ATTEMPTS = 1000
_EARTH_RADIUS_M = 6_371_000
_JITTER_M = 30


@cache
def _polygons() -> dict[str, list]:
    """Barangay name -> list of polygons (each: outer ring + holes, in lon/lat)."""
    collection = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    by_pcode: dict[str, list] = {}
    for feature in collection["features"]:
        geometry = feature["geometry"]
        coords = geometry["coordinates"]
        by_pcode[feature["properties"]["pcode"]] = (
            [coords] if geometry["type"] == "Polygon" else coords
        )
    return {b.name: by_pcode[b.pcode] for b in BY_NAME.values()}


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _inside(lon: float, lat: float, polygons: list) -> bool:
    return any(
        _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:])
        for poly in polygons
    )


def _bbox(polygons: list) -> tuple[float, float, float, float]:
    lons = [p[0] for poly in polygons for p in poly[0]]
    lats = [p[1] for poly in polygons for p in poly[0]]
    return min(lons), min(lats), max(lons), max(lats)


def pick_point(barangay: str, seed_key: str, place: str | None = None) -> tuple[float, float]:
    """A deterministic (lat, lon), optionally within 30 m of a shared place anchor."""
    polygons = _polygons()[barangay]
    if place is not None:
        anchor = pick_point(barangay, place)
        rng = random.Random(f"jitter\0{barangay}\0{place}\0{seed_key}")
        lat, lon = map(math.radians, anchor)
        for _ in range(_ATTEMPTS):
            distance = _JITTER_M * math.sqrt(rng.random()) / _EARTH_RADIUS_M
            bearing = rng.uniform(0, 2 * math.pi)
            jitter_lat = math.asin(
                math.sin(lat) * math.cos(distance)
                + math.cos(lat) * math.sin(distance) * math.cos(bearing)
            )
            jitter_lon = lon + math.atan2(
                math.sin(bearing) * math.sin(distance) * math.cos(lat),
                math.cos(distance) - math.sin(lat) * math.sin(jitter_lat),
            )
            point = (math.degrees(jitter_lat), math.degrees(jitter_lon))
            if _inside(point[1], point[0], polygons):
                return point
        return anchor  # even a narrow polygon keeps the pin inside and within 30 m
    min_lon, min_lat, max_lon, max_lat = _bbox(polygons)
    seed = int(hashlib.sha256(f"{barangay}\0{seed_key}".encode()).hexdigest(), 16) % 2**32
    rng = random.Random(seed)
    for _ in range(_ATTEMPTS):
        lon = rng.uniform(min_lon, max_lon)
        lat = rng.uniform(min_lat, max_lat)
        if _inside(lon, lat, polygons):
            return lat, lon
    # Fallback: grid scan always finds a point, in a fixed order.
    for i in range(100):
        for j in range(100):
            lon = min_lon + (max_lon - min_lon) * (i + 0.5) / 100
            lat = min_lat + (max_lat - min_lat) * (j + 0.5) / 100
            if _inside(lon, lat, polygons):
                return lat, lon
    raise ValueError(f"no point found inside {barangay}")

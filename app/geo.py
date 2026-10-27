"""Map a GPS point to a Biñan barangay, and read GPS from photo EXIF.

Boundaries: app/data/binan_barangays.geojson (OCHA COD-AB, CC BY-IGO; see app.gazetteer).
24 small polygons, so a plain ray-casting test is fast enough and needs no GIS library.
"""

import io
import json
import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.gazetteer import BY_PCODE, Barangay

logger = logging.getLogger(__name__)
BOUNDARIES = Path(__file__).parent / "data" / "binan_barangays.geojson"

Ring = list[tuple[float, float]]  # (lon, lat) pairs, GeoJSON order
Polygon = list[Ring]  # outer ring first, then holes


@dataclass(frozen=True)
class _Area:
    barangay: Barangay
    polygons: list[Polygon]
    bbox: tuple[float, float, float, float]  # min lon, min lat, max lon, max lat


@cache
def _areas() -> list[_Area]:
    collection = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    areas = []
    for feature in collection["features"]:
        geometry = feature["geometry"]
        coords = geometry["coordinates"]
        polygons = [coords] if geometry["type"] == "Polygon" else coords
        polygons = [[[tuple(p) for p in ring] for ring in poly] for poly in polygons]
        lons = [p[0] for poly in polygons for p in poly[0]]
        lats = [p[1] for poly in polygons for p in poly[0]]
        areas.append(
            _Area(
                BY_PCODE[feature["properties"]["pcode"]],
                polygons,
                (min(lons), min(lats), max(lons), max(lats)),
            )
        )
    return areas


def _in_ring(lon: float, lat: float, ring: Ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(lon: float, lat: float, polygon: Polygon) -> bool:
    outer, *holes = polygon
    return _in_ring(lon, lat, outer) and not any(_in_ring(lon, lat, h) for h in holes)


def barangay_at(lat: float, lon: float) -> Barangay | None:
    """The barangay containing the point, or None if it is outside Biñan."""
    for area in _areas():
        min_lon, min_lat, max_lon, max_lat = area.bbox
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if any(_in_polygon(lon, lat, poly) for poly in area.polygons):
            return area.barangay
    return None


_GPS_IFD = 0x8825
_LAT_REF, _LAT, _LON_REF, _LON = 1, 2, 3, 4


def exif_gps(data: bytes) -> tuple[float, float] | None:
    """(lat, lon) from a photo's EXIF GPS tags, or None.

    Telegram strips EXIF from photos sent the normal way; it survives only when the image is
    sent as a file. The coordinates are personal data: callers store them like a location pin
    and must not log them.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            gps = image.getexif().get_ifd(_GPS_IFD)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    try:
        lat = _degrees(gps[_LAT], gps.get(_LAT_REF, "N"))
        lon = _degrees(gps[_LON], gps.get(_LON_REF, "E"))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _degrees(dms, ref: str) -> float:
    degrees, minutes, seconds = (float(x) for x in dms)
    value = degrees + minutes / 60 + seconds / 3600
    return -value if str(ref).upper() in ("S", "W") else value

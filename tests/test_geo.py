import io

import pytest
from PIL import Image

from app.gazetteer import BARANGAYS, BY_NAME
from app.geo import barangay_at, exif_gps

# Centre points published with COD-AB (adm4 center_lat / center_lon), rounded to 5 decimals.
CENTRES = {
    "Biñan": (14.26429, 121.04994),
    "Bungahan": (14.30233, 121.07430),
    "Santo Tomas": (14.31440, 121.07267),
    "Canlalay": (14.34294, 121.07252),
    "Casile": (14.34407, 121.08842),
    "De La Paz": (14.35180, 121.08020),
    "Ganado": (14.28647, 121.08240),
    "San Francisco": (14.33351, 121.05588),
    "Langkiwa": (14.29786, 121.06130),
    "Loma": (14.28493, 121.06900),
    "Malaban": (14.34617, 121.08924),
    "Malamig": (14.27554, 121.05012),
    "Mamplasan": (14.29574, 121.08137),
    "Platero": (14.32263, 121.09238),
    "Poblacion": (14.33830, 121.08435),
    "Santo Niño": (14.32692, 121.08389),
    "San Antonio": (14.33373, 121.09113),
    "San Jose": (14.34030, 121.08265),
    "San Vicente": (14.33199, 121.08035),
    "Soro-soro": (14.32750, 121.05888),
    "Santo Domingo": (14.33870, 121.08194),
    "Timbao": (14.28292, 121.05636),
    "Tubigan": (14.33140, 121.07104),
    "Zapote": (14.31537, 121.08402),
}


def test_every_barangay_has_a_boundary_containing_its_centre():
    assert set(CENTRES) == {b.name for b in BARANGAYS}
    for name, (lat, lon) in CENTRES.items():
        assert barangay_at(lat, lon) == BY_NAME[name], name


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (14.3500, 121.1500),  # Laguna de Bay, east of the shore
        (14.5995, 120.9842),  # Manila
        (14.2700, 121.1200),  # Santa Rosa side
        (0.0, 0.0),
    ],
)
def test_points_outside_biñan_have_no_barangay(lat, lon):
    assert barangay_at(lat, lon) is None


def jpeg_with_gps(lat_dms, lat_ref, lon_dms, lon_ref) -> bytes:
    image = Image.new("RGB", (8, 8), "gray")
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps.update({1: lat_ref, 2: lat_dms, 3: lon_ref, 4: lon_dms})
    buf = io.BytesIO()
    image.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_exif_gps_is_read_in_decimal_degrees():
    data = jpeg_with_gps((14.0, 20.0, 17.88), "N", (121.0, 4.0, 52.2), "E")
    lat, lon = exif_gps(data)
    assert lat == pytest.approx(14.33830, abs=1e-5)
    assert lon == pytest.approx(121.08117, abs=1e-5)


def test_southern_and_western_refs_are_negative():
    lat, lon = exif_gps(jpeg_with_gps((10.0, 30.0, 0.0), "S", (20.0, 15.0, 0.0), "W"))
    assert (lat, lon) == (pytest.approx(-10.5), pytest.approx(-20.25))


@pytest.mark.parametrize(
    "data",
    [
        b"not an image",
        b"",
    ],
)
def test_unreadable_data_has_no_gps(data):
    assert exif_gps(data) is None


def test_photo_without_gps_has_none():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")
    assert exif_gps(buf.getvalue()) is None

"""Coordinate parsing and geodesy.

A parsing bug here puts the aircraft over the wrong piece of the planet, so the
rule is: parse unambiguously or refuse, never guess.
"""

from __future__ import annotations


import pytest

from dronegoto.geo import (
    CoordinateError,
    GeoPoint,
    bearing_deg,
    haversine_m,
    interpolate,
    parse_coordinates,
    project,
)

BIG_BEN = (51.5007, -0.1246)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("51.5074, -0.1278", (51.5074, -0.1278)),
        ("51.5074,-0.1278", (51.5074, -0.1278)),
        ("51.5074 -0.1278", (51.5074, -0.1278)),
        ("  51.5074 , -0.1278  ", (51.5074, -0.1278)),
        ('"51.5074, -0.1278"', (51.5074, -0.1278)),
        ("51.5074N, 0.1278W", (51.5074, -0.1278)),
        ("33.8688S, 151.2093E", (-33.8688, 151.2093)),
        ("0, 0", (0.0, 0.0)),
        ("-90, 180", (-90.0, 180.0)),
    ],
)
def test_decimal_pairs(text, expected):
    point = parse_coordinates(text)
    assert (point.lat, point.lon) == pytest.approx(expected)


def test_dms_pair():
    point = parse_coordinates("""51°30'26.6"N 0°07'39.8"W""")
    assert point.lat == pytest.approx(51.50739, abs=1e-4)
    assert point.lon == pytest.approx(-0.12772, abs=1e-4)


def test_dms_with_spaces_and_reversed_order():
    point = parse_coordinates("""0°07'39.8"W 51°30'26.6"N""")
    assert point.lat == pytest.approx(51.50739, abs=1e-4)
    assert point.lon == pytest.approx(-0.12772, abs=1e-4)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.google.com/maps/@51.5074,-0.1278,15z", (51.5074, -0.1278)),
        ("https://maps.google.com/?q=51.5074,-0.1278", (51.5074, -0.1278)),
        ("https://www.google.com/maps?q=loc:51.5074,-0.1278", (51.5074, -0.1278)),
    ],
)
def test_google_maps_urls(url, expected):
    point = parse_coordinates(url)
    assert (point.lat, point.lon) == pytest.approx(expected)


def test_place_url_prefers_the_pin_over_the_camera_position():
    """`@lat,lon` is where the map view sits; `!3d/!4d` is the place itself. The
    user means the place."""
    url = (
        "https://www.google.com/maps/place/Big+Ben/@51.4000,-0.2000,17z/"
        "data=!3m1!4b1!4m5!3m4!8m2!3d51.5007292!4d-0.1246254"
    )
    point = parse_coordinates(url)
    assert (point.lat, point.lon) == pytest.approx(BIG_BEN, abs=1e-3)


def test_shortlink_is_refused_with_an_actionable_message():
    with pytest.raises(CoordinateError, match="cannot be resolved offline"):
        parse_coordinates("https://maps.app.goo.gl/abcd1234")


def test_unrelated_url_is_refused_rather_than_mined_for_digits():
    with pytest.raises(CoordinateError, match="unrecognised URL"):
        parse_coordinates("https://example.com/51.5/0.12")


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not coordinates", "51.5074", "999, 999", "91, 0", "0, 181",
     "51.5074N, 0.1278N", "-51.5074N, 0.1278W"],
)
def test_bad_input_is_refused(text):
    with pytest.raises(CoordinateError):
        parse_coordinates(text)


def test_geopoint_rejects_non_finite():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(CoordinateError):
            GeoPoint(bad, 0.0)


# ---------------------------------------------------------------------------
# Geodesy
# ---------------------------------------------------------------------------

def test_haversine_against_a_known_distance():
    """London to Paris, ~343 km."""
    london, paris = GeoPoint(51.5074, -0.1278), GeoPoint(48.8566, 2.3522)
    assert haversine_m(london, paris) == pytest.approx(343_500, rel=0.01)


def test_haversine_is_symmetric_and_zero_on_itself():
    a, b = GeoPoint(51.5, -0.12), GeoPoint(51.6, -0.10)
    assert haversine_m(a, b) == pytest.approx(haversine_m(b, a))
    assert haversine_m(a, a) == 0.0


@pytest.mark.parametrize("heading", [0.0, 45.0, 90.0, 180.0, 270.0, 359.0])
def test_project_then_measure_round_trips(heading):
    origin = GeoPoint(51.5074, -0.1278)
    destination = project(origin, heading, 2500.0)
    assert haversine_m(origin, destination) == pytest.approx(2500.0, rel=1e-6)
    assert bearing_deg(origin, destination) == pytest.approx(heading, abs=1e-6)


def test_projection_across_the_antimeridian_stays_valid():
    """Longitude must wrap back into range rather than producing an unbuildable point."""
    point = project(GeoPoint(0.0, 179.99), 90.0, 5000.0)
    assert -180.0 <= point.lon <= 180.0
    assert point.lon < 0, "should have wrapped to the western hemisphere"


def test_interpolate_endpoints_and_spacing():
    a, b = GeoPoint(51.5074, -0.1278), GeoPoint(51.5312, -0.1123)
    points = interpolate(a, b, 5)
    assert len(points) == 5
    assert haversine_m(points[0], a) == pytest.approx(0.0, abs=0.01)
    assert haversine_m(points[-1], b) == pytest.approx(0.0, abs=0.01)
    gaps = [haversine_m(points[i], points[i + 1]) for i in range(4)]
    assert max(gaps) - min(gaps) < 0.1, "samples should be evenly spaced"


def test_interpolate_on_a_zero_length_route():
    a = GeoPoint(51.5, -0.12)
    assert interpolate(a, a, 3) == [a, a, a]


def test_interpolate_requires_at_least_two_samples():
    with pytest.raises(ValueError):
        interpolate(GeoPoint(0, 0), GeoPoint(1, 1), 1)

"""Geodesy and coordinate parsing.

Pure functions only - no I/O, no clock. Everything here is deterministic and
unit-testable, which matters because a coordinate-parsing bug puts the aircraft
over the wrong piece of the planet.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterator

# IUGG mean Earth radius. Using the mean (not equatorial) keeps haversine error
# under ~0.5% at any latitude, which is well inside our safety margins.
EARTH_RADIUS_M = 6_371_008.8


class CoordinateError(ValueError):
    """Raised when input cannot be resolved to an unambiguous position."""


@dataclass(frozen=True)
class GeoPoint:
    """A WGS84 position. Immutable, validated at construction."""

    lat: float
    lon: float

    def __post_init__(self) -> None:
        for name, value in (("lat", self.lat), ("lon", self.lon)):
            if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
                raise CoordinateError(f"{name} must be a finite number, got {value!r}")
        if not -90.0 <= self.lat <= 90.0:
            raise CoordinateError(f"latitude {self.lat} outside [-90, 90]")
        if not -180.0 <= self.lon <= 180.0:
            raise CoordinateError(f"longitude {self.lon} outside [-180, 180]")

    def __str__(self) -> str:
        return f"{self.lat:.6f}, {self.lon:.6f}"


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = math.radians(b.lat - a.lat)
    dlambda = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def bearing_deg(a: GeoPoint, b: GeoPoint) -> float:
    """Initial great-circle bearing from a to b, degrees clockwise from north."""
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dlambda = math.radians(b.lon - a.lon)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.degrees(math.atan2(y, x)) % 360.0


def project(origin: GeoPoint, bearing: float, distance_m: float) -> GeoPoint:
    """Point reached by travelling `distance_m` from `origin` along `bearing`."""
    if distance_m < 0:
        raise ValueError("distance_m must be non-negative")
    delta = distance_m / EARTH_RADIUS_M
    theta = math.radians(bearing)
    phi1, lambda1 = math.radians(origin.lat), math.radians(origin.lon)
    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    # Normalise longitude back into [-180, 180] so a projection that crosses the
    # antimeridian stays constructible.
    lon = (math.degrees(lambda2) + 540.0) % 360.0 - 180.0
    return GeoPoint(math.degrees(phi2), lon)


def interpolate(a: GeoPoint, b: GeoPoint, samples: int) -> list[GeoPoint]:
    """`samples` points evenly spaced along the great circle from a to b, inclusive."""
    if samples < 2:
        raise ValueError("samples must be >= 2")
    total = haversine_m(a, b)
    if total == 0.0:
        return [a] * samples
    heading = bearing_deg(a, b)
    # Re-deriving the bearing from `a` each step would drift on long routes; for
    # the distances this system permits (single-digit km) a constant initial
    # bearing is accurate to well under a metre.
    return [project(a, heading, total * i / (samples - 1)) for i in range(samples)]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_DECIMAL_PAIR = re.compile(
    r"^\s*(?P<lat>[+-]?\d{1,3}(?:\.\d+)?)\s*(?P<lathem>[NnSs])?\s*[,;\s]\s*"
    r"(?P<lon>[+-]?\d{1,3}(?:\.\d+)?)\s*(?P<lonhem>[EeWw])?\s*$"
)

# 51°30'26.6"N  /  51 30 26.6 N  /  51°30.443'N
_DMS = re.compile(
    r"(?P<deg>\d{1,3})\s*[°d:\s]\s*"
    r"(?:(?P<min>\d{1,2}(?:\.\d+)?)\s*['m:\s]\s*)?"
    r"(?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*[\"s]?\s*)?"
    r"(?P<hem>[NSEWnsew])"
)

# Google place URLs carry the camera position as @lat,lon,zoom and the actual
# pinned place as !3dLAT!4dLON. The pin is what the user means, so it wins.
_GMAPS_PIN = re.compile(r"!3d(?P<lat>[+-]?\d+(?:\.\d+)?)!4d(?P<lon>[+-]?\d+(?:\.\d+)?)")
_GMAPS_AT = re.compile(r"@(?P<lat>[+-]?\d+(?:\.\d+)?),(?P<lon>[+-]?\d+(?:\.\d+)?)")
_GMAPS_QUERY = re.compile(
    r"[?&](?:q|query|ll|daddr|destination)=(?:loc:)?"
    r"(?P<lat>[+-]?\d+(?:\.\d+)?)(?:,|%2C)(?P<lon>[+-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_SHORTLINK = re.compile(r"^https?://(?:goo\.gl/maps|maps\.app\.goo\.gl)/", re.IGNORECASE)


def _apply_hemisphere(value: float, hem: str | None, axis: str) -> float:
    if not hem:
        return value
    hem = hem.upper()
    if axis == "lat" and hem not in ("N", "S"):
        raise CoordinateError(f"latitude hemisphere must be N or S, got {hem!r}")
    if axis == "lon" and hem not in ("E", "W"):
        raise CoordinateError(f"longitude hemisphere must be E or W, got {hem!r}")
    if value < 0:
        raise CoordinateError(f"cannot combine a negative value with hemisphere {hem!r}")
    return -value if hem in ("S", "W") else value


def _dms_to_decimal(match: re.Match[str]) -> tuple[float, str]:
    deg = float(match.group("deg"))
    minutes = float(match.group("min") or 0.0)
    seconds = float(match.group("sec") or 0.0)
    if minutes >= 60 or seconds >= 60:
        raise CoordinateError(f"invalid DMS component in {match.group(0)!r}")
    return deg + minutes / 60.0 + seconds / 3600.0, match.group("hem").upper()


def parse_coordinates(text: str) -> GeoPoint:
    """Resolve user input to a GeoPoint.

    Accepts decimal pairs, DMS, and Google Maps URLs (place pin, camera
    position, or query parameter). Raises CoordinateError with an actionable
    message rather than guessing when input is ambiguous.
    """
    if not isinstance(text, str) or not text.strip():
        raise CoordinateError("no coordinate supplied")
    raw = text.strip().strip("\"'")

    if _SHORTLINK.match(raw):
        raise CoordinateError(
            "shortened Google Maps links cannot be resolved offline. Open the link, "
            "then copy the full URL or the coordinates themselves."
        )

    if "google." in raw.lower() and "/maps" in raw.lower() or "maps.google" in raw.lower():
        for pattern in (_GMAPS_PIN, _GMAPS_QUERY, _GMAPS_AT):
            m = pattern.search(raw)
            if m:
                return GeoPoint(float(m.group("lat")), float(m.group("lon")))
        raise CoordinateError(f"no coordinates found in Google Maps URL: {raw!r}")

    # A bare URL that isn't Google Maps - be explicit rather than fall through to
    # the number matchers, which would happily parse digits out of a hostname.
    if re.match(r"^https?://", raw, re.IGNORECASE):
        raise CoordinateError(f"unrecognised URL, expected a Google Maps link: {raw!r}")

    m = _DECIMAL_PAIR.match(raw)
    if m:
        lat = _apply_hemisphere(float(m.group("lat")), m.group("lathem"), "lat")
        lon = _apply_hemisphere(float(m.group("lon")), m.group("lonhem"), "lon")
        return GeoPoint(lat, lon)

    dms = list(_DMS.finditer(raw))
    if len(dms) == 2:
        first, first_hem = _dms_to_decimal(dms[0])
        second, second_hem = _dms_to_decimal(dms[1])
        values = {first_hem: first, second_hem: second}
        if len(values) != 2:
            raise CoordinateError(f"duplicate hemisphere in {raw!r}")
        lat_hem = next((h for h in values if h in "NS"), None)
        lon_hem = next((h for h in values if h in "EW"), None)
        if lat_hem is None or lon_hem is None:
            raise CoordinateError(f"need one N/S and one E/W component in {raw!r}")
        return GeoPoint(
            _apply_hemisphere(values[lat_hem], lat_hem, "lat"),
            _apply_hemisphere(values[lon_hem], lon_hem, "lon"),
        )

    raise CoordinateError(
        f"could not parse {raw!r}. Expected 'lat, lon', a DMS pair, or a Google Maps URL."
    )

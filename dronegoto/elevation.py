"""Terrain elevation along the route.

Google Maps hands you a latitude and a longitude and nothing else. Fly a fixed
*relative* altitude toward ground that rises 80 m and you hit the hill - and no
altitude check measured from the launch point can see it coming. This module
turns a route into a ground profile so a cruise altitude can be chosen that
clears the highest point on it.

Network access is optional. If elevation data cannot be fetched the profile is
None, and `terrain.required` decides whether that blocks the flight.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import TerrainConfig
from .geo import GeoPoint, haversine_m, interpolate

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerrainProfile:
    """Ground elevation sampled along a route, in metres AMSL."""

    points: tuple[GeoPoint, ...]
    elevations: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.points) != len(self.elevations):
            raise ValueError("points and elevations must be the same length")
        if not self.points:
            raise ValueError("terrain profile cannot be empty")

    @property
    def max_elevation_m(self) -> float:
        return max(self.elevations)

    @property
    def min_elevation_m(self) -> float:
        return min(self.elevations)

    @property
    def home_elevation_m(self) -> float:
        return self.elevations[0]

    def elevation_at(self, point: GeoPoint) -> float:
        """Elevation of the nearest sample.

        Nearest-sample rather than interpolation: between samples the true ground
        may be higher than either endpoint, and rounding a hilltop down is the
        one error this module must never make.
        """
        best_index, best_distance = 0, float("inf")
        for i, sample in enumerate(self.points):
            distance = haversine_m(point, sample)
            if distance < best_distance:
                best_index, best_distance = i, distance
        return self.elevations[best_index]

    def required_cruise_amsl(self, clearance_m: float) -> float:
        return self.max_elevation_m + clearance_m


class ElevationSource(Protocol):
    def lookup(self, points: Sequence[GeoPoint]) -> list[float] | None: ...


class FlatTerrain:
    """Constant-elevation source. Used in tests and when terrain is disabled."""

    def __init__(self, elevation_m: float = 0.0) -> None:
        self.elevation_m = elevation_m

    def lookup(self, points: Sequence[GeoPoint]) -> list[float]:
        return [self.elevation_m] * len(points)


class OpenTopoData:
    """Elevation from an OpenTopoData-compatible HTTP API, with an on-disk cache.

    Failures return None rather than raising: an unreachable elevation service is
    a reason to fall back to the configured policy, not to crash a preflight.
    """

    def __init__(self, config: TerrainConfig) -> None:
        self.config = config
        self._cache_path = Path(config.cache_path)
        self._cache: dict[str, float] = self._load_cache()

    def _load_cache(self) -> dict[str, float]:
        try:
            return json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache))
        except OSError as exc:
            log.debug("could not write elevation cache: %s", exc)

    @staticmethod
    def _key(point: GeoPoint) -> str:
        # ~11 m resolution, which is finer than SRTM30m itself.
        return f"{point.lat:.4f},{point.lon:.4f}"

    def lookup(self, points: Sequence[GeoPoint]) -> list[float] | None:
        missing = [p for p in points if self._key(p) not in self._cache]
        if missing and not self._fetch(missing):
            return None
        return [self._cache[self._key(p)] for p in points]

    def _fetch(self, points: Sequence[GeoPoint]) -> bool:
        locations = "|".join(f"{p.lat:.6f},{p.lon:.6f}" for p in points)
        url = f"{self.config.provider_url}?locations={locations}"
        try:
            with urllib.request.urlopen(url, timeout=self.config.timeout_s) as response:
                payload = json.load(response)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            log.warning("elevation lookup failed: %s", exc)
            return False

        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(points):
            log.warning("elevation provider returned %s results for %s points",
                        len(results) if isinstance(results, list) else "no", len(points))
            return False

        for point, result in zip(points, results):
            elevation = result.get("elevation")
            if elevation is None:
                # A null elevation means "no data here" (ocean, gap in coverage).
                # Treating it as sea level would silently lower the cruise
                # altitude, so the whole lookup fails instead.
                log.warning("elevation provider has no data for %s", point)
                return False
            self._cache[self._key(point)] = float(elevation)
        self._save_cache()
        return True


def build_source(config: TerrainConfig) -> ElevationSource:
    return OpenTopoData(config) if config.enabled else FlatTerrain()


def profile_route(
    start: GeoPoint,
    end: GeoPoint,
    config: TerrainConfig,
    source: ElevationSource | None = None,
) -> TerrainProfile | None:
    """Sample ground elevation along the great circle from start to end."""
    if not config.enabled:
        return None
    source = source if source is not None else build_source(config)
    points = interpolate(start, end, config.samples)
    elevations = source.lookup(points)
    if elevations is None:
        return None
    return TerrainProfile(points=tuple(points), elevations=tuple(elevations))

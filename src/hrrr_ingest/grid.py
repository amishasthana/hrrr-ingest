"""Nearest-grid-point lookup.

The HRRR grid is Lambert Conformal and GRIB stores longitudes as 0..360, so
a KD-tree on raw (lat, lon) degrees is wrong twice over: -122 never matches
237.6, and a degree of longitude is not the same distance as a degree of
latitude. We normalise longitudes and search on 3-D unit vectors, where
Euclidean distance is monotonic in great-circle distance.
Basically, this module provides a fast way to find the nearest grid point to a given latitude and longitude on the HRRR grid.
It takes care of normalising longitudes and converting latitude/longitude pairs to 3-D unit vectors for accurate nearest-grid-point lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class GridPoint:
    index: int          # flat index into the GRIB values array
    latitude: float     # float32-rounded, exactly what DuckDB's FLOAT stores
    longitude: float
    distance_km: float  # from the requested point (informational)


def _normalise_lon(lon: np.ndarray) -> np.ndarray:
    return (np.asarray(lon, dtype=np.float64) + 180.0) % 360.0 - 180.0


def _to_xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    return np.column_stack((
        np.cos(lat_r) * np.cos(lon_r),
        np.cos(lat_r) * np.sin(lon_r),
        np.sin(lat_r),
    ))


class GridLocator:
    def __init__(self, latitudes: np.ndarray, longitudes: np.ndarray):
        self.latitudes = np.asarray(latitudes, dtype=np.float64).ravel()
        self.longitudes = _normalise_lon(longitudes).ravel()
        if self.latitudes.shape != self.longitudes.shape:
            raise ValueError("latitude/longitude arrays differ in size")
        self._tree = cKDTree(_to_xyz(self.latitudes, self.longitudes))

    @property
    def size(self) -> int:
        return self.latitudes.size

    def nearest(self, points: list[tuple[float, float]]) -> list[GridPoint]:
        lat = np.array([p[0] for p in points], dtype=np.float64)
        lon = _normalise_lon([p[1] for p in points])
        chord, idx = self._tree.query(_to_xyz(lat, lon))
        dist_km = 2 * EARTH_RADIUS_KM * np.arcsin(np.clip(chord / 2, 0, 1))
        return [
            GridPoint(
                index=int(i),
                latitude=float(np.float32(self.latitudes[i])),
                longitude=float(np.float32(self.longitudes[i])),
                distance_km=float(d),
            )
            for i, d in zip(np.atleast_1d(idx), np.atleast_1d(dist_km))
        ]

    # --- caching: the HRRR grid never changes, so download it once ---------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, lat=self.latitudes, lon=self.longitudes)
        tmp.replace(path)  # atomic: never leave a half-written cache

    @classmethod
    def load(cls, path: Path) -> "GridLocator":
        with np.load(path) as data:
            return cls(data["lat"], data["lon"])

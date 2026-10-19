"""Read the points file: one ``lat,lon`` pair per line."""

from __future__ import annotations

from pathlib import Path


class PointsFileError(ValueError):
    pass


def parse_points(text: str) -> list[tuple[float, float]]:
    """Parse ``lat,lon`` lines. Blank lines and ``#`` comments are skipped.

    Longitude may be -180..180 or 0..360. Raises ``PointsFileError`` naming
    the bad line, so the user can fix the file.
    """
    points = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            raise PointsFileError(f"line {lineno}: expected 'lat,lon', got {raw!r}")
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            raise PointsFileError(f"line {lineno}: not numbers: {raw!r}") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 360):
            raise PointsFileError(f"line {lineno}: out of range: {raw!r}")
        points.append((lat, lon))
    if not points:
        raise PointsFileError("no points found")
    return points


def read_points(path: str | Path) -> list[tuple[float, float]]:
    return parse_points(Path(path).read_text())

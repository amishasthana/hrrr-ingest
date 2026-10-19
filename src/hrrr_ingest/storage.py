"""DuckDB persistence for hrrr_forecasts.

The primary key makes duplicate rows impossible, and ``INSERT OR IGNORE``
makes re-inserting the same data a no-op. The planner also asks
``existing_keys`` before downloading, so already-ingested data isn't even
fetched.

Trade-offs:
  * ``INSERT OR IGNORE`` never overwrites. If NOAA republished a corrected
    file, stored values would not change. Switch to ``INSERT OR REPLACE``
    if corrections matter more than write speed.
  * latitude/longitude are FLOAT (float32) and part of the key. Values are
    written exactly as float32, so equality is reliable inside this tool,
    but ad-hoc SQL filters should use the stored values or ROUND().
  * ``executemany`` inserts row by row. That's fine for hundreds of points;
    for thousands, bulk-load through Arrow/pandas instead.
  * DuckDB allows one writer per file, so two runs can't share data.db at
    the same time (the CLI reports this as exit code 3).
  * ``value`` is NULL where the GRIB bitmap marks a cell as missing. Such
    rows count as "done" and are not re-fetched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS hrrr_forecasts (
    valid_time_utc TIMESTAMP NOT NULL,
    run_time_utc   TIMESTAMP NOT NULL,
    latitude       FLOAT     NOT NULL,
    longitude      FLOAT     NOT NULL,
    variable       VARCHAR   NOT NULL,
    value          FLOAT,
    source_s3      VARCHAR   NOT NULL,
    PRIMARY KEY (run_time_utc, valid_time_utc, latitude, longitude, variable)
)
"""


@dataclass(frozen=True)
class ForecastRow:
    """One row of hrrr_forecasts. Times are naive datetimes in UTC."""

    valid_time_utc: datetime
    run_time_utc: datetime
    latitude: float
    longitude: float
    variable: str
    value: float | None
    source_s3: str


class ForecastRepository:
    """Owns the DuckDB connection and the hrrr_forecasts schema.

    Use as a context manager so the connection (and its file lock) is
    released when the run ends.
    """

    def __init__(self, path: str | Path = "data.db"):
        """Open (or create) the database and ensure the table exists.

        Raises ``duckdb.IOException`` if another process holds the lock.
        """
        self.conn = duckdb.connect(str(path))
        self.conn.execute(SCHEMA)

    def existing_keys(
        self, source_s3: str, variables: list[str]
    ) -> set[tuple[str, float, float]]:
        """(variable, latitude, longitude) already stored for this file."""
        if not variables:
            return set()
        placeholders = ", ".join("?" for _ in variables)
        rows = self.conn.execute(
            f"SELECT variable, latitude, longitude FROM hrrr_forecasts "
            f"WHERE source_s3 = ? AND variable IN ({placeholders})",
            [source_s3, *variables],
        ).fetchall()
        return {(v, float(lat), float(lon)) for v, lat, lon in rows}

    def insert(self, rows: list[ForecastRow]) -> None:
        """Insert atomically; rows that already exist are ignored."""
        if not rows:
            return
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.executemany(
                "INSERT OR IGNORE INTO hrrr_forecasts "
                "(valid_time_utc, run_time_utc, latitude, longitude, variable, value, source_s3) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (r.valid_time_utc, r.run_time_utc, r.latitude, r.longitude,
                     r.variable, r.value, r.source_s3)
                    for r in rows
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def count(self) -> int:
        """Total rows in hrrr_forecasts (used by tests and diagnostics)."""
        return self.conn.execute("SELECT count(*) FROM hrrr_forecasts").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ForecastRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

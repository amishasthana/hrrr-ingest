"""Where HRRR files live and how they are named.

All knowledge of the bucket layout is in this module, so pointing the tool at
another run hour, product (e.g. wrfprs) or model only touches this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .transport import Transport

log = logging.getLogger(__name__)

BUCKET = "noaa-hrrr-bdp-pds"
HTTP_BASE = f"https://{BUCKET}.s3.amazonaws.com"
# The spec asks for source_s3 values of exactly this form.
S3_BASE = f"s3://{BUCKET}.s3.amazonaws.com"

RUN_HOUR = 6                 # the 06z run
MAX_FORECAST_HOURS = 48      # 06z is an extended run that goes out to f48


@dataclass(frozen=True)
class ForecastFile:
    """One GRIB2 file: a single run time and forecast hour.

    Times are naive datetimes in UTC, matching DuckDB's TIMESTAMP type.
    ``url``/``idx_url`` are what we download over HTTPS; ``source_s3`` is the
    form the spec asks us to store.
    """

    run_time: datetime
    forecast_hour: int

    @property
    def key(self) -> str:
        return (
            f"hrrr.{self.run_time:%Y%m%d}/conus/"
            f"hrrr.t{self.run_time:%H}z.wrfsfcf{self.forecast_hour:02d}.grib2"
        )

    @property
    def url(self) -> str:
        return f"{HTTP_BASE}/{self.key}"

    @property
    def idx_url(self) -> str:
        return f"{self.url}.idx"

    @property
    def source_s3(self) -> str:
        return f"{S3_BASE}/{self.key}"

    @property
    def valid_time(self) -> datetime:
        return self.run_time + timedelta(hours=self.forecast_hour)


def run_time_for(run_date: date) -> datetime:
    """The 06z run time (naive UTC) for a calendar date."""
    return datetime.combine(run_date, time(hour=RUN_HOUR))


def forecast_hours(num_hours: int) -> range:
    """Forecast hours to ingest for ``--num-hours``.

    Interpretation: N hours of data starting at the analysis, i.e. f00..f(N-1).
    Change this one function if you prefer f00..fN.
    """
    if not 1 <= num_hours <= MAX_FORECAST_HOURS:
        raise ValueError(f"num_hours must be between 1 and {MAX_FORECAST_HOURS}")
    return range(num_hours)


def forecast_files(run_date: date, num_hours: int) -> list[ForecastFile]:
    """All files to ingest for one run date.

    To support a date range later, call this once per date and concatenate;
    the pipeline already works on an arbitrary list of files.
    """
    run_time = run_time_for(run_date)
    return [ForecastFile(run_time, h) for h in forecast_hours(num_hours)]


def latest_complete_run_date(
    transport: Transport, today: date | None = None, lookback_days: int = 5
) -> date:
    """Most recent date whose 06z run has published its final forecast hour.

    The last file (f48) is published last, so its .idx existing is a cheap
    proxy for "the run is complete".

    Trade-off: this requires f48 even when ``--num-hours`` asks for fewer
    hours, so a run that is complete enough for a short request may still
    be skipped in favour of the previous day.
    """
    today = today or datetime.now(timezone.utc).date()
    for offset in range(lookback_days + 1):
        candidate = today - timedelta(days=offset)
        last = ForecastFile(run_time_for(candidate), MAX_FORECAST_HOURS)
        if transport.exists(last.idx_url):
            return candidate
        log.debug("Run %s not complete yet (%s missing)", candidate, last.key)
    raise LookupError(
        f"No complete {RUN_HOUR:02d}z run found in the last {lookback_days} days"
    )

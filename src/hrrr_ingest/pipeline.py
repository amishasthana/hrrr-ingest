"""Orchestration: plan -> fetch (parallel) -> decode -> store, one file at a time.

The unit of work is one forecast file. Each file succeeds or fails on its
own, so one missing hour never sinks the whole run, and every file's rows are
committed in a single transaction, so an interrupted run resumes cleanly.

Flow of ``Ingestor.run``:

  1. Load (or download and cache) the grid, map input points to grid cells,
     and merge points that share a cell.
  2. Plan: for each forecast file, ask the DB which (variable, cell)
     combinations are already stored; keep only variables with gaps.
  3. Fetch planned files in a thread pool (I/O bound): .idx, then one HTTP
     range request per variable.
  4. On the main thread, as each download completes: decode, pick the target
     cells, insert in one transaction, and classify the outcome.

Trade-offs:
  * Atomic per file: if one variable fails to decode, none of that file's
    variables are stored this run (the next run retries the whole file).
  * "Done" is derived from the table itself (no separate manifest). That keeps
    one source of truth, but "missing" and "never tried" look the same, so
    missing hours are re-checked on every run (cost: one small .idx request).
  * Variables absent from a file (e.g. solar flux at f00) give ``PARTIAL``,
    which does not change the exit code.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

from .catalog import ForecastFile, forecast_files
from .decode import decode_message
from .grid import GridLocator, GridPoint
from .idx import find_entry, parse_idx
from .storage import ForecastRepository, ForecastRow
from .transport import NotFoundError, Transport
from .variables import VariableSpec

log = logging.getLogger(__name__)

# Points further than this from any grid cell are almost certainly outside
# the HRRR domain (grid spacing is ~3 km).
MAX_REASONABLE_DISTANCE_KM = 10.0


class Status(str, Enum):
    INGESTED = "ingested"   # everything requested is now stored
    SKIPPED = "skipped"     # everything requested was already stored
    PARTIAL = "partial"     # stored, but some variables aren't in this file
    MISSING = "missing"     # file not published (yet)
    FAILED = "failed"       # error after retries


@dataclass
class FileResult:
    """Outcome of one forecast file, used for the CLI summary and exit code."""

    file: ForecastFile
    status: Status
    rows: int = 0
    absent_variables: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Fetched:
    messages: dict[str, bytes]    # variable name -> raw GRIB2 message
    absent: list[str]             # requested but not in the .idx


class Ingestor:
    """Runs one ingestion request.

    Args:
        repo: where rows are stored and where "already done" is looked up.
        transport: network access (``HttpTransport`` in production, a fake
            in tests).
        grid_cache: optional ``.npz`` path; the grid is downloaded once and
            reused from here.
        max_workers: parallel downloads. Raise for throughput; lower if S3
            starts throttling (429s are retried, but slow the run).
    """

    def __init__(
        self,
        repo: ForecastRepository,
        transport: Transport,
        grid_cache: Path | None = None,
        max_workers: int = 4,
    ):
        self.repo = repo
        self.transport = transport
        self.grid_cache = grid_cache
        self.max_workers = max_workers

    # ------------------------------------------------------------------ run
    def run(
        self,
        run_date: date,
        points: list[tuple[float, float]],
        variables: list[VariableSpec],
        num_hours: int,
    ) -> list[FileResult]:
        """Ingest ``variables`` at ``points`` for one run date.

        Returns one ``FileResult`` per forecast hour, in hour order. Does not
        raise for missing or broken files; those are reported in the results.
        Raises ``RuntimeError`` only if no file at all could be read to get
        the grid.
        """
        files =forecast_files(run_date, num_hours)
        locator = self._get_locator(files, variables[0])
        targets = self._locate(locator, points)

        results: dict[ForecastFile, FileResult] = {}
        work: list[tuple[ForecastFile, list[VariableSpec]]] = []
        for f in files:
            todo = self._plan(f, variables, targets)
            if todo:
                work.append((f, todo))
            else:
                results[f] = FileResult(f, Status.SKIPPED)
        log.info("%d file(s) to fetch, %d already complete", len(work), len(results))

        # Downloads run in parallel (I/O bound); decoding and DB writes stay on
        # this thread, so DuckDB only ever has one writer.
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._fetch, f, todo): f for f, todo in work}
            for fut in as_completed(futures):
                # pop, not [], so the downloaded bytes can be freed once stored
                # (otherwise every file's GRIB messages stay in memory until the end)
                f = futures.pop(fut)
                results[f] = self._store(f, fut, targets, locator)
                r = results[f]
                log.info("%s: %s (%d rows)%s", f.key, r.status.value, r.rows,
                         f" - {r.error}" if r.error else "")
        return [results[f] for f in files]

    # ------------------------------------------------------------ helpers
    def _get_locator(self, files: list[ForecastFile], spec: VariableSpec) -> GridLocator:
        """Grid from cache, else from the first available file of this run."""
        if self.grid_cache and self.grid_cache.exists():
            return GridLocator.load(self.grid_cache)
        # Grid coordinates come from any one message of any available file.
        for f in files:
            try:
                fetched = self._fetch(f, [spec])
            except NotFoundError:
                continue
            if spec.name not in fetched.messages:
                continue
            decoded = decode_message(fetched.messages[spec.name], with_grid=True)
            locator = GridLocator(decoded.latitudes, decoded.longitudes)
            if self.grid_cache:
                locator.save(self.grid_cache)
            return locator
        raise RuntimeError("Could not download any file to read the grid from")

    @staticmethod
    def _locate(locator: GridLocator, points: list[tuple[float, float]]) -> list[GridPoint]:
        """Map points to unique grid cells.

        Points far from every cell (likely outside the HRRR domain) only log
        a warning and still use the nearest edge cell. Make this an error if
        you'd rather reject them.
        """
        unique: dict[int, GridPoint] = {}
        for (lat, lon), gp in zip(points, locator.nearest(points)):
            if gp.distance_km > MAX_REASONABLE_DISTANCE_KM:
                log.warning("Point (%s, %s) is %.0f km from the nearest grid cell; "
                            "is it outside the HRRR domain?", lat, lon, gp.distance_km)
            if gp.index in unique:
                log.info("Point (%s, %s) shares a grid cell with another point", lat, lon)
            unique.setdefault(gp.index, gp)
        return list(unique.values())

    def _plan(self, f: ForecastFile, variables: list[VariableSpec],
              targets: list[GridPoint]) -> list[VariableSpec]:
        """Variables for which at least one target point is not yet stored."""
        have = self.repo.existing_keys(f.source_s3, [v.name for v in variables])
        return [
            v for v in variables
            if any((v.name, t.latitude, t.longitude) not in have for t in targets)
        ]

    def _fetch(self, f: ForecastFile, specs: list[VariableSpec]) -> _Fetched:
        """Download only the requested messages, using .idx byte ranges."""
        entries = parse_idx(self.transport.get_text(f.idx_url))
        messages, absent = {}, []
        for spec in specs:
            entry = find_entry(entries, spec)
            if entry is None:
                absent.append(spec.name)
                continue
            messages[spec.name] = self.transport.get_range(f.url, entry.start, entry.end)
        return _Fetched(messages, absent)

    def _store(self, f: ForecastFile, fut: Future, targets: list[GridPoint],
               locator: GridLocator) -> FileResult:
        """Decode a finished download, write its rows, classify the outcome."""
        try:
            fetched: _Fetched = fut.result()
        except NotFoundError:
            return FileResult(f, Status.MISSING, error="not published")
        except Exception as exc:  # network errors after retries, bad .idx, ...
            return FileResult(f, Status.FAILED, error=f"fetch: {exc}")

        try:
            rows = []
            for name, data in fetched.messages.items():
                values = decode_message(data).values
                if values.size != locator.size:
                    raise ValueError(
                        f"{name}: grid has {values.size} points, expected "
                        f"{locator.size} (delete the grid cache if the grid changed)"
                    )
                for t in targets:
                    v = float(values[t.index])
                    rows.append(ForecastRow(
                        valid_time_utc=f.valid_time,
                        run_time_utc=f.run_time,
                        latitude=t.latitude,
                        longitude=t.longitude,
                        variable=name,
                        value=None if math.isnan(v) else v,
                        source_s3=f.source_s3,
                    ))
            self.repo.insert(rows)
        except Exception as exc:
            return FileResult(f, Status.FAILED, error=f"decode/store: {exc}")

        status = Status.PARTIAL if fetched.absent else Status.INGESTED
        return FileResult(f, status, rows=len(rows), absent_variables=fetched.absent)

"""``hrrr-ingest`` command-line entry point.

Usage::

    hrrr-ingest points.txt [--run-date YYYY-MM-DD] [--variables a,b]
                           [--num-hours N] [--db data.db] [--workers 4] [-v]

Exit codes:
    0  every requested file was ingested or already present
       (``partial`` files, where a variable isn't published, still count as 0)
    1  some files were missing (not published yet) or failed; re-run later
    2  bad input: unreadable points file or unknown variable name
    3  could not run: database locked by another run, no complete run found,
       grid could not be downloaded, or network down
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import duckdb
import requests

from .catalog import MAX_FORECAST_HOURS, latest_complete_run_date
from .pipeline import FileResult, Ingestor, Status
from .points import PointsFileError, read_points
from .storage import ForecastRepository
from .transport import HttpTransport
from .variables import VARIABLES, UnknownVariableError, resolve_variables

DEFAULT_GRID_CACHE = Path.home() / ".cache" / "hrrr-ingest" / "hrrr_conus_grid.npz"

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE, EXIT_ERROR = 0, 1, 2, 3


def _num_hours(text: str) -> int:
    n = int(text)
    if not 1 <= n <= MAX_FORECAST_HOURS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_FORECAST_HOURS}")
    return n


def build_parser() -> argparse.ArgumentParser:
    """Command-line options. The first four mirror the spec; the rest are extras."""
    p =argparse.ArgumentParser(
        prog="hrrr-ingest",
        description="Ingest HRRR 06z forecasts for a list of points into DuckDB.",
    )
    p.add_argument("points_file", type=Path, help="text file with one 'lat,lon' per line")
    p.add_argument("--run-date", type=date.fromisoformat,
                   help="forecast run date, YYYY-MM-DD (default: latest complete run)")
    p.add_argument("--variables",
                   help=f"comma-separated list (default: all). Supported: {', '.join(VARIABLES)}")
    p.add_argument("--num-hours", type=_num_hours, default=MAX_FORECAST_HOURS,
                   help=f"forecast hours to ingest, f00..f(N-1) (default: {MAX_FORECAST_HOURS})")
    p.add_argument("--db", type=Path, default=Path("data.db"), help="DuckDB file (default: data.db)")
    p.add_argument("--workers", type=int, default=4, help="parallel downloads (default: 4)")
    p.add_argument("--grid-cache", type=Path, default=DEFAULT_GRID_CACHE,
                   help="where to cache the HRRR grid coordinates")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _print_summary(results: list[FileResult]) -> None:
    """One line of totals, then one line per file that needs attention."""
    counts = Counter(r.status for r in results)
    rows = sum(r.rows for r in results)
    print(f"\n{len(results)} file(s): " + ", ".join(
        f"{counts[s]} {s.value}" for s in Status if counts[s]) + f"; {rows} row(s) written")
    for r in results:
        if r.status in (Status.FAILED, Status.MISSING):
            print(f"  {r.status.value:8} {r.file.key}: {r.error}")
        elif r.status is Status.PARTIAL:
            print(f"  partial  {r.file.key}: not in file: {', '.join(r.absent_variables)}")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the ingestion, print a summary; return the exit code."""
    args =build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        points = read_points(args.points_file)
        names = [v.strip() for v in args.variables.split(",") if v.strip()] if args.variables else None
        variables = resolve_variables(names)
    except (OSError, PointsFileError, UnknownVariableError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    transport = HttpTransport()
    try:
        run_date = args.run_date or latest_complete_run_date(transport)
        logging.info("Run date %s, %d point(s), %d variable(s), %d hour(s)",
                     run_date, len(points), len(variables), args.num_hours)

        with ForecastRepository(args.db) as repo:
            ingestor = Ingestor(repo, transport, grid_cache=args.grid_cache,
                                max_workers=args.workers)
            results = ingestor.run(run_date, points, variables, args.num_hours)
    except duckdb.IOException as exc:
        # Most often: another hrrr-ingest process has data.db open (single writer).
        print(f"error: cannot open {args.db}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (LookupError, RuntimeError, requests.RequestException) as exc:
        # No complete run found, grid could not be downloaded, network down.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_summary(results)
    bad = any(r.status in (Status.FAILED, Status.MISSING) for r in results)
    return EXIT_INCOMPLETE if bad else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

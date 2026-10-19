"""End-to-end: real GRIB2 bytes, real DuckDB, fake bucket."""

from datetime import date

import pytest

from hrrr_ingest.catalog import forecast_files
from hrrr_ingest.pipeline import Ingestor, Status
from hrrr_ingest.storage import ForecastRepository
from hrrr_ingest.variables import resolve_variables

from conftest import NI, build_file, field_values, grid_latlon

RUN_DATE = date(2025, 4, 24)
VARS = resolve_variables(["temperature_2m", "surface_pressure"])
OFFSETS = {"temperature_2m": 0.0, "surface_pressure": 100000.0}
# San-Francisco-style negative longitude vs 0..360 grid; second point lands
# exactly on grid cell (j=4, i=10) -> lat 32.0, lon -95.0.
POINTS = [(32.1, -94.9), (31.9, -95.1)]   # both snap to the same cell
EXPECTED_INDEX = 4 * NI + 10


def publish(transport, hours, specs=VARS):
    for f in forecast_files(RUN_DATE, 48)[: max(hours) + 1]:
        if f.forecast_hour in hours:
            blob, idx = build_file(f, specs, OFFSETS)
            transport.put(f.url, blob)
            transport.put(f.idx_url, idx)


@pytest.fixture
def repo(tmp_path):
    with ForecastRepository(tmp_path / "data.db") as r:
        yield r


def rows(repo):
    return repo.conn.execute(
        "SELECT valid_time_utc, run_time_utc, latitude, longitude, variable, value, source_s3 "
        "FROM hrrr_forecasts ORDER BY valid_time_utc, variable").fetchall()


def test_ingest_values_and_schema(fake_transport, repo, tmp_path):
    publish(fake_transport, {0, 1})
    ing = Ingestor(repo, fake_transport, grid_cache=tmp_path / "grid.npz")
    results = ing.run(RUN_DATE, POINTS, VARS, num_hours=2)

    assert [r.status for r in results] == [Status.INGESTED, Status.INGESTED]
    got = rows(repo)
    assert len(got) == 4                        # 2 hours x 2 vars x 1 unique cell
    lat, lon = grid_latlon(EXPECTED_INDEX)
    first = got[0]
    assert (first[2], first[3]) == (lat, lon)   # nearest *grid* point stored
    assert str(first[0]) == "2025-04-24 06:00:00" and str(first[1]) == "2025-04-24 06:00:00"
    assert first[4] == "surface_pressure"
    assert first[5] == pytest.approx(field_values(100000.0, 0)[EXPECTED_INDEX], rel=1e-6)
    assert first[6].endswith("hrrr.20250424/conus/hrrr.t06z.wrfsfcf00.grib2")
    assert got[-1][5] == pytest.approx(field_values(0.0, 1)[EXPECTED_INDEX], rel=1e-6)


def test_rerun_is_idempotent_and_downloads_nothing(fake_transport, repo, tmp_path):
    publish(fake_transport, {0, 1})
    ing = Ingestor(repo, fake_transport, grid_cache=tmp_path / "grid.npz")
    ing.run(RUN_DATE, POINTS, VARS, num_hours=2)
    before, ranges = rows(repo), fake_transport.range_calls()

    results = ing.run(RUN_DATE, POINTS, VARS, num_hours=2)
    assert all(r.status is Status.SKIPPED for r in results)
    assert rows(repo) == before
    assert fake_transport.range_calls() == ranges   # no new downloads


def test_new_variable_only_fetches_the_gap(fake_transport, repo, tmp_path):
    publish(fake_transport, {0})
    ing = Ingestor(repo, fake_transport, grid_cache=tmp_path / "grid.npz")
    ing.run(RUN_DATE, POINTS, VARS[:1], num_hours=1)
    ranges = fake_transport.range_calls()

    [r] = ing.run(RUN_DATE, POINTS, VARS, num_hours=1)
    assert r.status is Status.INGESTED and r.rows == 1
    assert fake_transport.range_calls() == ranges + 1


def test_missing_hour_is_reported_then_backfilled(fake_transport, repo, tmp_path):
    publish(fake_transport, {0})                    # f01 not published yet
    ing = Ingestor(repo, fake_transport, grid_cache=tmp_path / "grid.npz")
    results = ing.run(RUN_DATE, POINTS, VARS, num_hours=2)
    assert [r.status for r in results] == [Status.INGESTED, Status.MISSING]

    publish(fake_transport, {1})
    results = ing.run(RUN_DATE, POINTS, VARS, num_hours=2)
    assert [r.status for r in results] == [Status.SKIPPED, Status.INGESTED]
    assert len(rows(repo)) == 4


def test_variable_absent_from_file_is_partial(fake_transport, repo, tmp_path):
    publish(fake_transport, {0}, specs=VARS[:1])   # file lacks surface_pressure
    ing = Ingestor(repo, fake_transport, grid_cache=tmp_path / "grid.npz")
    [r] = ing.run(RUN_DATE, POINTS, VARS, num_hours=1)
    assert r.status is Status.PARTIAL
    assert r.absent_variables == ["surface_pressure"]


def test_corrupt_file_fails_alone(fake_transport, repo, tmp_path):
    publish(fake_transport, {0, 1})
    f1 = forecast_files(RUN_DATE, 2)[1]
    fake_transport.put(f1.url, b"GRIB" + b"\x00" * 50_000)   # garbage bytes
    ing = Ingestor(repo, fake_transport, grid_cache=tmp_path / "grid.npz")
    results = ing.run(RUN_DATE, POINTS, VARS, num_hours=2)
    assert [r.status for r in results] == [Status.INGESTED, Status.FAILED]
    assert len(rows(repo)) == 2      # f00 committed, nothing half-written for f01


def test_same_point_twice_never_duplicates(fake_transport, repo, tmp_path):
    """Same input point repeated in one file, and again across runs with a
    fresh grid (cache deleted) and a cached grid: always the same cell,
    never extra rows."""
    publish(fake_transport, {0})
    cache = tmp_path / "grid.npz"
    pts = [(32.1, -94.9), (32.1, -94.9), (32.1, 265.1)]   # dup + same point in 0..360 form

    [r] = Ingestor(repo, fake_transport, grid_cache=cache).run(RUN_DATE, pts, VARS, 1)
    assert r.rows == 2                       # 1 cell x 2 variables
    first = rows(repo)

    cache.unlink()                           # fresh grid from the GRIB file
    Ingestor(repo, fake_transport, grid_cache=cache).run(RUN_DATE, pts[:1], VARS, 1)
    Ingestor(repo, fake_transport, grid_cache=cache).run(RUN_DATE, pts[:1], VARS, 1)  # cached
    assert rows(repo) == first

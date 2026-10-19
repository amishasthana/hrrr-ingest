from datetime import date, datetime

import numpy as np
import pytest

from hrrr_ingest.catalog import ForecastFile, forecast_files, latest_complete_run_date
from hrrr_ingest.grid import GridLocator
from hrrr_ingest.idx import find_entry, parse_idx
from hrrr_ingest.points import PointsFileError, parse_points
from hrrr_ingest.storage import ForecastRepository, ForecastRow
from hrrr_ingest.variables import VARIABLES, UnknownVariableError, resolve_variables

IDX = """\
1:0:d=2025042406:REFC:entire atmosphere:anl:
2:100:d=2025042406:UGRD:80 m above ground:anl:
3:250:d=2025042406:TMP:2 m above ground:anl:
4:400:d=2025042406:DSWRF:surface:0-1 hour ave fcst:
5:500:d=2025042406:DSWRF:surface:anl:
6:650:d=2025042406:PRES:surface:anl:
"""


# --- idx -------------------------------------------------------------------
def test_parse_idx_byte_ranges():
    entries = parse_idx(IDX)
    assert [(e.start, e.end) for e in entries] == [
        (0, 99), (100, 249), (250, 399), (400, 499), (500, 649), (650, None)]


def test_find_entry_matches_param_and_level():
    entries = parse_idx(IDX)
    assert find_entry(entries, VARIABLES["temperature_2m"]).start == 250
    assert find_entry(entries, VARIABLES["u_component_wind_80m"]).start == 100
    assert find_entry(entries, VARIABLES["u_component_wind_10m"]) is None
    assert find_entry(entries, VARIABLES["surface_pressure"]).end is None


def test_find_entry_prefers_instantaneous():
    from hrrr_ingest.variables import VariableSpec
    spec = VariableSpec("dswrf", "DSWRF", "surface", "")
    assert find_entry(parse_idx(IDX), spec).start == 500


# --- catalog ---------------------------------------------------------------
def test_forecast_file_paths():
    f = ForecastFile(datetime(2025, 1, 1, 6), 7)
    assert f.source_s3 == ("s3://noaa-hrrr-bdp-pds.s3.amazonaws.com/"
                           "hrrr.20250101/conus/hrrr.t06z.wrfsfcf07.grib2")
    assert f.url.startswith("https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250101/")
    assert f.valid_time == datetime(2025, 1, 1, 13)


def test_forecast_files_hours():
    files = forecast_files(date(2025, 4, 24), 2)
    assert [f.forecast_hour for f in files] == [0, 1]
    with pytest.raises(ValueError):
        forecast_files(date(2025, 4, 24), 0)


def test_latest_complete_run_date(fake_transport):
    done = ForecastFile(datetime(2025, 4, 23, 6), 48)
    fake_transport.put(done.idx_url, "x")
    assert latest_complete_run_date(fake_transport, today=date(2025, 4, 24)) == date(2025, 4, 23)
    with pytest.raises(LookupError):
        latest_complete_run_date(fake_transport, today=date(2025, 6, 1), lookback_days=2)


# --- variables / points ----------------------------------------------------
def test_resolve_variables():
    assert len(resolve_variables(None)) == 11
    assert [v.name for v in resolve_variables(["temperature_2m", "temperature_2m"])] == ["temperature_2m"]
    with pytest.raises(UnknownVariableError):
        resolve_variables(["temp"])


def test_parse_points():
    assert parse_points("# c\n31.0,-88.0\n\n 32.5 , -86.25 \n") == [(31.0, -88.0), (32.5, -86.25)]
    for bad in ("31.0", "a,b", "95,-88", ""):
        with pytest.raises(PointsFileError):
            parse_points(bad)


# --- grid ------------------------------------------------------------------
def test_locator_handles_0_360_longitudes():
    # The original code fed 0..360 grid longitudes and -180..180 query
    # longitudes to the same KD-tree, so every match was wrong.
    lats, lons = np.meshgrid(np.arange(30, 41, 1.0), np.arange(230, 291, 1.0), indexing="ij")
    loc = GridLocator(lats, lons)
    [gp] = loc.nearest([(37.7749, -122.4194)])
    assert (gp.latitude, gp.longitude) == (38.0, -122.0)
    assert gp.distance_km < 60


def test_locator_uses_great_circle_distance():
    # At 60N a degree of longitude is half a degree of latitude in km;
    # naive (lat, lon) Euclidean distance would pick the wrong neighbour.
    loc = GridLocator(np.array([60.0, 60.6]), np.array([0.0, 1.0]))
    [gp] = loc.nearest([(60.0, 0.55)])
    assert gp.latitude == 60.0


def test_locator_cache_roundtrip(tmp_path):
    loc = GridLocator(np.array([[30.0, 31.0]]), np.array([[270.0, 271.0]]))
    loc.save(tmp_path / "g.npz")
    again = GridLocator.load(tmp_path / "g.npz")
    assert np.array_equal(again.longitudes, [-90.0, -89.0])


# --- storage ---------------------------------------------------------------
def test_storage_ignores_duplicates(tmp_path):
    row = ForecastRow(datetime(2025, 1, 1, 7), datetime(2025, 1, 1, 6), 31.5, -88.25,
                      "temperature_2m", 280.5, "s3://x")
    with ForecastRepository(tmp_path / "t.db") as repo:
        repo.insert([row])
        repo.insert([row, row])
        assert repo.count() == 1
        assert repo.existing_keys("s3://x", ["temperature_2m"]) == {("temperature_2m", 31.5, -88.25)}
        assert repo.existing_keys("s3://x", ["dewpoint_2m"]) == set()

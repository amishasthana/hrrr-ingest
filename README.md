# hrrr-ingest

A command-line tool that ingests NOAA **HRRR** (High-Resolution Rapid Refresh)
06z forecasts from the public AWS bucket for a list of lat/lon points, and
stores them in a local DuckDB database (`data.db`, table `hrrr_forecasts`) in
long format.

- Downloads only the variables you ask for, using byte-range requests
  (about 1–2 MB per variable instead of about 130 MB per file).
- **Idempotent:** a re-run stores nothing twice and downloads nothing it
  already has.
- **Fault tolerant:** each forecast hour succeeds or fails on its own; missing
  or failed hours are reported and picked up by the next run.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Usage](#2-usage)
3. [Testing it end to end](#3-testing-it-end-to-end)
4. [Output table](#4-output-table)
5. [Architecture](#5-architecture)
6. [Idempotency and fault tolerance](#6-idempotency-and-fault-tolerance)
7. [Trade-offs and design decisions](#7-trade-offs-and-design-decisions)
8. [Known limitations and failure modes](#8-known-limitations-and-failure-modes)
9. [Extending](#9-extending)
10. [Project layout](#10-project-layout)

---

## 1. Quick start

Requires Python 3.10 or newer.

```bash
unzip hrrr-ingest.zip && cd hrrr-ingest
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
pytest -q                     # 19 tests, no network needed

hrrr-ingest points.txt --run-date 2025-04-24 \
    --variables temperature_2m,surface_pressure --num-hours 2
```

The `eccodes` package from pip normally brings its own copy of the ecCodes C
library. If `import eccodes` fails (most often on Windows), install it with
`pip install eccodeslib` or `conda install -c conda-forge eccodes`.

## 2. Usage

```
hrrr-ingest POINTS_FILE [--run-date YYYY-MM-DD] [--variables a,b,...]
                        [--num-hours N] [--db PATH] [--workers N]
                        [--grid-cache PATH] [-v]
```

| Option | Default | Meaning |
|---|---|---|
| `POINTS_FILE` | required | One `lat,lon` per line. Blank lines and `#` comments are allowed. Longitude may be −180..180 or 0..360. |
| `--run-date` | Latest complete run | Forecast run date. "Complete" means the 06z f48 file has been published. |
| `--variables` | All 11 | Comma-separated list from the table below. |
| `--num-hours` | 48 | Ingests forecast hours **f00..f(N−1)**, with N from 1 to 48. |
| `--db` | `data.db` | DuckDB file. |
| `--workers` | 4 | Number of downloads that run in parallel. |
| `--grid-cache` | `~/.cache/hrrr-ingest/hrrr_conus_grid.npz` | Cached grid coordinates. |
| `-v` | off | Debug logging. |

### Supported variables

Values are stored as NOAA publishes them, with no unit conversion.

| Stored name | `.idx` match | Unit |
|---|---|---|
| `surface_pressure` | `PRES:surface` | Pa |
| `surface_roughness` | `SFCR:surface` | m |
| `visible_beam_downward_solar_flux` | `VBDSF:surface` | W/m² |
| `visible_diffuse_downward_solar_flux` | `VDDSF:surface` | W/m² |
| `temperature_2m` | `TMP:2 m above ground` | K |
| `dewpoint_2m` | `DPT:2 m above ground` | K |
| `relative_humidity_2m` | `RH:2 m above ground` | % |
| `u_component_wind_10m` | `UGRD:10 m above ground` | m/s |
| `v_component_wind_10m` | `VGRD:10 m above ground` | m/s |
| `u_component_wind_80m` | `UGRD:80 m above ground` | m/s |
| `v_component_wind_80m` | `VGRD:80 m above ground` | m/s |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every requested hour was ingested or already present. A `partial` hour (a variable not published in that file) still counts as 0. |
| 1 | Some hours were `missing` (not published yet) or `failed`. Run the command again later. |
| 2 | Bad input: unreadable points file or unknown variable name. |
| 3 | Could not run: database locked by another run, no complete run found, grid could not be downloaded, or network down. |

At the end of each run the tool prints a summary, for example
`2 file(s): 1 ingested, 1 missing; 6 row(s) written`, followed by one line for
each file that needs attention.

## 3. Testing it end to end

**Offline tests** (`pytest -q`). These build real GRIB2 files with synthetic
values in an in-memory fake bucket and write to a real DuckDB file. They cover:

- stored values and the schema, including 0–360 grid longitudes;
- a re-run makes **zero** downloads;
- adding a variable fetches only that variable;
- a missing hour is reported, then filled in on the next run;
- a variable absent from a file produces `partial`;
- a corrupt file fails on its own and leaves no half-written rows;
- the same point sent twice, or written in 0–360 form, never creates duplicates
  (with a fresh grid and with a cached one).

**Live smoke test.** The offline tests can't confirm what NOAA actually
publishes, so run this once against real data.

```bash
# 1. Confirm the .idx strings match a real file (expect 11 lines)
curl -s https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250424/conus/hrrr.t06z.wrfsfcf01.grib2.idx \
  | grep -E ':(PRES|SFCR|VBDSF|VDDSF):surface|:(TMP|DPT|RH):2 m above|:(UGRD|VGRD):(10|80) m above'

# 2. Small run: 3 points x 2 variables x 2 hours = 12 rows
head -3 points.txt > small.txt
hrrr-ingest small.txt --run-date 2025-04-24 \
    --variables temperature_2m,surface_pressure --num-hours 2 --db test.db -v

# 3. Inspect: temperature ~290-300 K, pressure ~100000 Pa, negative longitudes
python -c "import duckdb; duckdb.sql(\"SELECT * FROM 'test.db'.hrrr_forecasts ORDER BY 1, 5\").show()"

# 4. Idempotency: the same command again should say '2 skipped; 0 row(s) written'
hrrr-ingest small.txt --run-date 2025-04-24 \
    --variables temperature_2m,surface_pressure --num-hours 2 --db test.db
```

## 4. Output table

```sql
CREATE TABLE hrrr_forecasts (
    valid_time_utc TIMESTAMP NOT NULL,  -- run_time + forecast hour
    run_time_utc   TIMESTAMP NOT NULL,  -- always 06:00 on the run date
    latitude       FLOAT     NOT NULL,  -- of the nearest GRID CELL, not the input point
    longitude      FLOAT     NOT NULL,  -- -180..180
    variable       VARCHAR   NOT NULL,  -- stored name from the table above
    value          FLOAT,               -- NULL if the cell is masked in the GRIB file
    source_s3      VARCHAR   NOT NULL,  -- s3://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.YYYYMMDD/conus/hrrr.t06z.wrfsfcfHH.grib2
    PRIMARY KEY (run_time_utc, valid_time_utc, latitude, longitude, variable)
);
```

## 5. Architecture

```
              cli.py   parse args, validate, summary, exit code
                 |
            pipeline.py   Ingestor.run:  plan -> fetch -> decode -> store
       +-------+-+-------+---------+----------+
       |       |         |         |          |
  catalog.py  idx.py  decode.py  grid.py  storage.py
  paths,      byte    GRIB2 ->   nearest  DuckDB schema,
  run date    ranges  numpy      cell     dedupe, insert
       |       |
     transport.py  (HTTPS + retries; swappable Transport protocol)

  variables.py  registry: stored name -> .idx param + level
  points.py     reads the points file
```

What a run does:

1. **Grid.** Load the grid's coordinates from the cache, or download one
   message, build the grid from it, and cache it. Map each input point to its
   nearest grid cell; points that land in the same cell are merged.
2. **Plan.** For each forecast hour, ask DuckDB which (variable, cell)
   combinations are already stored, and keep only the variables with gaps.
   Hours with no gaps are `skipped` without any network call.
3. **Fetch** (in a thread pool). Download the `.idx` file, find each variable's
   byte range, and make one HTTP range request per variable.
4. **Decode and store** (on the main thread, as each download finishes).
   Decode each message with ecCodes, pick the target cells, and insert all of
   that hour's rows in one transaction.
5. **Report** each hour as `ingested`, `skipped`, `partial`, `missing` or
   `failed`, then print the summary and set the exit code.

Why it's built this way:

- **The `.idx` file instead of a GRIB library's variable names.** cfgrib,
  pygrib and ecCodes name variables differently (`t2m` / `2t` / `TMP`). The
  `.idx` strings are NOAA's own and are also what we need for byte ranges.
- **Decoding single messages from bytes.** No temp files, no cfgrib index
  files, and no cfgrib errors from mixing fields with different time steps.
- **Nearest neighbour on 3-D unit vectors.** HRRR uses a Lambert Conformal
  grid with 0–360 longitudes. Searching on raw degrees picks the wrong cell
  (a degree of longitude is shorter than a degree of latitude, and −122
  never matches 238). Unit vectors give the true nearest cell by
  great-circle distance.
- **Downloads in parallel, one database writer.** The network is the
  bottleneck, and DuckDB allows only one writer per file.

## 6. Idempotency and fault tolerance

**Idempotency**

| Layer | How |
|---|---|
| Planner | Skips every (hour, variable, cell) combination already in the table, so nothing is downloaded twice. |
| Database | The primary key plus `INSERT OR IGNORE` means a duplicate row can't be stored even if the planner were bypassed. |
| Points | The nearest-cell lookup is deterministic, so the same point always maps to the same cell. Duplicate input points are merged. |

**Fault tolerance**

| Situation | Behaviour |
|---|---|
| Hour not published yet (404) | `missing`, exit 1. The next run fetches it. |
| Throttling (429), server errors (5xx), dropped connections | Retried with exponential backoff, up to 5 times, then `failed`. |
| Variable not in the file (e.g. solar flux at f00) | `partial`; the other variables are stored. |
| Corrupt or short download | `failed` for that hour only; nothing from it is written. |
| Crash or Ctrl-C mid-run | Finished hours are committed, unfinished ones are not. Re-running resumes cleanly. |
| Two runs on the same `data.db` | The second exits with code 3 and a clear message. |
| Point outside the HRRR domain | Warning in the log; the nearest edge cell is used. |
| Cached grid doesn't match the file's size | That hour fails with a message to delete the grid cache. |

## 7. Trade-offs and design decisions

| Decision | Why | Cost / risk |
|---|---|---|
| Byte-range downloads via `.idx` | ~1–2 MB per variable instead of ~130 MB per file | Relies on the `.idx` file being correct; there's no fallback that downloads the whole file. |
| "Already done" is read from the data table, with no separate status table | One source of truth that can't drift out of sync | Can't tell "never tried" from "tried and missing", so missing hours are re-checked each run (one small `.idx` request). |
| `INSERT OR IGNORE` | A re-run can never duplicate rows | Values are never updated, so if NOAA republished a corrected file, stored values wouldn't change. Use `INSERT OR REPLACE` if corrections matter. |
| All of an hour's variables in one transaction | No half-written hours, simple to resume | One variable that fails to decode blocks that hour's other variables until the next run. |
| Grid cell's lat/lon as part of the key | Required by the spec; duplicate points collapse automatically | You can't trace a row back to its input point. Lat/lon are float32, so ad-hoc SQL filters need an exact match or `ROUND()`. |
| `--num-hours N` = f00..f(N−1) | "N hours of data" starting at the analysis hour | The default of 48 skips f48. To include it, change `catalog.forecast_hours` so N gives f00..fN. |
| Default date requires f48 to be published | Guarantees a complete run | A run that's complete enough for a short `--num-hours` may be skipped in favour of the previous day. |
| Prefer the instantaneous value when a variable appears more than once | The usual meaning of a forecast value | A judgement call the spec doesn't settle. Add a forecast-type match to `VariableSpec` if you need averages. |
| Values stored in NOAA's units | Faithful to the source | There's no units column; consumers need the units table above. |
| Grid cache in `~/.cache`, checked only by size | Avoids re-downloading the grid on every run | A grid change that keeps the same size would map points to different cells. Delete the cache after an HRRR grid upgrade. |
| 403 treated as "not found" | S3 returns 403 for missing keys in some setups | A proxy or firewall blocking the bucket shows as `missing`, not as an access error. |
| `partial` hours exit 0 | Solar flux at f00 is routinely absent; treating that as failure would be noise | A genuinely mis-named variable only shows in the summary, not in the exit code. |
| Points outside the domain only warn | Doesn't block a batch over one bad point | The edge cell's data is stored for a point it doesn't represent. |

## 8. Known limitations and failure modes

- **Older dates.** The 06z run has only gone out to 48 hours since HRRR v4
  (December 2020). For older dates, the later hours will always be `missing`
  (exit code 1).
- **Scale.** Inserts go row by row through `executemany`. That's fine for
  hundreds of points; for thousands, bulk-load through Arrow or pandas. The
  primary-key index also gets heavy at hundreds of millions of rows.
- **Scope.** One run date and the 06z run only. The spec also mentions a date
  range; the pipeline already works on any list of files (see section 9).
- **No retry inside a run** for corrupt or short downloads. They're marked
  `failed`, and the next run retries them.
- **Not yet checked against live data in development.** The tests use
  synthetic GRIB2 files. Run the live smoke test in section 3 once to confirm
  the `.idx` strings, especially for the solar-flux and roughness variables.

## 9. Extending

| To add… | Change |
|---|---|
| A variable | One `VariableSpec(...)` line in `variables.py`. |
| A date range | Add `--start-date/--end-date` in `cli.py`, then concatenate `forecast_files(d, n)` for each date. The planner and pipeline already work on a list of files. |
| Another run hour, product (`wrfprs`) or model | `catalog.py` holds all path and run-hour logic. Turn `ForecastFile.key` into a template if you need several at once. |
| A different data source (local mirror, boto3, cache) | Implement the 3-method `Transport` protocol in `transport.py`. |
| A traceable point → cell mapping | Add a `point_locations` table in `storage.py` and record each `GridPoint` the first time a point is seen. |
| Faster inserts | Replace `executemany` in `ForecastRepository.insert` with an Arrow or pandas bulk load followed by `INSERT OR IGNORE ... SELECT`. |
| Scheduled runs | No code change. The tool is idempotent, so run it daily with defaults and treat exit code 1 as "try again later". |

## 10. Project layout

```
hrrr-ingest/
├── README.md
├── pyproject.toml          package metadata, dependencies, `hrrr-ingest` entry point
├── points.txt              example input (from the spec)
├── src/hrrr_ingest/
│   ├── cli.py              argument parsing, summary, exit codes
│   ├── pipeline.py         Ingestor: plan -> fetch -> decode -> store
│   ├── variables.py        variable registry
│   ├── catalog.py          bucket layout, paths, run hour, default run date
│   ├── transport.py        HTTPS with retries; Transport protocol
│   ├── idx.py              .idx parsing -> byte ranges
│   ├── decode.py           single-message GRIB2 decoding (ecCodes)
│   ├── grid.py             nearest grid cell, grid cache
│   ├── storage.py          DuckDB schema and writes
│   └── points.py           points-file parsing
└── tests/
    ├── conftest.py         synthetic GRIB2 builder + fake bucket
    ├── test_units.py       idx, catalog, grid, points, storage
    └── test_pipeline.py    end-to-end against the fake bucket
```

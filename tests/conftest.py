"""Test helpers: synthetic GRIB2 files and an in-memory fake bucket."""

from __future__ import annotations

import eccodes
import numpy as np
import pytest

from hrrr_ingest.catalog import ForecastFile
from hrrr_ingest.transport import NotFoundError
from hrrr_ingest.variables import VariableSpec

# Small regular lat/lon grid over the south-east US, longitudes stored 0..360
# the way GRIB stores them (260 == -100).
LAT0, LON0, NJ, NI, STEP = 30.0, 260.0, 21, 31, 0.5


def make_grib_message(values: np.ndarray) -> bytes:
    h = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        for key, val in {
            "Ni": NI, "Nj": NJ,
            "latitudeOfFirstGridPointInDegrees": LAT0,
            "longitudeOfFirstGridPointInDegrees": LON0,
            "latitudeOfLastGridPointInDegrees": LAT0 + (NJ - 1) * STEP,
            "longitudeOfLastGridPointInDegrees": LON0 + (NI - 1) * STEP,
            "iDirectionIncrementInDegrees": STEP,
            "jDirectionIncrementInDegrees": STEP,
            "jScansPositively": 1,
        }.items():
            eccodes.codes_set(h, key, val)
        eccodes.codes_set_values(h, values.ravel().astype(float))
        return eccodes.codes_get_message(h)
    finally:
        eccodes.codes_release(h)


def field_values(var_offset: float, forecast_hour: int) -> np.ndarray:
    """Distinct, predictable values: base + 100*hour + flat index."""
    return var_offset + 100.0 * forecast_hour + np.arange(NJ * NI, dtype=float)


def grid_latlon(flat_index: int) -> tuple[float, float]:
    j, i = divmod(flat_index, NI)
    return LAT0 + j * STEP, LON0 + i * STEP - 360.0


def build_file(f: ForecastFile, specs: list[VariableSpec], offsets: dict[str, float]):
    """Return (grib_bytes, idx_text) for a file containing ``specs``."""
    blob, lines, pos = b"", [], 0
    # A decoy message first, so byte offsets are non-trivial.
    decoy = make_grib_message(np.zeros(NJ * NI))
    lines.append(f"1:0:d={f.run_time:%Y%m%d%H}:REFC:entire atmosphere:anl:")
    blob += decoy
    for n, spec in enumerate(specs, start=2):
        msg = make_grib_message(field_values(offsets[spec.name], f.forecast_hour))
        lines.append(f"{n}:{len(blob)}:d={f.run_time:%Y%m%d%H}:"
                     f"{spec.idx_param}:{spec.idx_level}:{f.forecast_hour} hour fcst:")
        blob += msg
    return blob, "\n".join(lines) + "\n"


class FakeTransport:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def put(self, url: str, data: bytes | str) -> None:
        self.objects[url] = data.encode() if isinstance(data, str) else data

    def exists(self, url: str) -> bool:
        self.calls.append(("exists", url))
        return url in self.objects

    def get_text(self, url: str) -> str:
        self.calls.append(("text", url))
        if url not in self.objects:
            raise NotFoundError(url)
        return self.objects[url].decode()

    def get_range(self, url: str, start: int, end: int | None) -> bytes:
        self.calls.append(("range", url))
        if url not in self.objects:
            raise NotFoundError(url)
        data = self.objects[url]
        return data[start: None if end is None else end + 1]

    def range_calls(self) -> int:
        return sum(1 for kind, _ in self.calls if kind == "range")


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()

"""Decode single GRIB2 messages with ecCodes.

We decode one message at a time from bytes, so there are no temp files and
no cfgrib index files, and no "which hypercube does this variable land in"
problem: we already know which variable a message is from the .idx lookup.
"""

from __future__ import annotations

from dataclasses import dataclass

import eccodes
import numpy as np


@dataclass
class DecodedField:
    """A decoded field, flattened in the file's scan order."""

    values: np.ndarray                    # 1-D, in the file's grid order
    latitudes: np.ndarray | None = None   # same shape, only if requested
    longitudes: np.ndarray | None = None


def decode_message(data: bytes, with_grid: bool = False) -> DecodedField:
    """Decode one GRIB2 message.

    Args:
        data: the raw bytes of exactly one message (from a range request).
        with_grid: also return per-cell latitudes/longitudes. Only needed
            once, to build the grid; it roughly doubles decode time.

    Returns values as float64 with bitmap-masked cells set to NaN.
    Raises an ecCodes error if the bytes aren't a valid GRIB message.
    """
    handle =eccodes.codes_new_from_message(data)
    try:
        values = np.asarray(eccodes.codes_get_values(handle), dtype=np.float64)
        if eccodes.codes_get(handle, "bitmapPresent"):
            missing = eccodes.codes_get(handle, "missingValue")
            values[values == missing] = np.nan
        field = DecodedField(values=values)
        if with_grid:
            field.latitudes = np.asarray(eccodes.codes_get_array(handle, "latitudes"))
            field.longitudes = np.asarray(eccodes.codes_get_array(handle, "longitudes"))
        return field
    finally:
        eccodes.codes_release(handle)

"""Parse GRIB2 ``.idx`` sidecar files and locate messages by byte range.

A line looks like::

    71:47323947:d=2025042406:TMP:2 m above ground:anl:

i.e. message number, byte offset, reference date, parameter, level,
forecast descriptor. A message ends where the next one (with a larger
offset) begins; the last message runs to end of file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .variables import VariableSpec

log = logging.getLogger(__name__)

# Statistical (time-processed) fields; we want instantaneous values when a
# variable appears more than once.
_STATISTICAL_MARKERS = ("ave", "acc", "max", "min")


@dataclass(frozen=True)
class IdxEntry:
    """One GRIB2 message as listed in the .idx file."""

    start: int
    end: int | None   # inclusive; None = to end of file
    param: str
    level: str
    forecast: str


def parse_idx(text: str) -> list[IdxEntry]:
    """Parse the text of a ``.idx`` file into entries with byte ranges.

    Each message's end offset is derived from the next message's start
    offset (the .idx file only lists starts). Offsets are de-duplicated
    because some products list sub-messages that share one offset.

    Raises:
        ValueError: if a non-empty line doesn't have the expected fields.
    """
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 6:
            raise ValueError(f"Malformed .idx line: {line!r}")
        rows.append((int(parts[1]), parts[3], parts[4], parts[5]))

    offsets = sorted({r[0] for r in rows})
    next_offset = {a: b for a, b in zip(offsets, offsets[1:])}
    return [
        IdxEntry(
            start=start,
            end=(next_offset[start] - 1) if start in next_offset else None,
            param=param,
            level=level,
            forecast=forecast,
        )
        for start, param, level, forecast in rows
    ]


def find_entry(entries: list[IdxEntry], spec: VariableSpec) -> IdxEntry | None:
    """Return the message for ``spec``, or ``None`` if the file lacks it.

    Matching is on exact parameter and level strings. If several messages
    match (e.g. an instantaneous value and a 0-1 hour average), the
    instantaneous one is preferred.

    Trade-off: this is a heuristic, not something the spec defines. If you
    need the averaged/accumulated variants, add a ``forecast`` matcher to
    ``VariableSpec`` instead of relying on this preference.
    """
    matches =[e for e in entries if e.param == spec.idx_param and e.level == spec.idx_level]
    if not matches:
        return None
    if len(matches) > 1:
        instantaneous = [
            e for e in matches
            if not any(m in e.forecast for m in _STATISTICAL_MARKERS)
        ]
        chosen = (instantaneous or matches)[0]
        log.debug("%s matched %d messages; using %r", spec.name, len(matches), chosen.forecast)
        return chosen
    return matches[0]

"""Registry of supported HRRR variables.

Each entry maps the human-readable name we store in DuckDB to the way the
variable is written in the GRIB2 ``.idx`` sidecar file (parameter abbreviation
plus level string). Using the .idx strings instead of a GRIB library's
shortName avoids the naming inconsistencies between cfgrib / pygrib / ecCodes,
and lets us download only the bytes we need.

To support a new variable, add one ``VariableSpec`` to ``_SPECS``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VariableSpec:
    """How to find one variable in an HRRR wrfsfc file.

    Values are stored as published (no unit conversion): temperatures and
    dew point in K, pressure in Pa, wind in m/s, RH in %, flux in W/m^2,
    roughness in m.
    """

    name: str         # name stored in hrrr_forecasts.variable
    idx_param: str     # parameter abbreviation in the .idx file, e.g. "TMP"
    idx_level: str     # level string in the .idx file, e.g. "2 m above ground"
    description: str


_SPECS = [
    VariableSpec("surface_pressure", "PRES", "surface", "Surface pressure"),
    VariableSpec("surface_roughness", "SFCR", "surface", "Surface roughness"),
    VariableSpec("visible_beam_downward_solar_flux", "VBDSF", "surface",
                 "Visible beam downward solar flux"),
    VariableSpec("visible_diffuse_downward_solar_flux", "VDDSF", "surface",
                 "Visible diffuse downward solar flux"),
    VariableSpec("temperature_2m", "TMP", "2 m above ground",
                 "Temperature at 2m above ground"),
    VariableSpec("dewpoint_2m", "DPT", "2 m above ground",
                 "Dew point temperature at 2m above ground"),
    VariableSpec("relative_humidity_2m", "RH", "2 m above ground",
                 "Relative humidity at 2m above ground"),
    VariableSpec("u_component_wind_10m", "UGRD", "10 m above ground",
                 "U-component of wind at 10m above ground"),
    VariableSpec("v_component_wind_10m", "VGRD", "10 m above ground",
                 "V-component of wind at 10m above ground"),
    VariableSpec("u_component_wind_80m", "UGRD", "80 m above ground",
                 "U-component of wind at 80m above ground"),
    VariableSpec("v_component_wind_80m", "VGRD", "80 m above ground",
                 "V-component of wind at 80m above ground"),
]

VARIABLES: dict[str, VariableSpec] = {spec.name: spec for spec in _SPECS}


class UnknownVariableError(ValueError):
    pass


def resolve_variables(names: list[str] | None) -> list[VariableSpec]:
    """Turn user-supplied names into specs. ``None`` / empty means all.

    Raises ``UnknownVariableError`` listing the bad names and the valid ones.
    """
    if not names:
        return list(_SPECS)
    unknown = [n for n in names if n not in VARIABLES]
    if unknown:
        raise UnknownVariableError(
            f"Unknown variable(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(VARIABLES)}"
        )
    # Preserve order, drop duplicates.
    return [VARIABLES[n] for n in dict.fromkeys(names)]

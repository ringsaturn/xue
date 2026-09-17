"""Geostationary satellite imagery as a ``series_file`` observation source.

A satellite source is the JMA nowcast's and the CMA mosaic's shape: the
fetch stage produces one NetCDF series per window on a regular grid and
the converter downstream is untouched. What the fetch stage does here is
list a scan's tiles on the agency's public bucket, warp them from the
geostationary projection onto plate carrée, cache the result per frame,
and stack the window's frames into the series. Four seams are fixed for
what comes after the first satellite and channel:

- :mod:`platforms` — the registry: one row per spacecraft at an orbital
  slot, its instrument's channels, its files' bucket and reader;
- :mod:`readers` — how a product family's files are listed, fetched and
  opened as one GDAL dataset (the ISatSS tiles, the GOES CMIPF file, the
  MTG FCI chunks through :mod:`eumetsat`, the Data Store client);
- :mod:`projector` — how that dataset lands on the regular grid
  (``gdalwarp`` first);
- :mod:`producers` — how a composite is derived from a slot's channels
  (the Dust RGB through shachen).

:mod:`fetch` runs one window through them; :mod:`assemble` names the
frames and writes the series.
"""

from .platforms import (
    GOES_EAST,
    GOES_WEST,
    HIMAWARI,
    METEOSAT,
    METEOSAT_IODC,
    PLATFORMS,
    Channel,
    Platform,
    SatelliteBand,
    platform,
)

__all__ = [
    "GOES_EAST",
    "GOES_WEST",
    "HIMAWARI",
    "METEOSAT",
    "METEOSAT_IODC",
    "PLATFORMS",
    "Channel",
    "Platform",
    "SatelliteBand",
    "platform",
]

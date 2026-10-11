"""A WRF-ARW (Recast WOOF) run as CF NetCDF series files.

``xue wrf-series`` reads a run's hourly ``wrfout`` files and writes one
series file per Xue variable in the shape ``xuebuild.observation`` reads
for a series-file forecast (the ``ifshres`` layout: ``<stem>.<id>.nc``,
``time`` counted from the cycle, the registry's output unit). Everything
WRF-specific — the staggered grids, the map rotation, the Lambert
conformal placement, the per-level cloud fraction, the accumulated rain
— is resolved here, so the encoders see a regular latitude/longitude
series and nothing else.

netCDF4 is the ``wrf`` dependency group (``uv sync --group wrf``) and is
imported where it is used; the rest of the pipeline never needs it.
"""

from __future__ import annotations

from .reader import WrfError
from .series import VARIABLE_IDS, convert_run

__all__ = ["VARIABLE_IDS", "WrfError", "convert_run"]

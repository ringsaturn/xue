"""ERA5 hourly single levels as CF NetCDF series files.

``xue era5-series`` reads what the Copernicus Climate Data Store hands back
for a ``reanalysis-era5-single-levels`` request in NetCDF — one file, or the
zip of one file per step type it sends when a request mixes instantaneous
fields with the hourly gust maximum — and writes one series file per Xue
variable in the shape :mod:`xuebuild.observation` reads for a series-file
forecast (the ``woof`` layout: ``era5.<run>.<id>.nc``, ``time`` counted in
hours from the run, ascending latitudes). A reanalysis has no cycle: the run
is the window's first hour, named by ``--start``. With no input file the
tool asks the Data Store itself (``cdsapi``, the key in ``~/.cdsapirc``).

netCDF4 and cdsapi are the ``era5`` dependency group (``uv sync --group era5
--inexact``) and are imported where they are used.
"""

from __future__ import annotations

from .series import VARIABLE_IDS, Era5Error, convert, request

__all__ = ["VARIABLE_IDS", "Era5Error", "convert", "request"]

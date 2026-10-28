"""CAMS global near-real-time aerosol analyses as CF NetCDF series files.

``xue cams-series`` reads what the Copernicus Atmosphere Data Store hands
back for a ``cams-global-atmospheric-composition-forecasts`` request in
NetCDF — every 00Z and 12Z cycle's analysis and its first three 3-hourly
steps, so the frames run back to back every three hours — and writes one
series file per Xue variable in the shape :mod:`xuebuild.observation` reads
for a series-file forecast (the ``woof`` layout: ``cams.<run>.<id>.nc``,
``time`` counted in hours from the run, ascending latitudes). The run is the
window's first hour, named by ``--start``. With no input file the tool asks
the Data Store itself (``cdsapi``, the key in ``~/.cdsapirc``).

netCDF4 and cdsapi are the ``era5`` dependency group (``uv sync --group era5
--inexact``) and are imported where they are used.
"""

from __future__ import annotations

from .series import VARIABLE_IDS, CamsError, convert, request

__all__ = ["VARIABLE_IDS", "CamsError", "convert", "request"]

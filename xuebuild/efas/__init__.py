"""EFAS v5 river discharge as CF NetCDF series files.

``xue efas-series`` reads one EFAS GRIB2 file from the Early Warning Data
Store — a HRES forecast cycle or a stretch of the water balance, ``dis06``
on the 1/60° EFAS grid, product template 4.72, which GDAL reads without
its metadata — together with EFAS v5.1's four flood threshold maps and the
static upstream area map, and writes one series file per Xue variable in
the shape :mod:`xuebuild.observation` reads for a series-file forecast
(the ``woof`` layout: ``<model>.<run>.<id>.nc``, ``time`` counted in hours
from the run, ascending latitudes, a fill of -9999 the registry declares in
``fill_values``). The return-period class is
derived here, so the encoders see three plain series and nothing EFAS
knows.

eccodes and netCDF4 are the ``efas`` dependency group (``uv sync --group
efas --inexact``) and are imported where they are used; the rest of the
pipeline never needs them.
"""

from __future__ import annotations

from .series import (
    PRODUCER_VERSION,
    RETURN_PERIODS,
    VARIABLE_IDS,
    EfasError,
    convert,
    return_period_class,
)

__all__ = ["PRODUCER_VERSION", "RETURN_PERIODS", "VARIABLE_IDS", "EfasError", "convert", "return_period_class"]

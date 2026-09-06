"""Experimental native encoder for the Xue v1 bundle format.

The wheel carries its own minimal GDAL — GRIB and netCDF drivers only — with
GDAL's and PROJ's data directories alongside it. Both libraries look those up
through environment variables at first use, so they are pointed at the bundled
copies here, before the extension module is imported and GDAL registers.

`setdefault` rather than assignment: a caller who has already pointed these at
a system GDAL knows something this package does not.
"""

from __future__ import annotations

import os as _os
import pathlib as _pathlib

_PACKAGE = _pathlib.Path(__file__).resolve().parent

for _variable, _directory in (("GDAL_DATA", "gdal-data"), ("PROJ_DATA", "proj-data")):
    _path = _PACKAGE / _directory
    if _path.is_dir():
        _os.environ.setdefault(_variable, str(_path))
# PROJ 8 and earlier read PROJ_LIB; harmless to set for later versions too.
if "PROJ_DATA" in _os.environ:
    _os.environ.setdefault("PROJ_LIB", _os.environ["PROJ_DATA"])

from .xue_encode_py import (  # noqa: E402  (must follow the data-path setup)
    convert_bin,
    decimate,
    encode_poster,
    encode_residual,
    quantize,
)

__all__ = [
    "convert_bin",
    "decimate",
    "encode_poster",
    "encode_residual",
    "quantize",
]

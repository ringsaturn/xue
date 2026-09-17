"""Projectors: from a geostationary dataset to the regular grid a bundle
carries.

The projection is implemented once, here, on the fetch side: the two
encoders downstream read a NetCDF series on a plate carrée grid and never
learn the geostationary arithmetic (the sweep angle, the ellipsoid, where
the disk ends), the way the JMA nowcast's tiles are decoded by the fetch
and the CMA mosaic is read out of an archive. The first implementation is
``gdalwarp``; a NumPy projector with a cached lookup table (for parallax
correction, or for alignment with a producer's grid) is the same interface
with another class, and ``assemble.py`` would not change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import ConversionError
from ..gdal import require_command, run_command


@dataclass(frozen=True)
class TargetGrid:
    """A regular latitude/longitude grid by its edges and step: cell
    centres sit half a step inside the edges, so a bundle built on it has
    ``firstLongitude = west + step / 2``. Longitudes may run past 180."""

    west: float
    south: float
    east: float
    north: float
    step: float

    def __post_init__(self) -> None:
        if not (self.step > 0 and self.east > self.west and self.north > self.south):
            raise ConversionError(f"target grid is degenerate: {self}")
        for name, span in (("longitude", self.east - self.west), ("latitude", self.north - self.south)):
            cells = span / self.step
            if abs(cells - round(cells)) > 1e-6:
                raise ConversionError(f"target grid {name} span {span} is not a whole number of {self.step}° cells")

    @property
    def width(self) -> int:
        return round((self.east - self.west) / self.step)

    @property
    def height(self) -> int:
        return round((self.north - self.south) / self.step)

    @property
    def first_longitude(self) -> float:
        return round(self.west + self.step / 2, 10)

    @property
    def first_latitude(self) -> float:
        return round(self.north - self.step / 2, 10)

    def extent_arguments(self) -> list[str]:
        return ["-te", repr(self.west), repr(self.south), repr(self.east), repr(self.north), "-tr", repr(self.step), repr(self.step)]


class Projector(Protocol):
    def to_grid(self, source: Path, grid: TargetGrid, *, nodata: int, resampling: str, out: Path) -> None:
        """Write ``source`` resampled onto ``grid`` as a GeoTIFF at ``out``,
        with ``nodata`` marking the cells the source never covered and the
        source's scale and offset carried on the band."""
        ...


class GdalWarpProjector:
    """``gdalwarp -t_srs EPSG:4326``: GDAL's own geostationary projection
    (PROJ's ``geos``), deterministic for one GDAL version, multithreaded.
    The frame is an Int16 GeoTIFF, DEFLATE with the horizontal predictor,
    tiled, so the frame cache reads back fast and the mirror on the bucket
    stays small."""

    def to_grid(self, source: Path, grid: TargetGrid, *, nodata: int, resampling: str, out: Path) -> None:
        if resampling not in ("near", "bilinear", "cubic", "average"):
            raise ConversionError(f"unsupported resampling {resampling!r}")
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_suffix(out.suffix + ".part.tif")
        run_command(
            [
                "gdalwarp",
                "-q",
                "-overwrite",
                "-t_srs",
                "EPSG:4326",
                *grid.extent_arguments(),
                "-r",
                resampling,
                "-srcnodata",
                str(nodata),
                "-dstnodata",
                str(nodata),
                "-ot",
                "Int16",
                "-multi",
                "-wo",
                "NUM_THREADS=ALL_CPUS",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "PREDICTOR=2",
                "-co",
                "TILED=YES",
                str(source),
                str(temporary),
            ],
            description=f"gdalwarp {out.name}",
        )
        # A warp that lands nothing on the grid is a wrong projection (a GDAL
        # whose netCDF driver read the source's geostationary axes as
        # something else), not a frame: refuse it rather than cache and
        # publish a blank.
        # Exact statistics (a sample misses a disk edge on the big grid),
        # with the sidecar GDAL would otherwise leave beside the frame off.
        result = run_command(
            [require_command("gdalinfo"), "--config", "GDAL_PAM_ENABLED", "NO", "-json", "-stats", str(temporary)],
            description=f"inspect {out.name}",
        )
        try:
            band = json.loads(result.stdout)["bands"][0]
            valid = float(band.get("metadata", {}).get("", {}).get("STATISTICS_VALID_PERCENT", 0.0))
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConversionError(f"cannot read the statistics of {temporary}: {exc}") from exc
        if not valid > 0.0:
            raise ConversionError(f"gdalwarp put no data on the grid for {source}: the source's projection was not read")
        temporary.replace(out)


PROJECTORS: dict[str, Projector] = {"gdalwarp": GdalWarpProjector()}


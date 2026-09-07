from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np


@dataclass(frozen=True, order=True)
class GfsRun:
    time: datetime

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValueError("GFS run time must include a timezone")
        normalized = self.time.astimezone(UTC)
        if normalized.minute or normalized.second or normalized.microsecond:
            raise ValueError("GFS run time must be aligned to an hour")
        if normalized.hour not in (0, 6, 12, 18):
            raise ValueError("GFS run hour must be 00, 06, 12, or 18 UTC")
        object.__setattr__(self, "time", normalized)

    @property
    def id(self) -> str:
        return self.time.strftime("%Y%m%d%H")

    @property
    def date(self) -> str:
        return self.time.strftime("%Y%m%d")

    @property
    def cycle(self) -> str:
        return self.time.strftime("%H")

    def valid_time(self, forecast_hour: int) -> datetime:
        return self.time + timedelta(hours=forecast_hour)


@dataclass(frozen=True)
class SourceFrame:
    """One variable at one time, located in a file GDAL can open: a GRIB2
    record, or a band of a NetCDF observation series."""

    path: Path
    band: int
    variable_id: str
    run_time: datetime
    valid_time: datetime
    lead_seconds: int
    """Seconds from ``run_time``. A GRIB record's lead time is always a whole
    hour; an observation series can be finer (the radar mosaic is six
    minutes), and the encoder derives the bundle's axis unit from these."""
    unit: str


@dataclass(frozen=True)
class PlaneSource:
    """How gdal_translate must read one file's bands.

    GRIB records arrive already in physical units with every point valid, so
    the defaults do nothing. A packed NetCDF observation file needs its
    ``scale_factor``/``add_offset`` applied (``-unscale``) and its fill value
    turned into a real number the codebook can quantize — the format carries
    no bitmap, so missing data has to become a value."""

    unscale: bool = False
    fill_values: tuple[float, ...] = ()
    """Values marking missing data in the extracted plane. GDAL passes a
    band's nodata value through ``-unscale`` untouched, so the fill can arrive
    either raw or scaled; both are listed, and neither is a value the variable
    can physically take."""
    fill_replacement: float = 0.0
    """What missing data becomes; the bottom of the variable's codebook."""

    def apply_fill(self, values: np.ndarray) -> np.ndarray:
        if not self.fill_values:
            return values
        # Scaling is float arithmetic, so match with a relative tolerance
        # rather than for equality.
        missing = np.zeros(values.shape, dtype=bool)
        for fill in self.fill_values:
            missing |= np.isclose(values, fill, rtol=1e-6, atol=1e-6)
        return np.where(missing, self.fill_replacement, values)


GRIB_PLANE_SOURCE = PlaneSource()

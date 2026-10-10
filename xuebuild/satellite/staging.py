"""Staging the CAMEL emissivity months the DEBRA confidence reads.

:mod:`.ancillary` reads the ground's infrared emissivity from
``<ancillary root>/camel/<region>/<YYYYMM>.nc``: one NetCDF per region
and month, the CAMEL climatology (CAM5K30EM V003) already interpolated
to the DEBRA band centres, cropped to the region's box with a degree of
margin, and named for the month it serves. This module writes those
files. ``stage-ancillary.yml`` runs it before each month's first slot
and ``make push-r2-ancillary`` puts the result on the bucket, where
``make pull-r2-ancillary`` mirrors it into every satellite round.

A month is staged from the granule :func:`shachen.io.emissivity.
fetch_emissivity` serves for it, behind Earthdata credentials in
``~/.netrc``. The record ends in 2023-12, so a current month is served
by the same calendar month of the newest year that has one — a
climatological substitution the algorithm already makes on purpose,
never a different month — and the granule's own month is written as
the file's ``source_month``. The crop is on coordinates sorted
ascending first: CAMEL stores latitude north to south, and slicing a
descending coordinate selects nothing, silently.

The regions are a table here rather than a derivation from the disks:
staging a region is what extends the confidence over its land
(:func:`.ancillary.emissivity_on_grid`), so adding one is a decision
about the product.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..errors import ConversionError, DownloadError, XueError

if TYPE_CHECKING:
    import xarray as xr

LOG = logging.getLogger(__name__)

#: Degrees of margin around a region's box, so the bilinear regrid onto a
#: disk grid never extrapolates at the box's edge.
MARGIN_DEG = 1.0
#: What the staged file says it was cut from.
SOURCE = "CAMEL CAM5K30EM V003 monthly emissivity"
#: Where a region's granules are cached under the ancillary root; the
#: regions of a month share one.
GRANULES_DIR = "camel-granules"
#: The month token in a CAMEL granule's name, canonical
#: (``CAM5K30EM_202309.nc``) or native (``CAM5K30EM_emis_202309_V003.nc``).
_GRANULE_MONTH = re.compile(r"CAM5K30EM\D*?(\d{4})(0[1-9]|1[0-2])")
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
INSTALL_HINT = "install the staging group (uv sync --group staging)"


@dataclass(frozen=True)
class Region:
    """One staged region: its key (the directory under ``camel/``), its
    box as ``(lon_min, lat_min, lon_max, lat_max)`` and what it covers."""

    key: str
    bbox: tuple[float, float, float, float]
    description: str


REGIONS: dict[str, Region] = {
    region.key: region
    for region in (
        # The whole globe: the confidence is defined over every disk's land.
        # A box's margin past the granule's edge selects nothing extra, and
        # the reader wraps a global field across the antimeridian itself.
        Region("global", (-180.0, -90.0, 180.0, 90.0), "Every land cell of every disk (Himawari, GOES-East, GOES-West)"),
    )
}


def parse_month(text: str) -> date:
    """``YYYY-MM`` as the first day of that month."""
    match = _MONTH.match(text)
    if not match:
        raise XueError(f"a month is spelled YYYY-MM, not {text!r}")
    return date(int(match.group(1)), int(match.group(2)), 1)


def default_months(today: date | None = None) -> list[date]:
    """The months a scheduled run stages: the current UTC month and the
    next, so a month is on the bucket before its first slot."""
    this = (today or datetime.now(UTC).date()).replace(day=1)
    following = (this.replace(day=28) + timedelta(days=7)).replace(day=1)
    return [this, following]


def granule_month(path: Path, asked: date) -> date:
    """The month a granule carries, from its name; the asked month when
    the name does not say (a granule renamed by hand)."""
    match = _GRANULE_MONTH.search(Path(path).name)
    if not match:
        LOG.warning("%s does not name its month; recording %s as its source month", Path(path).name, f"{asked:%Y-%m}")
        return asked
    return date(int(match.group(1)), int(match.group(2)), 1)


def subset(dataset: xr.Dataset, region: Region) -> xr.Dataset:
    """The region's box plus :data:`MARGIN_DEG` on each side, on
    coordinates sorted ascending; empty is an error."""
    lon_min, lat_min, lon_max, lat_max = region.bbox
    sorted_dataset = dataset.sortby(["latitude", "longitude"])
    cropped = sorted_dataset.sel(
        latitude=slice(lat_min - MARGIN_DEG, lat_max + MARGIN_DEG),
        longitude=slice(lon_min - MARGIN_DEG, lon_max + MARGIN_DEG),
    )
    if cropped.sizes.get("latitude", 0) == 0 or cropped.sizes.get("longitude", 0) == 0:
        raise ConversionError(f"the {region.key} box {region.bbox} selects no cell of the granule")
    return cropped


def staged_path(root: Path, region: Region, month: date) -> Path:
    return Path(root) / "camel" / region.key / f"{month:%Y%m}.nc"


def _fetch_granule(month: date, granules: Path) -> Path:
    try:
        from shachen.io.emissivity import fetch_emissivity  # noqa: PLC0415 - the staging group
    except ImportError as exc:
        raise DownloadError(f"staging needs shachen[data] ({exc}); {INSTALL_HINT}") from exc
    granules.mkdir(parents=True, exist_ok=True)
    try:
        return Path(fetch_emissivity(month, granules))
    except RuntimeError as exc:
        # fetch_emissivity's own "no granule within the fallback years".
        raise DownloadError(str(exc)) from exc


def _load_granule(path: Path) -> xr.Dataset:
    try:
        from shachen.io.emissivity import load_band_emissivity  # noqa: PLC0415 - the staging group
    except ImportError as exc:
        raise DownloadError(f"staging needs shachen and xarray ({exc}); {INSTALL_HINT}") from exc
    return load_band_emissivity(path)


def stage_month(
    region: Region,
    month: date,
    root: Path,
    *,
    fetch: Callable[[date, Path], Path] | None = None,
    load: Callable[[Path], xr.Dataset] | None = None,
) -> Path:
    """Write ``<root>/camel/<region>/<YYYYMM>.nc`` for the month, over
    whatever is there (restaging is how a bad subset is fixed).

    ``fetch`` takes the month's first day and the granule cache and
    returns a granule; ``load`` turns a granule into the band-interpolated
    dataset. Both default to shachen's, which need Earthdata credentials
    and the ``staging`` group."""
    first = month.replace(day=1)
    granule = (fetch or _fetch_granule)(first, Path(root) / GRANULES_DIR)
    source_month = granule_month(granule, first)
    if source_month != first:
        LOG.info("%s %s is served by the %s granule", region.key, f"{first:%Y-%m}", f"{source_month:%Y-%m}")
    cropped = subset((load or _load_granule)(granule), region)
    # The interpolation between hinges promotes to float64; the staged
    # file carries the granule's single precision.
    cropped = cropped.assign({name: cropped[name].astype(np.float32) for name in cropped.data_vars})
    cropped.attrs.update(
        region=region.key,
        month=f"{first:%Y-%m}",
        source_month=f"{source_month:%Y-%m}",
        source=SOURCE,
        bbox=list(region.bbox),
        margin_deg=MARGIN_DEG,
    )
    path = staged_path(root, region, first)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    try:
        cropped.to_netcdf(partial, encoding={name: {"zlib": True, "complevel": 4} for name in cropped.data_vars})
    except ImportError as exc:
        raise DownloadError(f"writing a staged month needs netCDF4 ({exc}); {INSTALL_HINT}") from exc
    partial.replace(path)
    LOG.info("staged %s", path)
    return path


def stage(
    root: Path,
    regions: list[Region] | None = None,
    months: list[date] | None = None,
    *,
    fetch: Callable[[date, Path], Path] | None = None,
    load: Callable[[Path], xr.Dataset] | None = None,
) -> list[Path]:
    """Every region of every month, granule by granule: the paths written."""
    written: list[Path] = []
    for month in months or default_months():
        for region in regions or list(REGIONS.values()):
            written.append(stage_month(region, month, root, fetch=fetch, load=load))
    return written


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m xuebuild.satellite.staging",
        description="Stage the CAMEL emissivity months the DEBRA confidence reads.",
    )
    parser.add_argument("--root", type=Path, default=Path("data/raw/ancillary"), help="the ancillary root (default: data/raw/ancillary)")
    parser.add_argument(
        "--region",
        action="append",
        metavar="KEY",
        choices=sorted(REGIONS),
        help="a region to stage (repeatable; default: every region)",
    )
    parser.add_argument("--month", action="append", metavar="YYYY-MM", help="a month to stage (repeatable; default: this month and the next)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if arguments.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        months = [parse_month(text) for text in arguments.month] if arguments.month else None
        regions = [REGIONS[key] for key in arguments.region] if arguments.region else None
        written = stage(arguments.root, regions, months)
    except XueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n".join(str(path) for path in written))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Per-model source registry: where a model's GRIB comes from and how its
run directory, manifest identity, and time axis are named.

The models share one output contract: whatever the source, the bundles carry
the same data variable ids (tmp2m, prate, ugrd10m/vgrd10m, and on sflux also
dswrf) so the decoder and frontend never care which model produced them.
ECMWF has no native rate field; its accumulated ``tp`` input is de-accumulated
into prate by the converter. GFS sflux has only interval-averaged PRATE (the
averaging window resets every 6 hours); the converter de-averages consecutive
frames into hourly rates.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DownloadError


@dataclass(frozen=True)
class SourceSpec:
    id: str
    """CLI / URL / directory id: "gfs" or "ecmwf"."""
    manifest_model: str
    """The manifest and bundle-metadata ``model`` string."""
    product: str
    """The manifest ``product`` string."""
    latest_filename: str
    """Per-model mutable live pointer at the data root. GFS uses the bare
    ``latest.json``; the other models use ``latest-<model>.json``."""
    step_hours: int
    """Uniform forecast step of the published series we consume."""
    input_variable_ids: tuple[str, ...]
    """Variables fetched from the source, in GRIB assembly order."""
    accumulated_precipitation: bool
    """True when precipitation arrives as a run-total accumulation (ECMWF
    ``tp``, metres) and must be de-accumulated into a rate."""
    averaged_precipitation: bool = False
    """True when precipitation arrives as an interval-averaged rate whose
    averaging window resets every :attr:`average_window_hours` (GFS sflux
    ``PRATE ave``) and must be de-averaged into per-step mean rates."""
    average_window_hours: int = 6
    """Length of the averaging-window reset cycle for averaged precipitation."""
    optional_at_analysis: tuple[str, ...] = ()
    """Input variables absent from the analysis (f000) file — sflux carries no
    PRATE record at f000, so the derived prate series starts at the first
    real step (mirroring the ECMWF de-accumulated prate axis)."""
    bundle_scalar_ids: tuple[str, ...] = ("tmp2m", "prate")
    """Scalar variables published as single-variable bundles, in manifest
    order (the wind pair always ships as the combined wind10m bundle)."""
    production_grid: tuple[int, int] = (1440, 721)
    """Grid a complete (``require_complete``) build must arrive on."""
    fetch_concurrency: int = 4
    """Frames fetched in parallel. Each frame costs several fresh HTTPS
    round-trips, so sequential fetching is latency-bound; NOAA's bucket
    takes a few parallel streams happily, while ECMWF's open data bucket
    answers bursts with 503 Slow Down and stays at 1."""


SOURCES: dict[str, SourceSpec] = {
    "gfs": SourceSpec(
        id="gfs",
        manifest_model="GFS",
        product="pgrb2.0p25",
        latest_filename="latest.json",
        step_hours=1,
        input_variable_ids=("tmp2m", "prate", "ugrd10m", "vgrd10m"),
        accumulated_precipitation=False,
    ),
    "ecmwf": SourceSpec(
        id="ecmwf",
        manifest_model="ECMWF",
        product="ifs-0p25",
        latest_filename="latest-ecmwf.json",
        step_hours=3,
        input_variable_ids=("tmp2m", "tp", "ugrd10m", "vgrd10m"),
        accumulated_precipitation=True,
        fetch_concurrency=1,
    ),
    # GFS surface flux files on the native ~13 km T1534 Gaussian grid
    # (3072x1536; GDAL reports a uniform geoTransform whose tiny latitude
    # deviation from the true Gaussian latitudes is far below a pixel).
    # Adds the dswrf solar-radiation layer; prate is de-averaged from the
    # window-cumulative PRATE averages.
    "sflux": SourceSpec(
        id="sflux",
        manifest_model="GFS-SFLUX",
        product="sfluxgrb",
        latest_filename="latest-sflux.json",
        step_hours=1,
        input_variable_ids=("tmp2m", "prate_ave", "ugrd10m", "vgrd10m", "dswrf"),
        accumulated_precipitation=False,
        averaged_precipitation=True,
        optional_at_analysis=("prate_ave",),
        bundle_scalar_ids=("tmp2m", "prate", "dswrf"),
        production_grid=(3072, 1536),
    ),
}

MODEL_PRODUCTS: dict[str, str] = {spec.manifest_model: spec.product for spec in SOURCES.values()}


def source_spec(model: str) -> SourceSpec:
    try:
        return SOURCES[model]
    except KeyError as exc:
        raise DownloadError(f"unsupported model: {model}") from exc

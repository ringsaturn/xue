"""Rebuild one archived run locally: fetch → crop → encode, never upload.

The same encoder as the live feed, pointed at a past cycle and written into
a directory of its own, so an archived run can be replayed (backtesting, a
case study) without touching the bucket. The output is an ordinary schema v5
manifest and the bundles' Zarr stores — the map store and, for the source's
``series_bundle_ids``, the series companion — and nothing else: no ``.xue``
container, no video, no reduced-resolution variants, no live pointer, no
STAC. :func:`xuebuild.showcase.build_case` is this plus a catalog row.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .binconvert import bundle_input_ids, published_bundle_ids
from .encoder import convert_bin
from .errors import XueError
from .fetch import fetch_run, parse_run
from .sources import source_spec

LOG = logging.getLogger(__name__)

LOCAL_DIRECTORY = "local"
"""Where local runs live under the data root (``data/local/``) and under the
raw root (``data/raw/local/``): a narrowed record set fetched for a few
bundles must never be mistaken for a full fetch of the live run."""


class LocalRunError(XueError):
    """A local run request is unusable, or its build does not match it."""


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    """``W,S,E,N`` in degrees, as ``build-local --bbox`` takes it."""
    parts = value.split(",")
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError as exc:
        raise LocalRunError(f"--bbox must be W,S,E,N in degrees, not {value!r}") from exc
    # The converter's crop checks the box against the grid (an antimeridian
    # crossing is fine on a wrapping one).
    return west, south, east, north


def build_local_run(
    model: str,
    run: str,
    hours: int,
    *,
    bbox: tuple[float, float, float, float] | None,
    bundle_ids: tuple[str, ...] | None,
    output_dir: Path,
    raw_root: Path,
    work_root: Path,
    profile: str = "quality",
    force: bool = False,
    force_download: bool = False,
    series: bool = True,
    zarr: bool = True,
    container: bool = False,
) -> dict[str, Any]:
    """Fetch ``run`` of ``model`` to ``hours``, crop it to ``bbox`` and encode
    ``bundle_ids`` into ``output_dir``; return the converter's report.

    ``bbox`` None keeps the whole domain and ``bundle_ids`` None builds every
    bundle the source publishes. Only those bundles' inputs are downloaded,
    into ``raw_root`` (``fetch_run`` adds the ``<model>.<run>`` directory).
    ``zarr`` and ``container`` exist for the showcase, which still decides
    them per build; a local run ships the store alone.
    """
    source = source_spec(model)
    if run == "latest":
        # The live cycle is `build-bin`'s; a local run names the one it replays.
        raise LocalRunError("a local run names its cycle as YYYYMMDDHH, not latest")
    parsed = parse_run(run, source.id)
    if bundle_ids is None:
        bundle_ids = published_bundle_ids(source)
    else:
        unknown = [bundle_id for bundle_id in bundle_ids if bundle_id not in published_bundle_ids(source)]
        if unknown:
            raise LocalRunError(f"{source.manifest_model} publishes {list(published_bundle_ids(source))}, not {unknown}")
    input_ids = tuple(
        dict.fromkeys(input_id for bundle_id in bundle_ids for input_id in bundle_input_ids(source, bundle_id))
    )
    if source.observation:
        LOG.info("fetching %s window %s +%d h (%s)", source.manifest_model, run, hours, ", ".join(input_ids))
    else:
        LOG.info("fetching %s run %s f000-f%03d (%s)", source.manifest_model, run, hours, ", ".join(input_ids))
    # Exactly the requested frames: the raw directory can hold more, left
    # behind by an earlier build of the same run with a longer range.
    inputs = fetch_run(parsed, hours, raw_root, force=force_download, model=model, input_ids=input_ids)

    manifest_path = output_dir / "manifest.json"
    report = convert_bin(
        inputs,
        output_dir,
        profile=profile,
        work_root=work_root,
        expected_hours=hours,
        manifest_path=manifest_path,
        force=force,
        # A cropped run is already small: the half-resolution ladder has
        # nothing left to save, and the H.264 companion cannot beat it.
        skip_video=True,
        skip_variants=True,
        model=model,
        bbox=bbox,
        bundle_ids=bundle_ids,
        zarr=zarr,
        container=container,
        series=series,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["forecastHours"] != hours:
        # A forecast run is held to its axis by the converter; a fetched
        # observation window can only come up short when the archive lacks
        # its last hour, and the requested range is never silently shortened.
        raise LocalRunError(
            f"the {source.manifest_model} {run} build reaches +{manifest['forecastHours']} h, "
            f"not the declared {hours}; the archive lacks the frames past that"
        )
    return report

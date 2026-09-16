"""The ``jma-radar`` tool as the pipeline invokes it.

The JMA precipitation nowcast reaches Xue through the sibling project
`jma-radar <https://github.com/ringsaturn/jma-radar>`_, which lists the
agency's ``targetTimes``, fetches and decodes the palette tiles, mosaics
them onto a lat/lon grid and writes a window of analyses as one NetCDF
series. It is driven as a subprocess the way GDAL, zstd, ffmpeg and eccodes
are (``jma-radar window --json``), so ``xuebuild`` keeps NumPy as its only
runtime dependency and the tool's own (httpx, Pillow, xarray, netCDF4)
stay in its wheel.

The command defaults to ``python -m jma_radar`` on this interpreter — the
tool installed into the project's own environment with
``uv pip install git+https://github.com/ringsaturn/jma-radar`` — and
``XUE_JMA_RADAR`` names another command (``jma-radar``, a path, a whole
command line) to run instead.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import DownloadError

LOG = logging.getLogger(__name__)

COMMAND_VARIABLE = "XUE_JMA_RADAR"
INSTALL_HINT = "uv pip install git+https://github.com/ringsaturn/jma-radar"


def command() -> list[str]:
    """The command line the tool is invoked with, without its arguments."""
    override = os.environ.get(COMMAND_VARIABLE, "").strip()
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "jma_radar"]


def version() -> str:
    """The tool's version, or a :class:`DownloadError` saying how to install
    it — the fetch's first step, so a runner without the tool fails before
    it lists anything."""
    try:
        result = subprocess.run(
            [*command(), "version"], text=True, capture_output=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DownloadError(f"jma-radar is not available ({exc}); install it with: {INSTALL_HINT}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise DownloadError(
            f"jma-radar is not available ({detail[-1] if detail else 'exit ' + str(result.returncode)}); "
            f"install it with: {INSTALL_HINT}"
        )
    return result.stdout.strip()


def fetch_window(
    *,
    start: str,
    hours: int,
    zoom: int,
    step: float,
    bbox: tuple[float, float, float, float],
    method: str,
    frames_dir: Path,
    output: Path,
    variable: str,
    concurrency: int,
) -> dict[str, Any]:
    """Run ``jma-radar window`` and return its JSON summary: the grid, one
    entry per frame (its ``validtime`` and cache ``path``) and the series
    file written to ``output``, its rate variable named ``variable`` (the
    observation ingest finds a variable by its Xue id). A window the
    listing holds nothing of is an error of the tool's (exit 1), reported
    as a :class:`DownloadError`."""
    output.parent.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)
    arguments = [
        *command(),
        "window",
        "--start",
        start,
        "--hours",
        str(hours),
        "--zoom",
        str(zoom),
        "--step",
        f"{step:g}",
        "--bbox",
        ",".join(f"{value:g}" for value in bbox),
        "--method",
        method,
        "--frames-dir",
        str(frames_dir),
        "--out",
        str(output),
        "--variable",
        variable,
        "--concurrency",
        str(concurrency),
        "--json",
    ]
    LOG.info("running %s", " ".join(shlex.quote(argument) for argument in arguments))
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise DownloadError(f"jma-radar could not be run ({exc}); install it with: {INSTALL_HINT}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise DownloadError(
            f"jma-radar window failed (exit {result.returncode}): {detail[-1] if detail else 'no output'}"
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"jma-radar window printed no JSON summary: {exc}") from exc
    if not isinstance(summary, dict) or not isinstance(summary.get("frames"), list):
        raise DownloadError("jma-radar window printed an unexpected summary")
    return summary

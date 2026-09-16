"""The ``cma-radar`` tool as the pipeline invokes it.

The CMA radar mosaic reaches Xue through a private sibling tool, invoked as
``cma-radar``, which decodes the agency's BIN tiles and keeps them as one
Zarr v3 store per UTC day on a private bucket of its own. What this
pipeline needs is the reverse trip: ``cma-radar window`` reads the written
slots of one UTC window back out of those stores and writes them as one
NetCDF series — the shape the same tool's ``fetch`` writes, which is what
the observation ingest has always read (:mod:`xuebuild.observation`). It
is driven as a subprocess the way GDAL, zstd, ffmpeg, eccodes and
``jma-radar`` are, so ``xuebuild`` keeps NumPy as its only runtime
dependency and the tool's own (zarr, s3fs, xarray, netCDF4) stay in its
wheel; the archive's location and credentials are the tool's too
(``XUE_CMA_ARCHIVE``, and ``R2_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` /
``R2_SECRET_ACCESS_KEY`` or the ``AWS_*`` pair the bucket uploads use).

The command defaults to the ``cma-radar`` script of this interpreter's
environment (the tool installed beside ``xuebuild``; the publish workflow
installs it from a repository secret), else ``cma-radar`` on the PATH, and
``XUE_CMA_RADAR`` names another command (a path, a whole command line) to
run instead.
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

COMMAND_VARIABLE = "XUE_CMA_RADAR"
SCRIPT_NAME = "cma-radar"
INSTALL_HINT = f"install the {SCRIPT_NAME} tool into this environment, or name its command in {COMMAND_VARIABLE}"


def command() -> list[str]:
    """The command line the tool is invoked with, without its arguments."""
    override = os.environ.get(COMMAND_VARIABLE, "").strip()
    if override:
        return shlex.split(override)
    beside_interpreter = Path(sys.executable).parent / SCRIPT_NAME
    if beside_interpreter.is_file():
        return [str(beside_interpreter)]
    return [SCRIPT_NAME]


def version() -> str:
    """The tool's version, or a :class:`DownloadError` saying how to install
    it — the fetch's first step, so a runner without the tool fails before
    it opens anything."""
    try:
        result = subprocess.run(
            [*command(), "version"], text=True, capture_output=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DownloadError(f"{SCRIPT_NAME} is not available ({exc}); {INSTALL_HINT}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise DownloadError(
            f"{SCRIPT_NAME} is not available ({detail[-1] if detail else 'exit ' + str(result.returncode)}); "
            f"{INSTALL_HINT}"
        )
    return result.stdout.strip()


def window(
    *,
    source: str,
    start: str,
    hours: int,
    zoom: int,
    product: str,
    output: Path | None,
) -> dict[str, Any]:
    """Run ``cma-radar window`` and return its JSON summary: one entry per
    slot the archive holds between ``start`` (a ``YYYYMMDDHH`` UTC hour)
    and ``hours`` past it, inclusive, with its status, and ``frames``, the
    written slots' times (``YYYY-MM-DDTHH:MM:SSZ``). With ``output`` the
    written slots are read and written there as one NetCDF series and the
    summary carries the grid; without it the stores' two small index arrays
    are all that is read, which is how the live window's newest frame is
    asked for. A window with nothing written in it is an error of the
    tool's (exit 1) only when a series was asked for."""
    arguments = [
        *command(),
        "window",
        "--source",
        source,
        "--start",
        start,
        "--hours",
        str(hours),
        "--zoom",
        str(zoom),
        "--product",
        product,
        "--json",
    ]
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        arguments += ["--out", str(output)]
    # The archive's location is private: the log names the command, not
    # the source it was given.
    LOG.info("running %s window --start %s --hours %d", shlex.quote(arguments[0]), start, hours)
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise DownloadError(f"{SCRIPT_NAME} could not be run ({exc}); {INSTALL_HINT}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise DownloadError(
            f"{SCRIPT_NAME} window failed (exit {result.returncode}): {detail[-1] if detail else 'no output'}"
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"{SCRIPT_NAME} window printed no JSON summary: {exc}") from exc
    if (
        not isinstance(summary, dict)
        or not isinstance(summary.get("frames"), list)
        or not isinstance(summary.get("slots"), list)
    ):
        raise DownloadError(f"{SCRIPT_NAME} window printed an unexpected summary")
    return summary

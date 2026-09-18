"""The ``om2nc`` tool as the pipeline invokes it.

ECMWF's IFS HRES reaches Xue through Open-Meteo, which redistributes the
forecast on its native O1280 reduced Gaussian grid as one ``.om`` file per
time step. Reading that format means the Open-Meteo SDKs, every one of which
is GPL-2.0-only, so the decoding lives in a separate project —
`om2nc <https://github.com/ringsaturn/om2nc>`_, GPL-2.0-only, published as a
statically linked binary — and this repository never imports, links or
vendors any of it. The tool is driven as a subprocess exactly the way GDAL,
zstd, ffmpeg, eccodes and ``jma-radar`` are: it reads the byte ranges of the
variables asked for, resamples the reduced Gaussian grid onto a regular
latitude/longitude one by nearest neighbour (the cell selection the
Open-Meteo API itself makes) and writes a CF NetCDF series, which
:mod:`xuebuild.observation` then reads like any other series.

The command defaults to ``om2nc`` on PATH — the release binary, which the
publishing workflow installs and checksums — and ``XUE_OM2NC`` names another
command (a path, a whole command line) to run instead.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .errors import DownloadError

LOG = logging.getLogger(__name__)

COMMAND_VARIABLE = "XUE_OM2NC"
INSTALL_HINT = "download a release binary from https://github.com/ringsaturn/om2nc/releases"


def command() -> list[str]:
    """The command line the tool is invoked with, without its arguments."""
    override = os.environ.get(COMMAND_VARIABLE, "").strip()
    if override:
        return shlex.split(override)
    return ["om2nc"]


def version() -> str:
    """The tool's version, or a :class:`DownloadError` saying how to install
    it — the fetch's first step, so a runner without the tool fails before it
    reads anything off the bucket."""
    try:
        result = subprocess.run(
            [*command(), "--version"], text=True, capture_output=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DownloadError(f"om2nc is not available ({exc}); {INSTALL_HINT}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise DownloadError(
            f"om2nc is not available ({detail[-1] if detail else 'exit ' + str(result.returncode)}); {INSTALL_HINT}"
        )
    return result.stdout.strip()


def step_spec(steps: Sequence[int]) -> str:
    """The tool's ``--step`` argument for an explicit list of forecast hours.

    The tool also takes ranges (``0..90``, ``93..144:3``), which would have
    to reproduce the source's own segment boundaries to mean the same thing;
    a comma list says exactly which hours are wanted and is 145 numbers long
    at its worst."""
    if not steps:
        raise DownloadError("a fetch needs at least one forecast hour")
    return ",".join(str(step) for step in steps)


def fetch_variable(
    *,
    model: str,
    init: str,
    steps: Sequence[int],
    variable: str,
    resolution: float,
    output: Path,
    concurrency: int,
    deflate: int = 1,
) -> None:
    """Write one variable's whole series to ``output`` with ``om2nc fetch``.

    ``model`` is the Open-Meteo model directory (``ecmwf_ifs``), ``init`` the
    run in the tool's own spelling (``2026-09-18T00Z``), ``steps`` the
    forecast hours to fetch and ``variable`` the Open-Meteo name of the
    quantity (:attr:`~xuebuild.variables.VariableSpec.open_meteo`), which is
    also the name of the variable inside the file. Every step asked for must
    exist: a variable that has no analysis file (an interval total, a mean,
    a maximum) is asked for its own steps and nothing else, so
    ``--missing-as-nan`` is never passed and a gap is an error rather than a
    plane of NaN. ``--accumulate`` is never passed either: this fetch takes
    every native step, so each interval total already covers the step it
    ends, and deriving a rate is the converters' business — a byte-identical
    one, which cannot live in another project.

    ``deflate`` is the tool's zlib level; 1 by default, because the file
    lives only until the conversion has read it and the runner writes it
    faster than it would compress it.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        *command(),
        "-q",
        "fetch",
        "--model",
        model,
        "--init",
        init,
        "--step",
        step_spec(steps),
        "--var",
        variable,
        "--resolution",
        f"{resolution:g}",
        "--deflate",
        str(deflate),
        "--concurrency",
        str(concurrency),
        "--overwrite",
        "-o",
        str(output),
    ]
    LOG.info("running %s", " ".join(shlex.quote(argument) for argument in arguments))
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise DownloadError(f"om2nc could not be run ({exc}); {INSTALL_HINT}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise DownloadError(
            f"om2nc fetch of {variable} failed (exit {result.returncode}): "
            f"{detail[-1] if detail else 'no output'}"
        )
    if not output.is_file():
        # The tool writes <name>.part and renames on success, so this is the
        # one thing a zero exit status does not already promise.
        raise DownloadError(f"om2nc reported success but wrote no series at {output}")

"""eccodes CLI subprocess wrapper (same pattern as gdal.py / zstdcli.py).

Two callers. The CCSDS GRIB path: ECMWF open data, DWD ICON-EU and
Météo-France ARPEGE messages are CCSDS/AEC packed (DRS template 5.42),
which many GDAL builds cannot decode, so ``grib_set`` repacks the
downloaded messages to ``grid_simple`` before GDAL reads them (and writes
the zero accumulation ARPEGE lacks at the analysis). And the tropical cyclone product (``xuebuild.tc``): ECMWF's
track files are BUFR, and ``bufr_dump`` is how they become JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ConversionError
from .gdal import require_command, run_command


def repack_grid_simple(source: Path, destination: Path) -> None:
    run_command(
        [require_command("grib_set"), "-r", "-s", "packingType=grid_simple", str(source), str(destination)],
        description=f"repack {source} to grid_simple",
    )


def zero_accumulation_at_analysis(source: Path, destination: Path) -> None:
    """The run-total accumulation records of ``source`` restamped to the
    analysis (an empty interval ending at the run time) with every value
    zero: the record a source that writes no accumulation at step 0 (the
    ARPEGE packages) would have written, the same shape ICON-EU's
    ``TOT_PREC`` takes at step 0. A run total at the run time is zero by
    definition, and it is what the converter differences the first step
    against."""
    run_command(
        [require_command("grib_set"), "-d", "0", "-s", "endStep=0", str(source), str(destination)],
        description=f"restamp {source} as a zero accumulation at the analysis",
    )


def bufr_dump_json(source: Path) -> dict[str, Any]:
    """``bufr_dump -j f``: the file's data section as one flat list of
    ``{key, value, index, …}`` entries under ``messages`` — every message
    and, in an uncompressed message, every subset in sequence; a
    compressed message's per-subset values arrive as arrays. The flat
    form is what :mod:`xuebuild.tc.bufrtracks` walks; the structured
    ``-j s`` nests replications a dozen levels deep for no gain here."""
    completed = run_command(
        [require_command("bufr_dump"), "-j", "f", str(source)],
        description=f"bufr_dump {source}",
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"bufr_dump produced invalid JSON for {source}: {exc}") from exc

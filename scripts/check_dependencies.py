#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import subprocess
import sys


def command_version(command: str, arguments: list[str], pattern: str) -> tuple[int, ...] | None:
    executable = shutil.which(command)
    if not executable:
        print(f"missing: {command}", file=sys.stderr)
        return None
    result = subprocess.run([executable, *arguments], text=True, capture_output=True, check=False)
    output = result.stdout + result.stderr
    match = re.search(pattern, output)
    if result.returncode or not match:
        print(f"could not determine {command} version", file=sys.stderr)
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def main() -> int:
    discovered = {
        "Python": (sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        "GDAL": command_version("gdalinfo", ["--version"], r"GDAL\s+(\d+(?:\.\d+)+)"),
        "Node.js": command_version("node", ["--version"], r"v(\d+(?:\.\d+)+)"),
        "zstd": command_version("zstd", ["--version"], r"v(\d+(?:\.\d+)+)"),
    }
    minimums = {"Python": (3, 12), "GDAL": (3, 8), "Node.js": (22, 0), "zstd": (1, 5, 0)}
    failed = False
    for name, minimum in minimums.items():
        version = discovered[name]
        if version is None:
            failed = True
            continue
        formatted = ".".join(str(part) for part in version)
        if version < minimum:
            print(f"unsupported: {name} {formatted}, require {'.'.join(map(str, minimum))}+", file=sys.stderr)
            failed = True
        else:
            print(f"ok: {name} {formatted}")
    for utility in ("gdal_translate", "npm"):
        if shutil.which(utility):
            print(f"ok: {utility}")
        else:
            print(f"missing: {utility}", file=sys.stderr)
            failed = True
    # eccodes is only needed for the ECMWF source (its open data is CCSDS
    # packed and gets repacked with grib_set); GFS-only builds run without it.
    if shutil.which("grib_set"):
        print("ok: grib_set (eccodes)")
    else:
        print("warning: grib_set (eccodes) missing — ECMWF builds need it, GFS builds do not", file=sys.stderr)
    try:
        import numpy

        print(f"ok: numpy {numpy.__version__}")
    except ImportError:
        print("missing: numpy (run `uv sync` and use .venv/bin/python)", file=sys.stderr)
        failed = True

    # ffmpeg is optional: it only builds the WebCodecs temperature video
    # artifact, and its absence just skips that artifact rather than failing
    # the build (see xue/videoconvert.py).
    if shutil.which("ffmpeg"):
        print("ok: ffmpeg")
    else:
        print("optional: ffmpeg not found, temperature video artifact will be skipped", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

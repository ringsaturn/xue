from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .binconvert import convert_bin, verify_bin
from .errors import XueError
from .fetch import fetch_run, resolve_run
from .sources import SOURCES, source_spec


def forecast_hours(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hours must be an integer") from exc
    if not 0 <= parsed <= 384:
        raise argparse.ArgumentTypeError("hours must be between 0 and 384")
    return parsed


def _common_run_arguments(parser: argparse.ArgumentParser, *, force_help: str) -> None:
    parser.add_argument("--run", default="latest", help="latest or a UTC cycle in YYYYMMDDHH format")
    parser.add_argument(
        "--hours",
        type=forecast_hours,
        default=240,
        help="last forecast hour, inclusive; must lie on the model's published axis "
        "(e.g. GFS: hourly to 120, then 3-hourly to 240)",
    )
    parser.add_argument("--force", action="store_true", help=force_help)


def _model_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        choices=tuple(SOURCES),
        default="gfs",
        help=(
            "forecast source: NOAA GFS 0.25 degree (hourly), ECMWF IFS open data "
            "(3-hourly), or GFS surface flux on the native ~13 km grid (hourly, adds dswrf)"
        ),
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="xue", description="Pack weather forecasts into Xue bundles and build the viewer")
    root.add_argument("-v", "--verbose", action="store_true", help="show detailed progress")
    commands = root.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="download exact GRIB2 records from NOAA or ECMWF")
    _common_run_arguments(fetch, force_help="download and replace existing GRIB files")
    _model_argument(fetch)
    fetch.add_argument("--raw-dir", type=Path, default=Path("data/raw"))

    convert_bin_parser = commands.add_parser("convert-bin", help="convert a GRIB run into per-variable Xue bundles")
    convert_bin_parser.add_argument("input", type=Path)
    _model_argument(convert_bin_parser)
    convert_bin_parser.add_argument("--output", type=Path, required=True, help="output directory for per-variable .xue files")
    convert_bin_parser.add_argument("--profile", choices=("quality", "compact", "balanced"), default="quality")
    convert_bin_parser.add_argument("--work-dir", type=Path, default=Path("data/work"))
    convert_bin_parser.add_argument("--manifest", type=Path, help="write a schema v3 manifest.json to this path")
    convert_bin_parser.add_argument("--force", action="store_true", help="replace an existing manifest")
    convert_bin_parser.add_argument(
        "--skip-video",
        action="store_true",
        help="do not build the optional per-variable WebCodecs video artifacts",
    )
    convert_bin_parser.add_argument(
        "--skip-variants",
        action="store_true",
        help="do not build the half-resolution .half.xue variant bundles",
    )

    verify_bin_parser = commands.add_parser("verify-bin", help="validate and fully decode a Xue bundle")
    verify_bin_parser.add_argument("bundle", type=Path)

    build_bin_parser = commands.add_parser("build-bin", help="fetch a run and build per-variable Xue bundles and the manifest")
    _common_run_arguments(build_bin_parser, force_help="replace existing bundle and manifest")
    _model_argument(build_bin_parser)
    build_bin_parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    build_bin_parser.add_argument("--output-dir", type=Path, default=Path("web/public/data"))
    build_bin_parser.add_argument("--work-dir", type=Path, default=Path("data/work"))
    build_bin_parser.add_argument("--profile", choices=("quality", "compact", "balanced"), default="quality")
    build_bin_parser.add_argument(
        "--force-download",
        action="store_true",
        help="download GRIB files again even when valid local files exist",
    )
    build_bin_parser.add_argument(
        "--skip-video",
        action="store_true",
        help="do not build the optional per-variable WebCodecs video artifacts",
    )
    build_bin_parser.add_argument(
        "--skip-variants",
        action="store_true",
        help="do not build the half-resolution .half.xue variant bundles",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        if arguments.command == "fetch":
            run = resolve_run(arguments.run, hours=arguments.hours, model=arguments.model)
            paths = fetch_run(run, arguments.hours, arguments.raw_dir, force=arguments.force, model=arguments.model)
            print("\n".join(str(path) for path in paths))
        elif arguments.command == "convert-bin":
            report = convert_bin(
                arguments.input,
                arguments.output,
                profile=arguments.profile,
                work_root=arguments.work_dir,
                manifest_path=arguments.manifest,
                force=arguments.force,
                skip_video=arguments.skip_video,
                skip_variants=arguments.skip_variants,
                model=arguments.model,
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "verify-bin":
            print(json.dumps(verify_bin(arguments.bundle), indent=2))
        elif arguments.command == "build-bin":
            source = source_spec(arguments.model)
            run = resolve_run(arguments.run, hours=arguments.hours, model=arguments.model)
            run_directory = f"{source.id}.{run.id}"
            raw_directory = arguments.raw_dir / run_directory
            fetch_run(run, arguments.hours, arguments.raw_dir, force=arguments.force_download, model=arguments.model)
            report = convert_bin(
                raw_directory,
                arguments.output_dir / run_directory,
                profile=arguments.profile,
                work_root=arguments.work_dir,
                require_complete=True,
                expected_hours=arguments.hours,
                # The manifest is immutable and lives
                # inside the run directory; the tiny mutable per-model latest
                # pointer at the data root is what takes a new run live.
                manifest_path=arguments.output_dir / run_directory / "manifest.json",
                latest_path=arguments.output_dir / source.latest_filename,
                run_id=run.id,
                force=arguments.force,
                skip_video=arguments.skip_video,
                skip_variants=arguments.skip_variants,
                model=arguments.model,
            )
            print(json.dumps(report, indent=2))
        return 0
    except XueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

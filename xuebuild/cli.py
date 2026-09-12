from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .assemble import DEFAULT_MAX_GROUPS, assemble_run, bundle_group_matrix, bundle_group_slug, partial_manifest_path
from .binconvert import bundle_input_ids, published_bundle_ids, verify_bin
from .encoder import convert_bin
from .errors import ConversionError, XueError
from .fetch import fetch_run, parse_run, resolve_run
from .showcase import build_case, load_cases, write_catalog
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


def _model_argument(parser: argparse.ArgumentParser, *, live_only: bool = True) -> None:
    """The --model choice. Fetching and live runs are for forecast sources
    only; conversion also takes an observation source, whose input is one
    local NetCDF file rather than a fetched cycle."""
    choices = tuple(name for name, source in SOURCES.items() if source.live or not live_only)
    parser.add_argument(
        "--model",
        choices=choices,
        default="gfs",
        help=(
            "data source: NOAA GFS 0.25 degree (hourly), ECMWF IFS open data "
            "(3-hourly), or GFS surface flux on the native ~13 km grid (hourly, adds dswrf)"
            + ("" if live_only else "; radar is the CMA mosaic, read from a local NetCDF file")
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

    convert_bin_parser = commands.add_parser(
        "convert-bin", help="convert a GRIB run (or one observation NetCDF file) into per-variable Xue bundles"
    )
    convert_bin_parser.add_argument("input", type=Path)
    _model_argument(convert_bin_parser, live_only=False)
    convert_bin_parser.add_argument("--output", type=Path, required=True, help="output directory for per-variable .xue files")
    convert_bin_parser.add_argument("--profile", choices=("quality", "compact", "balanced"), default="quality")
    convert_bin_parser.add_argument("--work-dir", type=Path, default=Path("data/work"))
    convert_bin_parser.add_argument("--manifest", type=Path, help="write a schema v3 manifest.json to this path")
    convert_bin_parser.add_argument("--force", action="store_true", help="replace an existing manifest")
    convert_bin_parser.add_argument(
        "--hours",
        type=forecast_hours,
        help="observation sources only: last hour of the series to include, counted from its first frame",
    )
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
    build_bin_parser.add_argument(
        "--bundles",
        nargs="+",
        metavar="BUNDLE",
        help="build only these bundles (one job of a fanned-out publish): fetch just their inputs, "
        "write a manifest.part.<group>.json beside them instead of manifest.json, and no live pointer",
    )

    assemble_parser = commands.add_parser(
        "assemble-run",
        help="merge the manifest.part.*.json of a run built with --bundles into its manifest.json and the live pointer",
    )
    assemble_parser.add_argument("--run", required=True, help="the UTC cycle the parts were built from, YYYYMMDDHH")
    assemble_parser.add_argument(
        "--hours",
        type=forecast_hours,
        default=240,
        help="last forecast hour the parts were built to, inclusive",
    )
    assemble_parser.add_argument("--force", action="store_true", help="replace an existing manifest")
    _model_argument(assemble_parser)
    assemble_parser.add_argument("--output-dir", type=Path, default=Path("web/public/data"))

    groups_parser = commands.add_parser(
        "bundle-groups",
        help="print the bundle groups a fanned-out publish builds, as a GitHub Actions matrix (JSON)",
    )
    _model_argument(groups_parser)
    groups_parser.add_argument(
        "--max-jobs",
        type=int,
        default=DEFAULT_MAX_GROUPS,
        help=f"most groups to split the run into (default {DEFAULT_MAX_GROUPS})",
    )

    showcase_parser = commands.add_parser(
        "showcase",
        help="build the historical showcase cases (past runs cropped to one weather event)",
    )
    showcase_commands = showcase_parser.add_subparsers(dest="showcase_command", required=True)

    showcase_build = showcase_commands.add_parser("build", help="fetch, crop and encode one or more cases")
    showcase_build.add_argument(
        "cases",
        nargs="*",
        help="case ids to build; every definition in --cases-dir when omitted",
    )
    showcase_build.add_argument("--cases-dir", type=Path, default=Path("showcase/cases"))
    showcase_build.add_argument("--output-dir", type=Path, default=Path("web/public/data"))
    showcase_build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    showcase_build.add_argument("--work-dir", type=Path, default=Path("data/work"))
    showcase_build.add_argument("--force", action="store_true", help="replace an existing case manifest")
    showcase_build.add_argument(
        "--force-download",
        action="store_true",
        help="download GRIB files again even when valid local files exist",
    )
    showcase_build.add_argument(
        "--skip-catalog",
        action="store_true",
        help="do not rewrite showcase.json after building",
    )

    showcase_catalog = showcase_commands.add_parser(
        "catalog", help="rewrite showcase.json from the cases already built on disk"
    )
    showcase_catalog.add_argument("--output-dir", type=Path, default=Path("web/public/data"))

    showcase_check = showcase_commands.add_parser("check", help="validate the case definitions without building")
    showcase_check.add_argument("cases", nargs="*")
    showcase_check.add_argument("--cases-dir", type=Path, default=Path("showcase/cases"))
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
                last_hour=arguments.hours,
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "verify-bin":
            print(json.dumps(verify_bin(arguments.bundle), indent=2))
        elif arguments.command == "showcase":
            if arguments.showcase_command == "catalog":
                print(write_catalog(arguments.output_dir))
            elif arguments.showcase_command == "check":
                for spec in load_cases(arguments.cases_dir, tuple(arguments.cases)):
                    origin = spec.run or str(spec.dataset_path)
                    print(f"{spec.id}: {spec.model} {origin} f000-f{spec.hours:03d} {list(spec.variables)}")
            else:
                entries = [
                    build_case(
                        spec,
                        output_root=arguments.output_dir,
                        raw_root=arguments.raw_dir,
                        work_root=arguments.work_dir,
                        force=arguments.force,
                        force_download=arguments.force_download,
                    )
                    for spec in load_cases(arguments.cases_dir, tuple(arguments.cases))
                ]
                if not arguments.skip_catalog:
                    write_catalog(arguments.output_dir)
                print(json.dumps(entries, indent=2, ensure_ascii=False))
        elif arguments.command == "build-bin":
            source = source_spec(arguments.model)
            bundle_ids = tuple(dict.fromkeys(arguments.bundles)) if arguments.bundles else None
            if bundle_ids is not None:
                unknown = [bundle_id for bundle_id in bundle_ids if bundle_id not in published_bundle_ids(source)]
                if unknown:
                    raise ConversionError(
                        f"{source.manifest_model} publishes {list(published_bundle_ids(source))}, not {unknown}"
                    )
            run = resolve_run(arguments.run, hours=arguments.hours, model=arguments.model)
            run_directory = f"{source.id}.{run.id}"
            output_directory = arguments.output_dir / run_directory
            if bundle_ids is None:
                raw_root = arguments.raw_dir
                input_ids = None
                # The manifest is immutable and lives inside the run
                # directory; the tiny mutable per-model latest pointer at the
                # data root is what takes a new run live.
                manifest_path = output_directory / "manifest.json"
                latest_path: Path | None = arguments.output_dir / source.latest_filename
            else:
                # One group of a fanned-out publish: only these bundles'
                # inputs are fetched, into a raw directory of their own so the
                # narrowed GRIB files are never mistaken for a full fetch of
                # the run, and the manifest written is a part for
                # `assemble-run` to merge — no pointer until then.
                raw_root = arguments.raw_dir / "partial" / bundle_group_slug(bundle_ids)
                input_ids = tuple(
                    dict.fromkeys(
                        input_id for bundle_id in bundle_ids for input_id in bundle_input_ids(source, bundle_id)
                    )
                )
                manifest_path = partial_manifest_path(output_directory, bundle_ids)
                latest_path = None
            fetch_run(
                run,
                arguments.hours,
                raw_root,
                force=arguments.force_download,
                model=arguments.model,
                input_ids=input_ids,
            )
            report = convert_bin(
                raw_root / run_directory,
                output_directory,
                profile=arguments.profile,
                work_root=arguments.work_dir,
                require_complete=True,
                expected_hours=arguments.hours,
                manifest_path=manifest_path,
                latest_path=latest_path,
                run_id=run.id if latest_path is not None else None,
                force=arguments.force,
                skip_video=arguments.skip_video,
                skip_variants=arguments.skip_variants,
                model=arguments.model,
                bundle_ids=bundle_ids,
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "assemble-run":
            # The parts were built from one concrete cycle; nothing here
            # touches the network to find one.
            if arguments.run == "latest":
                raise XueError("assemble-run needs the cycle the parts were built from: --run YYYYMMDDHH")
            run = parse_run(arguments.run)
            report = assemble_run(
                arguments.output_dir,
                model=arguments.model,
                run_id=run.id,
                expected_hours=arguments.hours,
                force=arguments.force,
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "bundle-groups":
            print(json.dumps(bundle_group_matrix(source_spec(arguments.model), arguments.max_jobs)))
        return 0
    except XueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

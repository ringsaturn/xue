from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .assemble import (
    DEFAULT_MAX_GROUPS,
    assemble_run,
    bundle_group_matrix,
    bundle_group_slug,
    missing_bundle_ids,
    partial_manifest_path,
    read_manifest,
)
from .binconvert import bundle_input_ids, published_bundle_ids, verify_bin
from .encoder import convert_bin
from .errors import ConversionError, XueError
from .fetch import WINDOW_FILENAME, fetch_run, parse_run, resolve_run, window_summary
from .showcase import CASE_SIDECAR, build_case, load_cases, refresh_sidecar, write_catalog
from .sources import SOURCES, source_spec
from .zarrstore import (
    DEFAULT_INDEX_LOCATION,
    INDEX_LOCATIONS,
    container_enabled_by_environment,
    enabled_by_environment,
    export_bundle,
    store_path_for,
)
from .tc.build import build_product as build_tc_product
from .tc.build import load_previous_index as load_previous_tc_index
from .tc.fetch import SOURCE_IDS as TC_SOURCE_IDS
from .tc.fetch import fetch_sources as fetch_tc_sources
from .tc.schema import parse_issue as parse_tc_issue


def forecast_hours(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hours must be an integer") from exc
    if not 0 <= parsed <= 384:
        raise argparse.ArgumentTypeError("hours must be between 0 and 384")
    return parsed


def round_name(value: str) -> str:
    """``HHMM`` of a rolling-window round, or ``now`` for the current UTC
    minute."""
    if value == "now":
        return datetime.now(UTC).strftime("%H%M")
    if len(value) != 4 or not value.isdigit() or int(value[:2]) > 23 or int(value[2:]) > 59:
        raise argparse.ArgumentTypeError("round must be HHMM (UTC) or now")
    return value


def _common_run_arguments(parser: argparse.ArgumentParser, *, force_help: str) -> None:
    parser.add_argument(
        "--run",
        default="latest",
        help="latest or a UTC cycle in YYYYMMDDHH format; on an observation source, the window's "
        "first hour, or latest for the live window ending at the bucket's newest frame",
    )
    parser.add_argument(
        "--hours",
        type=forecast_hours,
        default=None,
        help="last forecast hour, inclusive; must lie on the model's published axis "
        "(e.g. GFS: hourly to 120, then 3-hourly to 240); defaults to the whole axis "
        "the model publishes (240 for the global models, 18 for HRRR); on an observation "
        "source, the window length in hours (3 for MRMS)",
    )
    parser.add_argument("--force", action="store_true", help=force_help)


def _model_argument(parser: argparse.ArgumentParser, *, fetched_only: bool = True) -> None:
    """The --model choice. Fetching and building are for the sources with a
    bucket to fetch from — every forecast, and the MRMS mosaic; conversion
    also takes the local-file observation source, whose input is one NetCDF
    file rather than a fetched run."""
    choices = tuple(name for name, source in SOURCES.items() if source.fetched or not fetched_only)
    parser.add_argument(
        "--model",
        choices=choices,
        default="gfs",
        help=(
            "data source: NOAA GFS 0.25 degree (hourly), ECMWF IFS open data "
            "(3-hourly), GFS surface flux on the native ~13 km grid (hourly, adds dswrf), "
            "NOAA HRRR over the contiguous US (3 km, a cycle every hour, hourly to 18), "
            "or NOAA MRMS, the radar mosaic over the contiguous US (an observation every "
            "two minutes; --run names the window's first hour and --hours its length, 3 by default)"
            + ("" if fetched_only else "; radar is the CMA mosaic, read from a local NetCDF file")
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
    _model_argument(convert_bin_parser, fetched_only=False)
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
    convert_bin_parser.add_argument(
        "--zarr",
        action="store_true",
        help="also derive a Zarr v3 store beside every bundle and variant (docs/zarr-profile.md) and name it "
        "in the manifest; XUE_ZARR=1 in the environment does the same",
    )
    convert_bin_parser.add_argument(
        "--no-xue",
        dest="container",
        action="store_false",
        help="publish the Zarr store alone: remove each .xue once its store and video companions are read "
        "out of it and name no container in the manifest (needs --zarr); XUE_CONTAINER=0 does the same",
    )

    verify_bin_parser = commands.add_parser("verify-bin", help="validate and fully decode a Xue bundle")
    verify_bin_parser.add_argument("bundle", type=Path)

    export_zarr_parser = commands.add_parser(
        "export-zarr",
        help="derive a Zarr v3 store from a Xue bundle: the same codes, re-chunked onto a regular six-frame time "
        "grid, one shard per time chunk (docs/zarr-profile.md)",
    )
    export_zarr_parser.add_argument("bundle", type=Path)
    export_zarr_parser.add_argument(
        "--out", type=Path, help="the store's root directory (default: <bundle>.zarr beside the bundle)"
    )
    export_zarr_parser.add_argument(
        "--delta",
        action="store_true",
        help="put the xue.delta codec in front of zstd on the variables the bundle predicts from the previous "
        "frame, so their chunks compress to the bundle's own bytes; a client then needs the codec registered",
    )
    export_zarr_parser.add_argument(
        "--index-location",
        choices=INDEX_LOCATIONS,
        default=DEFAULT_INDEX_LOCATION,
        help=f"where each shard keeps its chunk index (default {DEFAULT_INDEX_LOCATION}, which a client fetches "
        "as a suffix range without knowing the object's length)",
    )

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
        "--zarr",
        action="store_true",
        help="also derive a Zarr v3 store beside every bundle and variant (docs/zarr-profile.md) and name it "
        "in the manifest; XUE_ZARR=1 in the environment does the same",
    )
    build_bin_parser.add_argument(
        "--no-xue",
        dest="container",
        action="store_false",
        help="publish the Zarr store alone: remove each .xue once its store and video companions are read "
        "out of it and name no container in the manifest (needs --zarr); XUE_CONTAINER=0 does the same",
    )
    build_bin_parser.add_argument(
        "--bundles",
        nargs="+",
        metavar="BUNDLE",
        help="build only these bundles (one job of a fanned-out publish): fetch just their inputs, "
        "write a manifest.part.<group>.json beside them instead of manifest.json, and no live pointer",
    )
    build_bin_parser.add_argument(
        "--round",
        type=round_name,
        metavar="HHMM",
        help="one round of a rolling window (the live MRMS feed): write the run into the "
        "<model>.<run>/<HHMM>/ subdirectory, so a rebuild of the same run never overwrites artifacts "
        "a viewer may still be reading, and point the live pointer at that round; now for the current "
        "UTC minute",
    )

    assemble_parser = commands.add_parser(
        "assemble-run",
        help="merge the manifest.part.*.json of a run built with --bundles into its manifest.json and the live pointer",
    )
    assemble_parser.add_argument("--run", required=True, help="the UTC cycle the parts were built from, YYYYMMDDHH")
    assemble_parser.add_argument(
        "--hours",
        type=forecast_hours,
        default=None,
        help="last forecast hour the parts were built to, inclusive (default: the whole axis the model publishes)",
    )
    assemble_parser.add_argument("--force", action="store_true", help="replace an existing manifest")
    assemble_parser.add_argument(
        "--base-manifest",
        type=Path,
        help=(
            "top up instead of assemble: the run's live manifest, whose entries the parts are merged onto "
            "(a part replaces the live entry for the same bundle)"
        ),
    )
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
    groups_parser.add_argument(
        "--base-manifest",
        type=Path,
        help=(
            "group only the published bundles this manifest (the run as R2 serves it) lacks — "
            "the top-up's build jobs; prints [] when it lacks none"
        ),
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
    showcase_build.add_argument(
        "--zarr",
        action="store_true",
        help="also derive a Zarr v3 store beside every bundle of the case (or set XUE_ZARR=1)",
    )

    showcase_catalog = showcase_commands.add_parser(
        "catalog", help="rewrite showcase.json from the cases already built on disk"
    )
    showcase_catalog.add_argument("--output-dir", type=Path, default=Path("web/public/data"))

    showcase_refresh = showcase_commands.add_parser(
        "refresh",
        help="rewrite built cases' catalog rows (title, summary, tags, credit) from their definitions, without rebuilding",
    )
    showcase_refresh.add_argument("cases", nargs="*", help="case ids; every built definition in --cases-dir when omitted")
    showcase_refresh.add_argument("--cases-dir", type=Path, default=Path("showcase/cases"))
    showcase_refresh.add_argument("--output-dir", type=Path, default=Path("web/public/data"))

    showcase_check = showcase_commands.add_parser("check", help="validate the case definitions without building")
    showcase_check.add_argument("cases", nargs="*")
    showcase_check.add_argument("--cases-dir", type=Path, default=Path("showcase/cases"))

    tc_build = commands.add_parser(
        "tc-build",
        help="fetch the tropical cyclone track sources and write one tc.<issue>/ product directory and its pointer",
    )
    tc_build.add_argument(
        "--issue",
        default="now",
        help="the aggregation hour, YYYYMMDDHH in UTC, or now (the current hour); one directory per hour",
    )
    tc_build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    tc_build.add_argument("--output-dir", type=Path, default=Path("web/public/data"))
    tc_build.add_argument(
        "--sources",
        help=f"comma-separated subset of the sources to build from (default: all of {','.join(TC_SOURCE_IDS)})",
    )
    tc_build.add_argument(
        "--previous-index",
        type=Path,
        help="the previous hour's index.json, whose system ids carry over (default: the one the local "
        "latest-tc.json names, if any; `make live-tc-index` fetches the live one)",
    )
    tc_build.add_argument("--offline", action="store_true", help="build from what is already fetched; touch no network")
    tc_build.add_argument("--force", action="store_true", help="rebuild an issue whose directory exists")
    tc_build.add_argument("--force-download", action="store_true", help="fetch every source again")
    return root


def _run_hours(arguments: argparse.Namespace) -> int:
    """``--hours`` as given, else the whole axis the model publishes."""
    if arguments.hours is not None:
        return arguments.hours
    return source_spec(arguments.model).horizon_hours


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        if arguments.command in ("fetch", "build-bin", "assemble-run"):
            arguments.hours = _run_hours(arguments)
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
                zarr=arguments.zarr or enabled_by_environment(),
                container=arguments.container and container_enabled_by_environment(),
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "verify-bin":
            print(json.dumps(verify_bin(arguments.bundle), indent=2))
        elif arguments.command == "export-zarr":
            report = export_bundle(
                arguments.bundle,
                arguments.out if arguments.out is not None else store_path_for(arguments.bundle),
                delta=arguments.delta,
                index_location=arguments.index_location,
            )
            print(json.dumps(report.to_dict(), indent=2))
        elif arguments.command == "showcase":
            if arguments.showcase_command == "catalog":
                print(write_catalog(arguments.output_dir))
            elif arguments.showcase_command == "refresh":
                specs = load_cases(arguments.cases_dir, tuple(arguments.cases))
                if not arguments.cases:
                    # Every definition that has a build on disk; the rest are
                    # simply not published yet.
                    specs = [
                        spec for spec in specs if (arguments.output_dir / spec.output_subdirectory / CASE_SIDECAR).is_file()
                    ]
                entries = [refresh_sidecar(spec, arguments.output_dir) for spec in specs]
                write_catalog(arguments.output_dir)
                print(json.dumps(entries, indent=2, ensure_ascii=False))
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
                        zarr=arguments.zarr or enabled_by_environment(),
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
            if arguments.round is not None and bundle_ids is not None:
                raise ConversionError("--round builds a whole run; it cannot be combined with --bundles")
            run = resolve_run(arguments.run, hours=arguments.hours, model=arguments.model)
            run_directory = f"{source.id}.{run.id}"
            output_directory = arguments.output_dir / run_directory
            if arguments.round is not None:
                output_directory = output_directory / arguments.round
            if bundle_ids is None:
                raw_root = arguments.raw_dir
                input_ids = None
                # The manifest is immutable and lives inside the run
                # directory; the tiny mutable per-model latest pointer at the
                # data root is what takes a new run live.
                manifest_path = output_directory / "manifest.json"
                # A rolling window's pointer names the round's manifest
                # inside the run directory; the pointer itself stays at the
                # root like every other model's.
                latest_path: Path | None = (
                    arguments.output_dir / source.latest_filename if source.latest_filename else None
                )
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
            window: dict[str, object] | None = None
            if source.observation:
                # What the window holds, beside its manifest: the rolling
                # publish reads the live round's copy to know whether the
                # bucket has a newer frame than the one it last built to.
                window = window_summary(raw_root / run_directory)
                window.update({"run": run.id, "round": arguments.round, "hours": arguments.hours})
                output_directory.mkdir(parents=True, exist_ok=True)
                (output_directory / WINDOW_FILENAME).write_text(
                    json.dumps(window, indent=2) + "\n", encoding="utf-8"
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
                zarr=arguments.zarr or enabled_by_environment(),
                container=arguments.container and container_enabled_by_environment(),
            )
            report["run"] = run.id
            if arguments.round is not None:
                report["round"] = arguments.round
            if window is not None:
                report["window"] = window
            print(json.dumps(report, indent=2))
        elif arguments.command == "assemble-run":
            # The parts were built from one concrete cycle; nothing here
            # touches the network to find one.
            if arguments.run == "latest":
                raise XueError("assemble-run needs the cycle the parts were built from: --run YYYYMMDDHH")
            run = parse_run(arguments.run, arguments.model)
            report = assemble_run(
                arguments.output_dir,
                model=arguments.model,
                run_id=run.id,
                expected_hours=arguments.hours,
                force=arguments.force,
                base_manifest=arguments.base_manifest,
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "bundle-groups":
            source = source_spec(arguments.model)
            bundle_ids = None
            if arguments.base_manifest is not None:
                bundle_ids = missing_bundle_ids(source, read_manifest(arguments.base_manifest, what="live manifest"))
            print(json.dumps(bundle_group_matrix(source, arguments.max_jobs, bundle_ids)))
        elif arguments.command == "tc-build":
            if arguments.issue == "now":
                issue = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            else:
                issue = parse_tc_issue(arguments.issue)
            sources = TC_SOURCE_IDS
            if arguments.sources:
                sources = tuple(dict.fromkeys(item.strip() for item in arguments.sources.split(",") if item.strip()))
                unknown = [item for item in sources if item not in TC_SOURCE_IDS]
                if unknown:
                    raise XueError(f"unknown tc sources {unknown}; choose from {', '.join(TC_SOURCE_IDS)}")
            if not arguments.offline:
                fetch_tc_sources(arguments.raw_dir, issue, sources, force=arguments.force_download)
            previous = load_previous_tc_index(arguments.previous_index, arguments.output_dir)
            report = build_tc_product(
                issue,
                arguments.raw_dir,
                arguments.output_dir,
                sources=sources,
                previous_index=previous,
                force=arguments.force,
            )
            print(json.dumps(report, indent=2))
        return 0
    except XueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

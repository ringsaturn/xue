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
from .stac import POINT_PRODUCTS, write_point_product_documents, write_run_documents
from .zarrstore import (
    DEFAULT_INDEX_LOCATION,
    INDEX_LOCATIONS,
    container_enabled_by_environment,
    enabled_by_environment,
    export_bundle,
    store_path_for,
)
from .airport.build import build_product as build_airport_product
from .airport.build import load_previous_index as load_previous_airport_index
from .airport.fetch import fetch_round as fetch_airport_round
from .airport.schema import floor_round as floor_airport_round
from .airport.schema import parse_round as parse_airport_round
from .sounding.build import build_product as build_sounding_product
from .sounding.build import load_previous_index as load_previous_sounding_index
from .sounding.build import previous_watermarks as sounding_watermarks
from .sounding.fetch import SOURCE_IDS as SOUNDING_SOURCE_IDS
from .sounding.fetch import fetch_sources as fetch_sounding_sources
from .sounding.schema import parse_issue as parse_sounding_issue
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
        "source, the window length in hours (3 for MRMS, JMA and the CMA mosaic)",
    )
    parser.add_argument("--force", action="store_true", help=force_help)


def _model_argument(parser: argparse.ArgumentParser, *, fetched_only: bool = True) -> None:
    """The --model choice. Fetching and building are for the sources with a
    bucket to fetch from — every forecast and the three observation feeds;
    conversion also takes any source's run directory or, for a series-file
    observation, one NetCDF file."""
    choices = tuple(name for name, source in SOURCES.items() if source.fetched or not fetched_only)
    parser.add_argument(
        "--model",
        choices=choices,
        default="gfs",
        help=(
            "data source: NOAA GFS 0.25 degree (hourly), ECMWF IFS open data "
            "(3-hourly), GFS surface flux on the native ~13 km grid (hourly, adds dswrf), "
            "NOAA HRRR over the contiguous US (3 km, a cycle every hour, hourly to 18), "
            "NOAA MRMS, the radar mosaic over the contiguous US (an observation every "
            "two minutes; --run names the window's first hour and --hours its length, 3 by default), "
            "JMA, the precipitation nowcast over Japan (an observation every five minutes, "
            "fetched through the jma-radar tool; a window the same way), "
            "or cma, the CMA composite reflectivity mosaic over China (an observation every "
            "six minutes, read out of its daily Zarr archive; "
            "a window the same way"
            + ("" if fetched_only else ", or one NetCDF file the tool wrote")
            + ")"
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
        "grid, one shard per array (docs/zarr-profile.md)",
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

    stac_parser = commands.add_parser(
        "stac",
        help="rewrite a published run's or point product issue's STAC documents (docs/stac.md) from the "
        "manifest or index on disk: its item.json, the source's or product's collection.json and live "
        "item.json, and the root catalog.json",
    )
    stac_parser.add_argument("--run", help="the run whose manifest is on disk, YYYYMMDDHH")
    stac_parser.add_argument("--round", type=round_name, metavar="HHMM", help="the round of a rolling window")
    _model_argument(stac_parser)
    stac_parser.add_argument(
        "--product",
        choices=POINT_PRODUCTS,
        help="a point product instead of a run (docs/sounding.md, docs/airport.md, docs/tc.md); needs --issue",
    )
    stac_parser.add_argument(
        "--issue",
        help="the point product issue whose index.json is on disk: YYYYMMDDHH for sounding and tc, "
        "YYYYMMDDHHMM for an airport round",
    )
    stac_parser.add_argument("--output-dir", type=Path, default=Path("web/public/data"))

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
    showcase_build.add_argument(
        "--no-xue",
        dest="container",
        action="store_false",
        help="publish each bundle's Zarr store alone and retire its .xue (needs --zarr; or set XUE_CONTAINER=0)",
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

    airport_build = commands.add_parser(
        "airport-build",
        help="fetch the airport METAR and TAF caches and write one airport.<round>/ index, the shards that changed and the pointer",
    )
    airport_build.add_argument(
        "--round",
        default="now",
        help="the round, YYYYMMDDHHMM in UTC with the minute a multiple of ten, or now (this minute floored to ten)",
    )
    airport_build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    airport_build.add_argument("--output-dir", type=Path, default=Path("web/public/data"))
    airport_build.add_argument(
        "--previous-index",
        type=Path,
        help="the previous round's index.json, whose shards are this round's history (default: the one the "
        "local latest-airport.json names, if any; `make live-airport-index` fetches the live one and its shards)",
    )
    airport_build.add_argument("--offline", action="store_true", help="build from what is already fetched; touch no network")
    airport_build.add_argument("--force", action="store_true", help="rebuild a round whose index exists")
    airport_build.add_argument("--force-download", action="store_true", help="fetch every source again, station table included")

    sounding_build = commands.add_parser(
        "sounding-build",
        help="fetch the radiosonde bulletins and write one sounding.<issue>/ product directory and its pointer",
    )
    sounding_build.add_argument(
        "--issue",
        default="now",
        help="the aggregation hour, YYYYMMDDHH in UTC, or now (the current hour); one directory per hour",
    )
    sounding_build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    sounding_build.add_argument("--output-dir", type=Path, default=Path("web/public/data"))
    sounding_build.add_argument(
        "--sources",
        help=f"comma-separated subset of the gateways to build from (default: all of {','.join(SOUNDING_SOURCE_IDS)})",
    )
    sounding_build.add_argument(
        "--previous-index",
        type=Path,
        help="the previous hour's index.json, whose watermark bounds the fetch and whose station files are copied "
        "forward (default: the one the local latest-sounding.json names, if any; `make live-sounding-index` "
        "fetches the live one and its station files)",
    )
    sounding_build.add_argument(
        "--offline", action="store_true", help="build from what is already fetched; touch no network"
    )
    sounding_build.add_argument("--force", action="store_true", help="rebuild an issue whose directory exists")
    sounding_build.add_argument(
        "--force-download", action="store_true", help="list and fetch each gateway again, ignoring the watermark"
    )
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
                        container=arguments.container and container_enabled_by_environment(),
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
            if latest_path is not None:
                # A whole live run, by either encoder: the STAC face of the
                # manifest just written (docs/stac.md). A piece of a fanned-out
                # publish gets its Item from assemble-run instead.
                report["stac"] = write_run_documents(arguments.output_dir, source=source, manifest_path=manifest_path)
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
            report["stac"] = write_run_documents(
                arguments.output_dir, source=source_spec(arguments.model), manifest_path=Path(report["manifest"])
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "stac" and arguments.product is not None:
            if arguments.issue is None:
                raise XueError(f"stac --product {arguments.product} needs the issue it describes: --issue YYYYMMDDHH")
            index_path = arguments.output_dir / f"{arguments.product}.{arguments.issue}" / "index.json"
            if not index_path.is_file():
                raise XueError(f"no index at {index_path}")
            print(
                json.dumps(
                    write_point_product_documents(
                        arguments.output_dir, product=arguments.product, index_path=index_path
                    ),
                    indent=2,
                )
            )
        elif arguments.command == "stac":
            if arguments.run is None:
                raise XueError("stac takes a run (--model/--run) or a point product issue (--product/--issue)")
            source = source_spec(arguments.model)
            run = parse_run(arguments.run, arguments.model)
            run_directory = arguments.output_dir / f"{source.id}.{run.id}"
            if arguments.round is not None:
                run_directory = run_directory / arguments.round
            manifest_path = run_directory / "manifest.json"
            if not manifest_path.is_file():
                raise XueError(f"no manifest at {manifest_path}")
            print(json.dumps(write_run_documents(arguments.output_dir, source=source, manifest_path=manifest_path), indent=2))
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
        elif arguments.command == "airport-build":
            if arguments.round == "now":
                moment = floor_airport_round(datetime.now(UTC))
            else:
                moment = parse_airport_round(arguments.round)
            if not arguments.offline:
                fetch_airport_round(arguments.raw_dir, moment, force=arguments.force_download)
            previous = load_previous_airport_index(arguments.previous_index, arguments.output_dir)
            report = build_airport_product(
                moment,
                arguments.raw_dir,
                arguments.output_dir,
                previous_index=previous,
                force=arguments.force,
            )
            print(json.dumps(report, indent=2))
        elif arguments.command == "sounding-build":
            if arguments.issue == "now":
                issue = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            else:
                issue = parse_sounding_issue(arguments.issue)
            sources = SOUNDING_SOURCE_IDS
            if arguments.sources:
                sources = tuple(dict.fromkeys(item.strip() for item in arguments.sources.split(",") if item.strip()))
                unknown = [item for item in sources if item not in SOUNDING_SOURCE_IDS]
                if unknown:
                    raise XueError(
                        f"unknown sounding sources {unknown}; choose from {', '.join(SOUNDING_SOURCE_IDS)}"
                    )
            previous = load_previous_sounding_index(arguments.previous_index, arguments.output_dir)
            if not arguments.offline:
                fetch_sounding_sources(
                    arguments.raw_dir,
                    issue,
                    sources,
                    watermarks=None if arguments.force_download else sounding_watermarks(previous),
                    force=arguments.force_download,
                )
            report = build_sounding_product(
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

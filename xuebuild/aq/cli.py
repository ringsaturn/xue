"""``xue aq-build``: the command's arguments and its run, kept beside the
product so the top-level CLI only registers and dispatches it."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .build import build_product, load_previous_index
from .fetch import fetch_round
from .networks import NETWORKS
from .schema import AqProductError, floor_round, parse_round

COMMAND = "aq-build"


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        COMMAND,
        help="fetch the air-quality station networks and write one aq.<round>/ index, a <network>.jsonl and a "
        "<network>.stations.json per network, and the pointer (docs/aq.md)",
    )
    parser.add_argument(
        "--round",
        default="now",
        help="the round, YYYYMMDDHHMM in UTC with the minute a multiple of five, or now (this minute floored to five)",
    )
    parser.add_argument(
        "--networks",
        help=f"comma-separated networks to fetch (default: all of {', '.join(network.id for network in NETWORKS)}); "
        "the others carry their history over",
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("web/public/data"))
    parser.add_argument(
        "--previous-index",
        type=Path,
        help="the previous round's index.json, whose network files this round merges onto (default: the one the "
        "local latest-aq.json names, if any; `make live-aq-index` fetches the live one and its files)",
    )
    parser.add_argument("--offline", action="store_true", help="build from what is already fetched; touch no network")
    parser.add_argument("--force", action="store_true", help="rebuild a round whose index exists")
    parser.add_argument("--force-download", action="store_true", help="fetch every source again")


def run(arguments: argparse.Namespace) -> None:
    moment = floor_round(datetime.now(UTC)) if arguments.round == "now" else parse_round(arguments.round)
    networks = None
    if arguments.networks:
        known = tuple(network.id for network in NETWORKS)
        networks = tuple(dict.fromkeys(item.strip() for item in arguments.networks.split(",") if item.strip()))
        unknown = [item for item in networks if item not in known]
        if unknown:
            raise AqProductError(f"unknown aq networks {unknown}; choose from {', '.join(known)}")
    previous = load_previous_index(arguments.previous_index, arguments.output_dir)
    if not arguments.offline:
        fetch_round(arguments.raw_dir, moment, previous_index=previous, networks=networks, force=arguments.force_download)
    report = build_product(moment, arguments.raw_dir, arguments.output_dir, previous_index=previous, force=arguments.force)
    print(json.dumps(report, indent=2, ensure_ascii=False))

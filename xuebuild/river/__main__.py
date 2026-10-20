"""``python -m xuebuild.river build <definition.json>``: write one case's
``river/`` directory (``docs/river.md``) from the work directory its
definition names."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ..errors import XueError
from .build import build, load_definition


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xuebuild.river")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="write a case's river/ directory from its definition")
    build_parser.add_argument("definition", type=Path, help="showcase/river-cases/<id>.json")
    build_parser.add_argument("--work", type=Path, help="the work directory, in place of the definition's")
    build_parser.add_argument("--output", type=Path, help="the river/ directory to write, in place of the definition's")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        index = build(load_definition(arguments.definition, work=arguments.work, output=arguments.output))
    except XueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: index[key] for key in index if key != "gauges"}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

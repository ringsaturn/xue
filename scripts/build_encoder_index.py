#!/usr/bin/env python3
"""Build a PEP 503 simple index over the encoder wheels attached to GitHub releases.

The experimental encoder is not published to PyPI: it carries a bundled GDAL,
it is pinned to the versions its byte-identity comparison was run against, and
it is a research artifact rather than a library anyone should depend on. But it
still has to be installable, so the wheels ride on GitHub release assets and
this writes the index that points at them — the arrangement ringsaturn/tzfpy
uses for its own overflow wheels.

The output is a static tree for GitHub Pages; it contains links, never wheels.

    python scripts/build_encoder_index.py --repository ringsaturn/xue --output site

Set GITHUB_TOKEN to raise the API rate limit (the workflow passes the job's
token). Nothing here needs write access.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PACKAGE = "xue-encode-py"
# Encoder releases are tagged apart from the crate's own v* tags, which the
# release-crate workflow owns.
TAG_PREFIX = "encoder-v"
API = "https://api.github.com"


def normalize(name: str) -> str:
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def request_json(url: str, token: str | None) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    request.add_header("User-Agent", "xue-encoder-index")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"error: GitHub API {error.code} for {url}: {detail}") from error


def releases(repository: str, token: str | None) -> list[dict]:
    found: list[dict] = []
    page = 1
    while True:
        batch = request_json(f"{API}/repos/{repository}/releases?per_page=100&page={page}", token)
        if not isinstance(batch, list) or not batch:
            return found
        found.extend(batch)
        if len(batch) < 100:
            return found
        page += 1


def wheels(repository: str, token: str | None) -> list[tuple[str, str, str]]:
    """Every wheel asset of every encoder release, as (filename, url, sha256)."""
    collected: dict[str, tuple[str, str, str]] = {}
    for release in releases(repository, token):
        if not str(release.get("tag_name", "")).startswith(TAG_PREFIX):
            continue
        if release.get("draft"):
            continue
        for asset in release.get("assets", []):
            name = str(asset.get("name", ""))
            if not name.endswith(".whl"):
                continue
            digest = str(asset.get("digest") or "")
            sha256 = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
            # A filename identifies a wheel completely; if two releases carry
            # the same one, the newer release wins.
            collected[name] = (name, str(asset.get("browser_download_url", "")), sha256)
    return sorted(collected.values())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "ringsaturn/xue"))
    parser.add_argument("--output", type=Path, default=Path("site"))
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="write an empty index instead of failing when no release carries a wheel yet",
    )
    arguments = parser.parse_args(argv)

    found = wheels(arguments.repository, os.environ.get("GITHUB_TOKEN"))
    if not found and not arguments.allow_empty:
        print(
            f"error: no {TAG_PREFIX}* release of {arguments.repository} carries a wheel",
            file=sys.stderr,
        )
        return 1

    project = normalize(PACKAGE)
    links = "\n".join(
        f'    <a href="{html.escape(url)}{f"#sha256={sha256}" if sha256 else ""}">'
        f"{html.escape(name)}</a><br>"
        for name, url, sha256 in found
    )
    write(
        arguments.output / "simple" / project / "index.html",
        f"""<!DOCTYPE html>
<html>
  <head><meta name="pypi:repository-version" content="1.0"><title>Links for {project}</title></head>
  <body>
    <h1>Links for {project}</h1>
{links}
  </body>
</html>
""",
    )
    write(
        arguments.output / "simple" / "index.html",
        f"""<!DOCTYPE html>
<html>
  <head><meta name="pypi:repository-version" content="1.0"><title>Simple index</title></head>
  <body>
    <a href="{project}/">{project}</a><br>
  </body>
</html>
""",
    )
    print(f"wrote {arguments.output}/simple/ with {len(found)} wheel(s)")
    for name, _url, sha256 in found:
        print(f"  {name}{'' if sha256 else '  (no digest published)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

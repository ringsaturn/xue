"""The data root's landing page, ``index.html``, beside ``catalog.json``.

Not a STAC object: the page a person lands on when they open the data
root in a browser, naming the catalog, the repository's documents that are
its contract, and every Collection the catalog lists with its licence.
Derived like the catalog — a pure function of the registry and the prose
in ``stac.py`` (``root_catalog`` for the children, ``_source_prose`` /
``_point_product_prose`` for the terms), no timestamps — so every publish
rewrites the same bytes, and a source added to the registry appears here
with its Collection. What does change between publishes, the live run and
its extent, is read out of each Collection in the browser on the same
origin (``stacindex.html``'s script), never written into the page.

The template is ``stacindex.html``, a ``string.Template`` (``$$`` where
the JavaScript needs a dollar); the rows are the only markup built here.
"""

from __future__ import annotations

from html import escape
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

from . import stac

INDEX_HTML_FILENAME = "index.html"

# Where the site and its data live, as `web/src/site.ts` states them for the
# frontend; `tests/test_stac.py` holds the two in agreement. The catalog's
# own documents name no host (docs/stac.md), and the page names these two
# only where a reader needs an absolute address: the snippet to paste and
# the links back to the viewer and the code.
SITE_ORIGIN = "https://xue.ringsaturn.me"
REPO_URL = "https://github.com/ringsaturn/xue"
DATA_ORIGIN = "https://dataset.ringsaturn.me/xue/"
DOCS_URL = f"{REPO_URL}/blob/main/docs"


def _licence_text(prose: dict[str, Any]) -> str:
    """One Collection's terms as the page states them: the SPDX id when the
    prose names one, else each ``license`` link's title, linked."""
    links = [link for link in prose["links"] if link["rel"] == "license"]
    if prose["license"] != "other":
        href = links[0]["href"] if links else None
        label = escape(prose["license"])
        return f'<a href="{escape(href, quote=True)}">{label}</a>' if href else label
    if not links:
        return "the producer's own terms; see the Collection's providers"
    return "; ".join(
        f'<a href="{escape(link["href"], quote=True)}">{escape(link["title"])}</a>' for link in links
    )


def _collections() -> list[tuple[str, str, dict[str, Any] | None]]:
    """``(directory, title, prose)`` per child of the root catalog, in its
    order; the showcase has no prose of its own."""
    rows: list[tuple[str, str, dict[str, Any] | None]] = []
    for link in stac.root_catalog()["links"]:
        if link["rel"] != "child":
            continue
        directory = link["href"].removesuffix(f"/{stac.COLLECTION_FILENAME}")
        if directory == stac.SHOWCASE_COLLECTION_ID:
            prose = None
        elif directory in stac.POINT_PRODUCTS:
            prose = stac._point_product_prose(directory)
        else:
            prose = stac._source_prose(stac.source_spec(directory))
        rows.append((directory, link["title"], prose))
    return rows


def render_index() -> str:
    """The page, as one string; a pure function of the registry."""
    collections = _collections()
    collection_rows = "\n".join(
        f'      <tr data-dir="{escape(directory, quote=True)}">'
        f'<td><a href="{escape(directory, quote=True)}/{stac.COLLECTION_FILENAME}">{escape(title)}</a></td>'
        f"<td></td><td></td>"
        f'<td class="links">{"" if prose is not None else '<a href="showcase.json">showcase.json</a>'}</td></tr>'
        for directory, title, prose in collections
    )
    licence_rows = "\n".join(
        f"    <dt>{escape(title)}</dt><dd>{_licence_text(prose)}</dd>"
        for _directory, title, prose in collections
        if prose is not None
    )
    template = Template((files(__package__) / "stacindex.html").read_text(encoding="utf-8"))
    return template.substitute(
        site_origin=SITE_ORIGIN,
        repo_url=REPO_URL,
        repo_host=REPO_URL.removeprefix("https://"),
        docs_url=DOCS_URL,
        data_origin=DATA_ORIGIN,
        data_host=DATA_ORIGIN.removeprefix("https://"),
        stac_version=stac.STAC_VERSION,
        catalog_filename=stac.CATALOG_FILENAME,
        collection_rows=collection_rows,
        licence_rows=licence_rows,
    )


def write_index(output_dir: Path) -> Path:
    """Write ``index.html`` at ``output_dir`` (the data root) beside the
    catalog, unchanged bytes left untouched; returns its path."""
    path = output_dir / INDEX_HTML_FILENAME
    rendered = render_index()
    if not (path.exists() and path.read_text(encoding="utf-8") == rendered):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".html.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    return path

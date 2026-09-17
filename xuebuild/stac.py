"""A SpatioTemporal Asset Catalog face over what the encoder publishes.

Every JSON the pipeline writes today — a run's ``manifest.json``, the
per-model live pointer, ``showcase.json`` — keeps its shape and its readers;
this module *derives* a static STAC catalog beside them (``docs/stac.md``)
so the Zarr stores can be found the way the rest of that ecosystem finds
data: ``pystac`` / ``xpystac`` / ``odc-stac`` open an Item's
``application/vnd.zarr`` asset straight into xarray.

The layout, at the data root:

- ``catalog.json`` — the root **Catalog**, one child link per live source
  and one for the showcase.
- ``<source>/collection.json`` — one **Collection** per source, mutable
  like the pointer it mirrors: its ``item`` / ``latest-version`` links name
  the run the pointer names. Rewritten by every publish, uploaded with the
  pointer.
- ``<source>/item.json`` — the **live Item**: the run Item below, relocated
  to a path that never changes. Only the newest run is kept on the bucket
  (an hour for HRRR, minutes for an MRMS round), so a link into
  ``<source>.<run>/`` dies with the run; this is the URL a Collection
  links and a client bookmarks, and it always resolves.
- ``<source>.<run>/item.json`` (a rolling window's
  ``<source>.<run>/<HHMM>/item.json``) — one **Item** per published run,
  beside its manifest and derived from it alone: one asset per artifact
  (store, container, half tier, poster, video), the manifest itself as a
  ``metadata`` asset under its ``?v=``, the grid as ``bbox`` /
  ``cube:dimensions``, the variables as ``cube:variables``, the cycle as
  ``forecast:reference_datetime``.
- ``<product>/collection.json``, ``<product>/item.json`` and
  ``<product>.<issue>/item.json`` — the same three documents for each of
  the point products published beside the runs (``sounding``, ``airport``,
  ``tc``), derived from the issue's ``index.json`` alone: where the
  stations are, what period the issue covers, and one asset per file it
  ships.
- ``showcase/collection.json`` and ``showcase/<case>/item.json`` — the
  cases, from ``showcase.json``'s rows and their manifests.

Everything here is a pure function of the manifest, the catalog row and the
source registry — no timestamps, no host names — so a run built whole and a
run built in pieces derive the same documents (``tests/test_assemble.py``)
and a document can be regenerated at any time from what is on disk. Links
are relative, which is what a static catalog on a bucket wants: a client
resolves them against the URL it read the document from, whichever origin
serves the data.

What a manifest does not carry the Item does not claim. The manifest names
no grid; it is read off the poster (or video) metadata a run's core scalars
always carry, and a poster is the grid decimated two to one
(``GridInfo.decimated``: same origin, doubled step), so the full step is
exact and a regional grid's far edge is right to within one cell — the
authoritative grid is the store's own ``attributes.xue``. A manifest with no
such metadata (the synthetic ones in tests) gets a null geometry and no
spatial dimensions, which STAC allows.

Field names outside the core spec come from published extensions — forecast
v0.2.0, datacube v2.3.0, file v2.1.0 — and everything Xue-specific sits
under the ``xue:`` prefix, undeclared, as STAC custom fields are.
"""

from __future__ import annotations

import json
import posixpath
import re
import zlib
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from .binconvert import VECTOR_BUNDLES
from .errors import XueError
from .manifest import iso_z
from .sources import SOURCES, SourceSpec, source_spec
from .variables import VARIABLES

STAC_VERSION = "1.1.0"
CATALOG_ID = "xue"
CATALOG_FILENAME = "catalog.json"
COLLECTION_FILENAME = "collection.json"
ITEM_FILENAME = "item.json"
INDEX_FILENAME = "index.json"
SHOWCASE_COLLECTION_ID = "showcase"

FORECAST_EXTENSION = "https://stac-extensions.github.io/forecast/v0.2.0/schema.json"
DATACUBE_EXTENSION = "https://stac-extensions.github.io/datacube/v2.3.0/schema.json"
FILE_EXTENSION = "https://stac-extensions.github.io/file/v2.1.0/schema.json"

# `application/vnd.zarr` is the media type pystac 1.14 registers for a Zarr
# store (`MediaType.VND_ZARR`, stac-utils/pystac#1554); the older
# `application/vnd+zarr` spelling is what earlier catalogs carry. The
# container has no registered type. The video companion is a raw H.264
# Annex B stream, which is what IANA's `video/H264` names.
ZARR_MEDIA_TYPE = "application/vnd.zarr"
CONTAINER_MEDIA_TYPE = "application/octet-stream"
POSTER_MEDIA_TYPE = "application/octet-stream"
VIDEO_MEDIA_TYPE = "video/H264"
JSON_MEDIA_TYPE = "application/json"
STAC_JSON_MEDIA_TYPE = "application/json"
GEOJSON_MEDIA_TYPE = "application/geo+json"

# `file:checksum` is a multihash: the multicodec table assigns CRC-32 the
# code 0x0132 (IEEE 802.3, the CRC every manifest field already carries), so
# the manifest's eight hex digits are a valid self-describing hash once the
# code (as an unsigned varint, `b2 02`) and the digest length (`04`) are put
# in front. The same value appears bare as `xue:crc32`, the artifact's `?v=`.
CRC32_MULTIHASH_PREFIX = "b20204"

_CRC32_PATTERN = re.compile(r"^[0-9a-f]{8}$")


class StacError(XueError):
    """A derived STAC document violates its own contract."""


# What each source is called in a catalog, under which terms its data is
# published, and by whom. The shell's own copy of this is the sources sheet
# (`creditsGfs` and its siblings in `web/src/locales/en.ts`); STAC wants it
# as a `license` (an SPDX id, or `other` with a link) and a `providers`
# list. NOAA data is in the public domain — SPDX has no identifier for that
# beyond CC0, which NOAA does not use, so it is `other` with the open data
# policy linked. ECMWF open data is CC BY 4.0 and what is served is a
# converted derivative, which the attribution says.
_NOAA_PROVIDER = {
    "name": "NOAA / NCEP",
    "roles": ["producer", "licensor"],
    "url": "https://www.ncei.noaa.gov/",
}
_NOAA_LICENSE_LINK = {
    "rel": "license",
    "href": "https://www.noaa.gov/information-technology/open-data-dissemination",
    "type": "text/html",
    "title": "NOAA open data dissemination",
}
_XUE_PROVIDER = {
    "name": "Xue",
    "roles": ["processor", "host"],
    "url": "https://github.com/ringsaturn/xue",
}


# The point products published beside the runs (`docs/sounding.md`,
# `docs/airport.md`, `docs/tc.md`): a fixed list rather than a registry,
# since each is its own pipeline under one delivery contract
# (`xuebuild/pointproduct.py`). They are the root catalog's other children.
POINT_PRODUCTS = ("sounding", "airport", "tc")

NDJSON_MEDIA_TYPE = "application/x-ndjson"


def _point_product_prose(product: str) -> dict[str, Any]:
    """`_source_prose` for the three point products.

    None of the three has an SPDX identifier to name. The soundings are
    WMO core data under the Unified Data Policy (Resolution 1,
    Cg-Ext(2021)) — free and unrestricted with attribution of the original
    source requested, which no SPDX id spells — so the license is `other`
    with the resolution linked. The airport reports are decoded by a work
    of the United States government, which is in the public domain rather
    than under CC0 (`CC0-1.0` would claim a waiver nobody granted), so
    that too is `other`, with the service's own terms linked. The tracks
    come from several centres at once — US government works, ECMWF open
    data under CC BY 4.0, IBTrACS — so the Collection is `other` and the
    links name each.
    """
    prose: dict[str, dict[str, Any]] = {
        "sounding": {
            "title": "Radiosonde soundings (WIS2)",
            "description": (
                "The world's radiosonde ascents, aggregated every hour from the TEMP bulletins the "
                "national meteorological services exchange over the WMO Information System: one station "
                "per line of a single NDJSON file, spanned by the index beside it, each ascent thinned to "
                "the classical mandatory and significant levels in fixed point with four derived "
                "quantities. A rolling window with no fixed start — only the newest issue is published, "
                "and a station carries its newest four nominal times."
            ),
            "license": "other",
            "keywords": ["weather", "observation", "radiosonde", "sounding", "upper air", "wis2"],
            "providers": [
                {
                    "name": "WMO WIS2 Global Cache",
                    "description": (
                        "The GTS-to-WIS2 gateways run by the Japan Meteorological Agency and the "
                        "Deutscher Wetterdienst, which republish every centre's bulletins."
                    ),
                    "roles": ["producer"],
                    "url": "https://wis2.wmo.int/",
                },
                {
                    "name": "The originating national meteorological and hydrological services",
                    "description": "Every ascent is theirs; attribution of the original source is requested.",
                    "roles": ["producer", "licensor"],
                    "url": "https://community.wmo.int/en/members",
                },
                _XUE_PROVIDER,
            ],
            "links": [
                {
                    "rel": "license",
                    "href": "https://library.wmo.int/idurl/4/58009",
                    "type": "text/html",
                    "title": (
                        "WMO Unified Data Policy (Resolution 1, Cg-Ext(2021)): core data, free and "
                        "unrestricted, attribution of the original source requested"
                    ),
                }
            ],
        },
        "airport": {
            "title": "Airport observations and forecasts (METAR / TAF)",
            "description": (
                "The world's airport weather, aggregated every ten minutes from the NOAA Aviation "
                "Weather Center's decoded caches: about five thousand stations with their newest "
                "observation in the index, and each station's last 24 hours of METARs with its current "
                "TAF on one line of the history file beside it. A rolling window with no fixed start — "
                "only the newest round is published, and each round carries the whole window."
            ),
            "license": "other",
            "keywords": ["weather", "observation", "aviation", "metar", "taf", "airport"],
            "providers": [
                {
                    "name": "NOAA / NWS Aviation Weather Center",
                    "description": (
                        "The decoded METAR and TAF cache files, a work of the United States government "
                        "and in the public domain."
                    ),
                    "roles": ["producer", "licensor"],
                    "url": "https://aviationweather.gov/data/cache/",
                },
                {
                    "name": "The world's meteorological services",
                    "description": "The reports themselves, exchanged under WMO and ICAO arrangements.",
                    "roles": ["producer"],
                    "url": "https://community.wmo.int/en/members",
                },
                _XUE_PROVIDER,
            ],
            "links": [
                {
                    "rel": "license",
                    "href": "https://www.weather.gov/disclaimer",
                    "type": "text/html",
                    "title": "NWS disclaimer: a work of the US government, in the public domain",
                },
                {
                    "rel": "about",
                    "href": "https://aviationweather.gov/data/api/",
                    "type": "text/html",
                    "title": "Aviation Weather Center data services",
                },
            ],
        },
        "tc": {
            "title": "Tropical cyclone tracks",
            "description": (
                "Every tropical cyclone the warning centres and the models are tracking, aggregated "
                "every hour: the official forecasts of the National Hurricane Center and the Joint "
                "Typhoon Warning Center, the multi-agency best tracks, the NCEP tracker's GFS and GEFS "
                "tracks and ECMWF's deterministic and ensemble tracks — one JSON file per system beside "
                "an index that names the systems and their headline positions. A rolling window with no "
                "fixed start: only the newest issue is published."
            ),
            "license": "other",
            "keywords": ["weather", "forecast", "tropical cyclone", "hurricane", "typhoon", "track"],
            "providers": [
                {
                    "name": "NOAA / NWS National Hurricane Center",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.nhc.noaa.gov/",
                },
                {
                    "name": "Joint Typhoon Warning Center",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.metoc.navy.mil/jtwc/jtwc.html",
                },
                {
                    "name": "NOAA / NCEP",
                    "description": "The objective tracker's GFS and GEFS tracks.",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.nco.ncep.noaa.gov/pmb/products/hur/",
                },
                {
                    "name": "ECMWF",
                    "description": "The open data deterministic and ensemble cyclone tracks, CC BY 4.0.",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
                },
                {
                    "name": "NOAA NCEI / IBTrACS",
                    "description": "The international best track archive for climate stewardship.",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.ncei.noaa.gov/products/international-best-track-archive",
                },
                _XUE_PROVIDER,
            ],
            "links": [
                {
                    "rel": "license",
                    "href": "https://www.weather.gov/disclaimer",
                    "type": "text/html",
                    "title": (
                        "NWS disclaimer: the US agencies' products are works of the US government, "
                        "in the public domain"
                    ),
                },
                {
                    "rel": "license",
                    "href": "https://creativecommons.org/licenses/by/4.0/",
                    "type": "text/html",
                    "title": "Creative Commons Attribution 4.0, for the ECMWF open data tracks",
                },
            ],
        },
    }
    try:
        return prose[product]
    except KeyError as exc:  # pragma: no cover - the table is held to POINT_PRODUCTS by a test
        raise StacError(f"no catalog prose for point product {product}") from exc


def _source_prose(source: SourceSpec) -> dict[str, Any]:
    prose: dict[str, dict[str, Any]] = {
        "gfs": {
            "title": "NOAA GFS 0.25°",
            "description": (
                "The NOAA Global Forecast System at 0.25° (pgrb2.0p25), hourly to 120 hours and "
                "three-hourly to 240, with the GFS-Wave fields of the same cycle. Four cycles a day; "
                "only the newest is kept."
            ),
            "license": "other",
            "providers": [_NOAA_PROVIDER, _XUE_PROVIDER],
            "links": [_NOAA_LICENSE_LINK],
        },
        "sflux": {
            "title": "NOAA GFS surface fluxes",
            "description": (
                "The GFS surface flux files (sfluxgrb) on the model's native T1534 Gaussian grid: "
                "the surface fields and the downward shortwave radiation, hourly to 120 hours and "
                "three-hourly to 240."
            ),
            "license": "other",
            "providers": [_NOAA_PROVIDER, _XUE_PROVIDER],
            "links": [_NOAA_LICENSE_LINK],
        },
        "ecmwf": {
            "title": "ECMWF IFS open data 0.25°",
            "description": (
                "The ECMWF Integrated Forecasting System open data at 0.25°, three-hourly to 144 hours "
                "and six-hourly to 240, with the wave stream of the same cycle. Contains modified "
                "ECMWF open data; the published bundles are a quantized derivative."
            ),
            "license": "CC-BY-4.0",
            "providers": [
                {
                    "name": "ECMWF",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
                },
                _XUE_PROVIDER,
            ],
            "links": [
                {
                    "rel": "license",
                    "href": "https://creativecommons.org/licenses/by/4.0/",
                    "type": "text/html",
                    "title": "Creative Commons Attribution 4.0",
                }
            ],
        },
        "aifs": {
            "title": "ECMWF AIFS Single open data 0.25°",
            "description": (
                "The ECMWF Artificial Intelligence Forecasting System, its deterministic AIFS Single "
                "open data at 0.25°, six-hourly to 360 hours from every cycle, with the wave stream of "
                "the same cycle. Contains modified ECMWF open data; the published bundles are a "
                "quantized derivative."
            ),
            "license": "CC-BY-4.0",
            "providers": [
                {
                    "name": "ECMWF",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
                },
                _XUE_PROVIDER,
            ],
            "links": [
                {
                    "rel": "license",
                    "href": "https://creativecommons.org/licenses/by/4.0/",
                    "type": "text/html",
                    "title": "Creative Commons Attribution 4.0",
                }
            ],
        },
        "hrrr": {
            "title": "NOAA HRRR 3 km",
            "description": (
                "The NOAA High-Resolution Rapid Refresh over the contiguous United States, resampled "
                "from its 3 km Lambert conformal grid onto a regular 0.03° one, hourly to 18 hours, "
                "a new cycle every hour."
            ),
            "license": "other",
            "providers": [_NOAA_PROVIDER, _XUE_PROVIDER],
            "links": [_NOAA_LICENSE_LINK],
        },
        "mrms": {
            "title": "NOAA MRMS radar mosaic",
            "description": (
                "The NOAA Multi-Radar/Multi-Sensor composite reflectivity and precipitation rate over "
                "the contiguous United States, observed every two minutes and published as a rolling "
                "window rebuilt every five minutes, thinned to 0.02° by block maximum."
            ),
            "license": "other",
            "providers": [_NOAA_PROVIDER, _XUE_PROVIDER],
            "links": [_NOAA_LICENSE_LINK],
        },
        "jma": {
            "title": "JMA precipitation nowcast (hrpns)",
            "description": (
                "The Japan Meteorological Agency's high-resolution precipitation nowcast analysis "
                "(高解像度降水ナウキャスト) over Japan: precipitation intensity classes every five "
                "minutes, decoded from the agency's map tiles onto a regular 0.005° grid by the "
                "strongest class in each cell and published under prate at each class's "
                "representative rate (0.5, 3, 7.5, 15, 25, 40, 65 and 100 mm/h), as a rolling "
                "window rebuilt every five minutes. Source: Japan Meteorological Agency website "
                "(出典：気象庁ホームページ), regridded and reclassified by jma-radar."
            ),
            "license": "other",
            "providers": [
                {
                    "name": "Japan Meteorological Agency",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.jma.go.jp/bosai/en_nowc/",
                },
                _XUE_PROVIDER,
            ],
            "links": [
                {
                    "rel": "license",
                    "href": "https://www.jma.go.jp/jma/kishou/info/coment.html",
                    "type": "text/html",
                    "title": "気象庁ホームページ利用規約 (compatible with CC BY 4.0)",
                }
            ],
        },
        "cma": {
            "title": "CMA radar mosaic",
            "description": (
                "The China Meteorological Administration level-3 composite reflectivity mosaic "
                "(RADAR_L3_MST_CREF) over China: a national composite every six minutes, decoded "
                "from the agency's data portal tiles onto the portal's plate carrée grid at "
                "0.0439° (1792 × 1024 cells, 67.5°E–146.25°E, 11.25°N–56.25°N) and published "
                "under cref, as a rolling window rebuilt every few minutes. Source: China "
                "Meteorological Administration, National Meteorological Centre (www.nmc.cn)."
            ),
            "license": "other",
            "providers": [
                {
                    "name": "China Meteorological Administration",
                    "roles": ["producer", "licensor"],
                    "url": "https://www.nmc.cn",
                },
                _XUE_PROVIDER,
            ],
            "links": [],
        },
    }
    try:
        return prose[source.id]
    except KeyError as exc:  # pragma: no cover - the table is held to the registry by a test
        raise StacError(f"no catalog prose for source {source.id}") from exc


def prose_document() -> dict[str, Any]:
    """Every title, licence, provider and licence link the catalog
    publishes, as ``tests/fixtures/stac-prose.json`` pins it — the way
    ``tc-registry.json`` pins the agency table. What a Collection says about
    who owns the data and under which terms is not something to change by
    accident, so it is committed rather than only asserted about.

    Regenerate it with the change that meant it::

        .venv/bin/python -c "import json; from xuebuild import stac; \\
            print(json.dumps(stac.prose_document(), indent=2, ensure_ascii=False))" \\
            > tests/fixtures/stac-prose.json
    """
    return {
        "sources": {source.id: _source_prose(source) for source in SOURCES.values()},
        "pointProducts": {product: _point_product_prose(product) for product in POINT_PRODUCTS},
    }


# ---------------------------------------------------------------------------
# Reading a manifest


def _crc32_of(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso_duration(seconds: int) -> str:
    """An ISO 8601 duration in whole hours, minutes and seconds — ``PT240H``,
    ``PT6M``, ``PT2M`` — never days, so a lead time reads as the forecast
    hour it is."""
    if seconds < 0:
        raise StacError("a duration cannot be negative")
    hours, remainder = divmod(seconds, 3600)
    minutes, remainder = divmod(remainder, 60)
    parts = "".join(f"{count}{unit}" for count, unit in ((hours, "H"), (minutes, "M"), (remainder, "S")) if count)
    return f"PT{parts or '0S'}"


def _metadata_blocks(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every bundle metadata JSON the manifest carries, tagged with where it
    came from: ``video`` describes the full grid, ``poster`` the decimated
    one. Videos first, so a grid is read exact wherever one exists."""
    blocks: list[tuple[str, dict[str, Any]]] = []
    for kind in ("video", "poster"):
        for bundle in manifest["bundles"]:
            descriptor = bundle.get(kind)
            if descriptor is not None:
                blocks.append((kind, json.loads(descriptor["metadataJson"])))
    return blocks


def _grid_of(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The published grid, as ``{first_longitude, first_latitude,
    longitude_step, latitude_step, width, height, wraps}`` at full
    resolution, or None when the manifest carries no metadata to read one
    from. Off a poster the dimensions are the decimated ones and the steps
    are halved back; the extent is then computed from the decimated cell
    centers, which is the origin exactly and the far edge to within one
    full cell."""
    for kind, metadata in _metadata_blocks(manifest):
        grid = metadata["grid"]
        divisor = 2 if kind == "poster" else 1
        return {
            "first_longitude": float(grid["firstLongitude"]),
            "first_latitude": float(grid["firstLatitude"]),
            "longitude_step": float(grid["longitudeStep"]) / divisor,
            "latitude_step": float(grid["latitudeStep"]) / divisor,
            # The cell centers the extent is read off: the block's own.
            "width": int(grid["width"]),
            "height": int(grid["height"]),
            "center_longitude_step": float(grid["longitudeStep"]),
            "center_latitude_step": float(grid["latitudeStep"]),
            "wraps": bool(grid.get("wrapLongitude", False)),
        }
    return None


def _extent_of(grid: dict[str, Any]) -> list[float]:
    """``[west, south, east, north]`` over cell centers, the way
    ``showcase._grid_extent`` reads a cropped grid; a wrapping grid is the
    whole circle. A regional window across the antimeridian keeps its own
    frame (east < west), the GeoJSON convention for a bbox there."""
    west = grid["first_longitude"]
    north = grid["first_latitude"]
    south = north + (grid["height"] - 1) * grid["center_latitude_step"]
    if grid["wraps"]:
        west, east = -180.0, 180.0
    else:
        east = west + (grid["width"] - 1) * grid["center_longitude_step"]
        east = (east + 180.0) % 360.0 - 180.0
        west = (west + 180.0) % 360.0 - 180.0
    return [round(west, 6), round(min(south, north), 6), round(east, 6), round(max(south, north), 6)]


def _bbox_geometry(bbox: list[float]) -> dict[str, Any]:
    """A GeoJSON geometry for a bbox: one polygon, or two when the box
    crosses the antimeridian (west > east), as the STAC spec asks."""
    west, south, east, north = bbox

    def ring(w: float, e: float) -> list[list[float]]:
        return [[w, south], [e, south], [e, north], [w, north], [w, south]]

    if west <= east:
        return {"type": "Polygon", "coordinates": [ring(west, east)]}
    return {"type": "MultiPolygon", "coordinates": [[ring(west, 180.0)], [ring(-180.0, east)]]}


def _axis_seconds(time: dict[str, Any]) -> list[int]:
    unit = int(time["unitSeconds"])
    if "frameOffsets" in time:
        return [int(offset) * unit for offset in time["frameOffsets"]]
    first = int(time["firstFrameOffset"])
    step = int(time["frameStep"])
    return [(first + index * step) * unit for index in range(int(time["frameCount"]))]


def _time_axis_of(manifest: dict[str, Any]) -> tuple[list[int], bool]:
    """The union of every axis the manifest's metadata carries, in seconds
    from the run time, and whether it was read from metadata at all (else it
    is the whole declared ``forecastHours`` span, hour by hour, which is
    what a manifest with no metadata can say)."""
    leads: set[int] = set()
    for _, metadata in _metadata_blocks(manifest):
        leads.update(_axis_seconds(metadata["time"]))
    if leads:
        return sorted(leads), True
    return [hour * 3600 for hour in range(int(manifest["forecastHours"]) + 1)], False


def _temporal_dimension(run_time: datetime, leads: list[int]) -> dict[str, Any]:
    steps = {b - a for a, b in pairwise(leads)}
    return {
        "type": "temporal",
        "extent": [iso_z(run_time + timedelta(seconds=leads[0])), iso_z(run_time + timedelta(seconds=leads[-1]))],
        "step": _iso_duration(next(iter(steps))) if len(steps) == 1 else None,
    }


def _spatial_dimensions(grid: dict[str, Any], bbox: list[float]) -> dict[str, Any]:
    return {
        "x": {
            "type": "spatial",
            "axis": "x",
            "extent": [bbox[0], bbox[2]],
            "step": abs(grid["longitude_step"]),
            "reference_system": 4326,
        },
        "y": {
            "type": "spatial",
            "axis": "y",
            "extent": [bbox[1], bbox[3]],
            "step": abs(grid["latitude_step"]),
            "reference_system": 4326,
        },
    }


def _variables_of(manifest: dict[str, Any], dimensions: list[str]) -> dict[str, Any]:
    """One ``cube:variables`` entry per array the run publishes: a scalar
    bundle is one, a vector bundle its two components (the arrays its store
    holds). The label and unit come from the registry; a bundle the
    registry does not know — a manifest admits any well-formed name — is
    listed by name alone."""
    variables: dict[str, Any] = {}
    for bundle in manifest["bundles"]:
        bundle_id = bundle["variable"]
        for variable_id in VECTOR_BUNDLES.get(bundle_id, (bundle_id,)):
            entry: dict[str, Any] = {"dimensions": dimensions, "type": "data"}
            spec = VARIABLES.get(variable_id)
            if spec is not None:
                entry["description"] = spec.label
                entry["unit"] = spec.output_unit
            if variable_id != bundle_id:
                entry["xue:bundle"] = bundle_id
            variables[variable_id] = entry
    return variables


# ---------------------------------------------------------------------------
# Assets


def _file_fields(byte_length: int, crc32: str, *, single_file: bool) -> dict[str, Any]:
    """The size and the CRC of one artifact. ``file:checksum`` is a hash of
    one file's bytes, so a store — many objects, whose ``crc32`` is that of
    its root document — gets the bare ``xue:crc32`` only."""
    fields: dict[str, Any] = {"file:size": int(byte_length), "xue:crc32": crc32}
    if single_file:
        fields["file:checksum"] = CRC32_MULTIHASH_PREFIX + crc32
    return fields


def _data_assets(entry: dict[str, Any], key: str, *, tier: str, title: str, roles: list[str]) -> dict[str, Any]:
    """The delivery assets of one bundle entry or one variant: the store
    under ``key`` when the entry ships one, the container under ``key`` when
    it is the only delivery and under ``key-xue`` beside a store. They are
    not alternates of one another in the alternate-assets sense — different
    bytes — so each is an asset of its own."""
    assets: dict[str, Any] = {}
    store = entry.get("zarr")
    if store is not None:
        assets[key] = {
            "href": store["path"],
            "type": ZARR_MEDIA_TYPE,
            "title": title,
            "roles": roles,
            "xue:kind": "store",
            "xue:tier": tier,
            **_file_fields(store["byteLength"], store["crc32"], single_file=False),
        }
    if "path" in entry:
        assets[key if store is None else f"{key}-xue"] = {
            "href": entry["path"],
            "type": CONTAINER_MEDIA_TYPE,
            "title": title if store is None else f"{title} (.xue container)",
            "roles": roles,
            "xue:kind": "container",
            "xue:tier": tier,
            **_file_fields(entry["byteLength"], entry["crc32"], single_file=True),
        }
    return assets


def _bundle_title(bundle_id: str) -> str:
    if bundle_id in VECTOR_BUNDLES:
        u, v = VECTOR_BUNDLES[bundle_id]
        labels = [VARIABLES[c].label for c in (u, v) if c in VARIABLES]
        return " / ".join(labels) if labels else bundle_id
    spec = VARIABLES.get(bundle_id)
    return spec.label if spec is not None else bundle_id


def manifest_assets(manifest: dict[str, Any], manifest_crc32: str) -> dict[str, Any]:
    """Every artifact a manifest names, as STAC assets keyed by bundle:
    ``<bundle>`` (and ``<bundle>-xue``) for the full tier, ``<bundle>-half``
    for the reduced one, ``<bundle>-poster``, ``<bundle>-video`` with its
    ``-video-index``, and ``manifest`` for the manifest itself under the
    ``?v=`` a viewer fetches it with. Hrefs are relative to the Item, which
    sits beside the manifest."""
    assets: dict[str, Any] = {
        "manifest": {
            "href": f"manifest.json?v={manifest_crc32}",
            "type": JSON_MEDIA_TYPE,
            "title": "Xue manifest (schema v5)",
            "roles": ["metadata"],
            "xue:kind": "manifest",
            "xue:crc32": manifest_crc32,
        }
    }
    for bundle in manifest["bundles"]:
        bundle_id = bundle["variable"]
        title = _bundle_title(bundle_id)
        assets.update(_data_assets(bundle, bundle_id, tier="full", title=title, roles=["data"]))
        for variant in bundle.get("variants", []):
            half = _data_assets(variant, f"{bundle_id}-half", tier="half", title=f"{title} (half resolution)", roles=["data", "overview"])
            for asset in half.values():
                asset["xue:grid"] = {"width": variant["width"], "height": variant["height"]}
            assets.update(half)
        poster = bundle.get("poster")
        if poster is not None:
            assets[f"{bundle_id}-poster"] = {
                "href": poster["path"],
                "type": POSTER_MEDIA_TYPE,
                "title": f"{title} (first-frame poster)",
                "roles": ["overview"],
                "xue:kind": "poster",
                "xue:grid": {"width": poster["width"], "height": poster["height"]},
                **_file_fields(poster["byteLength"], poster["crc32"], single_file=True),
            }
        video = bundle.get("video")
        if video is not None:
            assets[f"{bundle_id}-video"] = {
                "href": video["streamPath"],
                "type": VIDEO_MEDIA_TYPE,
                "title": f"{title} (H.264 companion)",
                "roles": ["data"],
                "xue:kind": "video",
                "xue:tier": "full",
                "xue:codec": video["codec"],
                "xue:grid": {"width": video["width"], "height": video["height"]},
                **_file_fields(video["byteLength"], video["crc32"], single_file=True),
            }
            assets[f"{bundle_id}-video-index"] = {
                "href": video["indexPath"],
                "type": JSON_MEDIA_TYPE,
                "title": f"{title} (H.264 companion index)",
                "roles": ["metadata"],
                "xue:kind": "video-index",
            }
    return assets


# ---------------------------------------------------------------------------
# Items


def _up(depth: int) -> str:
    return "../" * depth


def _item_properties(
    manifest: dict[str, Any], *, source: SourceSpec, grid: dict[str, Any] | None, bbox: list[float] | None
) -> tuple[dict[str, Any], list[str]]:
    """The properties block shared by a run's Item and a case's, and the
    extensions it uses."""
    run_time = _parse_time(manifest["runTime"])
    leads, from_metadata = _time_axis_of(manifest)
    start = run_time + timedelta(seconds=leads[0])
    end = run_time + timedelta(seconds=leads[-1])
    extensions = [DATACUBE_EXTENSION, FILE_EXTENSION]
    dimensions: dict[str, Any] = {}
    if grid is not None and bbox is not None:
        dimensions.update(_spatial_dimensions(grid, bbox))
    dimensions["time"] = _temporal_dimension(run_time, leads)
    properties: dict[str, Any] = {
        # A period, so the two bounds carry it; `datetime` doubles the start
        # rather than going null so a client that sorts on it still can.
        "datetime": iso_z(start),
        "start_datetime": iso_z(start),
        "end_datetime": iso_z(end),
        "xue:model": manifest["model"],
        "xue:product": manifest["product"],
        "xue:source": source.id,
        "xue:runTime": manifest["runTime"],
        "xue:forecastHours": manifest["forecastHours"],
        "xue:observation": source.observation,
        "xue:manifestSchemaVersion": manifest["schemaVersion"],
        "xue:frameCount": len(leads) if from_metadata else None,
        "cube:dimensions": dimensions,
        "cube:variables": _variables_of(manifest, ["time", "y", "x"] if grid is not None else ["time"]),
    }
    if not source.observation:
        extensions.insert(0, FORECAST_EXTENSION)
        properties["forecast:reference_datetime"] = manifest["runTime"]
        # The extension's horizon is one lead; an Item covering a whole axis
        # states the longest, and the period's bounds say the rest.
        properties["forecast:horizon"] = _iso_duration(leads[-1])
        properties["forecast:perturbed"] = False
    return properties, extensions


def run_item(
    manifest: dict[str, Any],
    manifest_crc32: str,
    *,
    source: SourceSpec,
    manifest_relative_path: str,
) -> dict[str, Any]:
    """The Item for one published run, from its manifest alone.

    ``manifest_relative_path`` is the manifest's path from the data root
    (``gfs.2026091512/manifest.json``, ``mrms.2026091509/1455/manifest.json``
    for a rolling window's round) — what the live pointer's ``manifestPath``
    carries. The Item's id is that directory with ``/`` as ``.``, and its
    links climb back to the root and the source's Collection from there."""
    directory = Path(manifest_relative_path).parent
    if directory == Path(".") or Path(manifest_relative_path).name != "manifest.json":
        raise StacError(f"a run item derives from <run>/manifest.json, not {manifest_relative_path}")
    depth = len(directory.parts)
    grid = _grid_of(manifest)
    bbox = _extent_of(grid) if grid is not None else None
    properties, extensions = _item_properties(manifest, source=source, grid=grid, bbox=bbox)
    item: dict[str, Any] = {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": extensions,
        "id": ".".join(directory.parts),
        "collection": source.id,
        "geometry": _bbox_geometry(bbox) if bbox is not None else None,
        **({"bbox": bbox} if bbox is not None else {}),
        "properties": {
            "title": f"{_source_prose(source)['title']} · {_run_label(manifest, source)}",
            **properties,
        },
        "links": [
            {"rel": "root", "href": f"{_up(depth)}{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "parent", "href": f"{_up(depth)}{source.id}/{COLLECTION_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "collection", "href": f"{_up(depth)}{source.id}/{COLLECTION_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
        ],
        "assets": manifest_assets(manifest, manifest_crc32),
    }
    validate_item(item)
    return item


def _run_label(manifest: dict[str, Any], source: SourceSpec) -> str:
    run_time = _parse_time(manifest["runTime"])
    if source.observation:
        return f"window from {run_time.strftime('%Y-%m-%d %H:%MZ')}"
    return f"{run_time.strftime('%Y-%m-%d %HZ')} cycle"


def case_item(entry: dict[str, Any], manifest: dict[str, Any], manifest_crc32: str) -> dict[str, Any]:
    """The Item for one showcase case, from its ``showcase.json`` row and its
    manifest. The row carries what the manifest does not — the exact grid
    the case was encoded on, the event time, the eleven-locale prose — so
    the case's ``bbox`` is its ``dataBbox`` and its spatial steps are
    exact."""
    source = source_spec(entry["modelId"])
    bbox = [float(value) for value in entry["dataBbox"]]
    # The steps off the manifest's metadata are exact (a poster's are the
    # grid's doubled); the row's bbox and dimensions only approximate them.
    grid = _grid_of(manifest)
    if grid is None:
        width, height = int(entry["grid"]["width"]), int(entry["grid"]["height"])
        grid = {
            "longitude_step": ((bbox[2] - bbox[0]) % 360.0) / (width - 1) if width > 1 else 0.0,
            "latitude_step": (bbox[3] - bbox[1]) / (height - 1) if height > 1 else 0.0,
        }
    properties, extensions = _item_properties(manifest, source=source, grid=grid, bbox=bbox)
    properties.update(
        {
            "title": entry["title"]["en"],
            "description": entry["summary"]["en"],
            "license": _source_prose(source)["license"],
            "xue:case": entry["id"],
            "xue:title": entry["title"],
            "xue:summary": entry["summary"],
            "xue:bbox": entry["bbox"],
            "xue:grid": entry["grid"],
            "xue:defaultVariable": entry["defaultVariable"],
        }
    )
    if "eventTime" in entry:
        # The moment the case is about, which is what a search should find
        # it by; the period stays in the two bounds.
        properties["datetime"] = entry["eventTime"]
        properties["xue:eventTime"] = entry["eventTime"]
    if "tags" in entry:
        properties["xue:tags"] = list(entry["tags"])
    if "credit" in entry:
        properties["xue:credit"] = entry["credit"]
    item: dict[str, Any] = {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": extensions,
        "id": entry["id"],
        "collection": SHOWCASE_COLLECTION_ID,
        "geometry": _bbox_geometry(bbox),
        "bbox": bbox,
        "properties": properties,
        "links": [
            {"rel": "root", "href": f"../../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "parent", "href": f"../{COLLECTION_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "collection", "href": f"../{COLLECTION_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
        ],
        "assets": manifest_assets(manifest, manifest_crc32),
    }
    validate_item(item)
    return item


# ---------------------------------------------------------------------------
# The point products


def _point_product_directory(product: str, index_relative_path: str) -> str:
    """The issue directory an index lives in (``sounding.2026091402``),
    checked against the product it is claimed to belong to. The directory
    name is the Item's id, the way a run's is."""
    path = Path(index_relative_path)
    if path.name != INDEX_FILENAME or len(path.parts) != 2:
        raise StacError(f"a {product} item derives from <issue>/index.json, not {index_relative_path}")
    directory = path.parts[0]
    if not re.fullmatch(rf"{re.escape(product)}\.\d+", directory):
        raise StacError(f"{directory} is not a {product} issue directory")
    return directory


def _point_product_positions(index: dict[str, Any], product: str) -> list[tuple[float, float]]:
    """Every station's (longitude, latitude): the soundings' and the
    airports' own, a storm's headline position. A storm with nothing
    observed has none, and an index of such storms alone has no geometry
    at all — which STAC allows, geometry null and no bbox."""
    if product == "sounding":
        return [(float(station["lon"]), float(station["lat"])) for station in index["stations"]]
    if product == "airport":
        return [(float(row[2]), float(row[1])) for row in index["stations"]]
    return [
        (float(storm["position"]["lon"]), float(storm["position"]["lat"]))
        for storm in index["storms"]
        if storm.get("position")
    ]


def _point_product_period(index: dict[str, Any], product: str, issued: datetime) -> tuple[str, str]:
    """What the issue covers, out of the index alone: a sounding issue from
    the oldest nominal time any station still carries to the newest ascent
    in it; an airport round from 24 hours before it (the history window) to
    the newest observation; a tc issue across the headline positions. An
    index with nothing in it is the instant it was issued."""
    default = iso_z(issued)
    if product == "sounding":
        stations = index["stations"]
        bounds = (
            min((station["times"][-1] for station in stations), default=default),
            max((station["latest"] for station in stations), default=default),
        )
    elif product == "airport":
        bounds = (
            iso_z(issued - timedelta(hours=24)),
            max((row[4] for row in index["stations"]), default=default),
        )
    else:
        times = [storm["position"]["time"] for storm in index["storms"] if storm.get("position")]
        bounds = (min(times, default=default), max(times, default=default))
    start, end = (iso_z(_parse_time(value)) for value in bounds)
    return min(start, end), max(start, end)


def _point_product_assets(
    index: dict[str, Any], product: str, index_byte_length: int, index_crc32: str
) -> dict[str, Any]:
    """The issue's files as assets: the index itself, then what it names —
    the one NDJSON file the soundings and the airports publish, or one JSON
    file per storm. Every href carries the ``?v=`` the reader fetches it
    under, which for the index is the pointer's own CRC32 (the index does
    not state its own length, so the caller, which has the bytes, does)."""
    assets: dict[str, Any] = {
        "index": {
            "href": f"{INDEX_FILENAME}?v={index_crc32}",
            "type": JSON_MEDIA_TYPE,
            "title": f"{product} index (schema v{index['schemaVersion']})",
            "roles": ["metadata"],
            "xue:kind": "index",
            **_file_fields(index_byte_length, index_crc32, single_file=True),
        }
    }
    if product in ("sounding", "airport"):
        key, descriptor = ("soundings", index["soundings"]) if product == "sounding" else ("history", index["history"])
        assets[key] = {
            "href": f"{descriptor['path']}?v={descriptor['crc32']}",
            "type": NDJSON_MEDIA_TYPE,
            "title": "Soundings by station" if product == "sounding" else "Observations and forecasts by station",
            "description": (
                "One station per line, sorted by id. A station's `offset` and `length` in the index are "
                "the byte span of its line's JSON object, excluding the newline, so one station is one "
                "`Range: bytes=<offset>-<offset+length-1>` request and the whole file streams line by line."
            ),
            "roles": ["data"],
            "xue:kind": "series",
            **_file_fields(descriptor["byteLength"], descriptor["crc32"], single_file=True),
        }
        return assets
    for storm in index["storms"]:
        name = storm.get("name")
        assets[storm["id"]] = {
            "href": f"{storm['path']}?v={storm['crc32']}",
            "type": JSON_MEDIA_TYPE,
            "title": f"{name} ({storm['id']})" if name else storm["id"],
            "roles": ["data"],
            "xue:kind": "storm",
            "xue:level": storm["level"],
            "xue:basin": storm["basin"],
            **_file_fields(storm["byteLength"], storm["crc32"], single_file=True),
        }
    return assets


def _point_product_label(product: str, issued: datetime) -> str:
    if product == "airport":
        return f"{issued.strftime('%Y-%m-%d %H:%MZ')} round"
    return f"{issued.strftime('%Y-%m-%d %HZ')} issue"


def point_product_item(
    index: dict[str, Any],
    index_byte_length: int,
    index_crc32: str,
    *,
    product: str,
    index_relative_path: str,
) -> dict[str, Any]:
    """The Item for one issue of a point product, from its ``index.json``
    alone (``docs/stac.md`` §"Point products").

    ``index_relative_path`` is the index's path from the data root
    (``tc.2026091301/index.json``) — what the product's pointer carries —
    and its directory is the Item's id. There is no grid and no forecast
    axis here: a point product is a set of stations, so the Item states
    where they are, what period the issue covers and which files carry
    it."""
    directory = _point_product_directory(product, index_relative_path)
    issued = _parse_time(index["issued"])
    positions = _point_product_positions(index, product)
    bbox = (
        [
            round(min(lon for lon, _ in positions), 6),
            round(min(lat for _, lat in positions), 6),
            round(max(lon for lon, _ in positions), 6),
            round(max(lat for _, lat in positions), 6),
        ]
        if positions
        else None
    )
    start, end = _point_product_period(index, product, issued)
    prose = _point_product_prose(product)
    count_key = "xue:storms" if product == "tc" else "xue:stations"
    properties: dict[str, Any] = {
        "title": f"{prose['title']} · {_point_product_label(product, issued)}",
        # The issue is one instant a client sorts on; the period it covers
        # is the two bounds, as a run's Item states them.
        "datetime": iso_z(issued),
        "start_datetime": start,
        "end_datetime": end,
        "xue:product": product,
        "xue:schemaVersion": index["schemaVersion"],
        "xue:issued": iso_z(issued),
        count_key: len(index["storms"] if product == "tc" else index["stations"]),
        "xue:sources": [{"id": source["id"], "ok": bool(source["ok"])} for source in index["sources"]],
    }
    if product == "sounding":
        # The product's own account of how current it is, per gateway.
        properties["xue:watermark"] = index["watermark"]
    item: dict[str, Any] = {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": [FILE_EXTENSION],
        "id": directory,
        "collection": product,
        "geometry": _bbox_geometry(bbox) if bbox is not None else None,
        **({"bbox": bbox} if bbox is not None else {}),
        "properties": properties,
        "links": [
            {"rel": "root", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "parent", "href": f"../{product}/{COLLECTION_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "collection", "href": f"../{product}/{COLLECTION_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
        ],
        "assets": _point_product_assets(index, product, index_byte_length, index_crc32),
    }
    validate_item(item)
    return item


def point_product_collection(product: str, item: dict[str, Any], item_relative_path: str) -> dict[str, Any]:
    """The Collection of one point product, at ``<product>/collection.json``
    — the STAC face of ``latest-<product>.json``, in the posture
    `source_collection` takes for a run: its ``item`` and ``latest-version``
    links both name the live Item beside it, and the issue's own copy is the
    ``alternate`` that dies with the issue.

    The extent is the whole world and an open interval: every issue is a
    rolling window whose start moves, so a Collection stating the live
    issue's bounds would be wrong the moment the next one lands."""
    prose = _point_product_prose(product)
    properties = item["properties"]
    collection: dict[str, Any] = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "id": product,
        "title": prose["title"],
        "description": prose["description"],
        "license": prose["license"],
        "keywords": prose["keywords"],
        "providers": prose["providers"],
        "extent": {
            "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {"interval": [[None, None]]},
        },
        "xue:pointer": f"latest-{product}.json",
        "xue:live": item["id"],
        "links": [
            {"rel": "root", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "parent", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "item", "href": ITEM_FILENAME, "type": GEOJSON_MEDIA_TYPE, "title": properties["title"]},
            {"rel": "latest-version", "href": ITEM_FILENAME, "type": GEOJSON_MEDIA_TYPE, "title": properties["title"]},
            {
                "rel": "alternate",
                "href": f"../{item_relative_path}",
                "type": GEOJSON_MEDIA_TYPE,
                "title": f"{properties['title']} (beside its index; gone when the issue is)",
            },
            {
                "rel": "xue:pointer",
                "href": f"../latest-{product}.json",
                "type": JSON_MEDIA_TYPE,
                "title": "live pointer (schema v1)",
            },
            {
                "rel": "describedby",
                "href": f"https://github.com/ringsaturn/xue/blob/main/docs/{product}.md",
                "type": "text/markdown",
                "title": f"The {product} product, schema v{properties['xue:schemaVersion']}",
            },
            *prose["links"],
        ],
    }
    validate_collection(collection)
    return collection


# ---------------------------------------------------------------------------
# Collections and the catalog


def source_collection(source: SourceSpec, item: dict[str, Any], item_relative_path: str) -> dict[str, Any]:
    """The Collection of one live source: its extent is the live run's
    (only the newest run is kept), and its ``item`` and ``latest-version``
    links both name the **live Item** beside it (``item.json`` in the same
    directory, the run's Item relocated to a stable path) — ``latest-version``
    because the Collection is the STAC face of the live pointer and a
    client following it lands where the pointer points. The run's own Item
    beside its manifest is the ``alternate``: the same document at an
    address that dies with the run. Sits at ``<source>/collection.json``,
    one level under the data root."""
    if not source.live:
        raise StacError(f"{source.manifest_model} has no live feed to catalog")
    prose = _source_prose(source)
    item_href = ITEM_FILENAME
    properties = item["properties"]
    collection: dict[str, Any] = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [extension for extension in item["stac_extensions"] if extension == FORECAST_EXTENSION],
        "id": source.id,
        "title": prose["title"],
        "description": prose["description"],
        "license": prose["license"],
        "keywords": ["weather", "observation" if source.observation else "forecast", source.manifest_model, "zarr"],
        "providers": prose["providers"],
        "extent": {
            "spatial": {"bbox": [item.get("bbox", [-180.0, -90.0, 180.0, 90.0])]},
            "temporal": {"interval": [[properties["start_datetime"], properties["end_datetime"]]]},
        },
        "xue:pointer": source.latest_filename,
        "xue:live": item["id"],
        "links": [
            {"rel": "root", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "parent", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "item", "href": item_href, "type": GEOJSON_MEDIA_TYPE, "title": properties["title"]},
            {"rel": "latest-version", "href": item_href, "type": GEOJSON_MEDIA_TYPE, "title": properties["title"]},
            {
                "rel": "alternate",
                "href": f"../{item_relative_path}",
                "type": GEOJSON_MEDIA_TYPE,
                "title": f"{properties['title']} (beside its manifest; gone when the run is)",
            },
            {
                "rel": "xue:pointer",
                "href": f"../{source.latest_filename}",
                "type": JSON_MEDIA_TYPE,
                "title": "live pointer (schema v1)",
            },
            *prose["links"],
        ],
    }
    if FORECAST_EXTENSION in collection["stac_extensions"]:
        collection["summaries"] = {"forecast:reference_datetime": [properties["forecast:reference_datetime"]]}
    validate_collection(collection)
    return collection


def showcase_collection(items: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """The Collection of the showcase cases, at ``showcase/collection.json``:
    one ``item`` link per case, an extent that is the union of theirs. The
    cases come from several sources under several licenses, so the
    Collection's is ``other`` and each Item states its own."""
    bboxes = [item["bbox"] for _, item in items]
    intervals = [[item["properties"]["start_datetime"], item["properties"]["end_datetime"]] for _, item in items]
    collection: dict[str, Any] = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "id": SHOWCASE_COLLECTION_ID,
        "title": "Xue showcase cases",
        "description": (
            "Historical weather events, each a past run or observation window cropped to the event: "
            "the archive the viewer keeps when live runs are pruned."
        ),
        "license": "other",
        "keywords": ["weather", "case study", "zarr"],
        "providers": [_XUE_PROVIDER],
        "extent": {
            "spatial": {"bbox": [_union_bbox(bboxes), *bboxes] if bboxes else [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {
                "interval": [
                    [min(start for start, _ in intervals), max(end for _, end in intervals)],
                    *intervals,
                ]
                if intervals
                else [[None, None]]
            },
        },
        "xue:catalog": "../showcase.json",
        "links": [
            {"rel": "root", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            {"rel": "parent", "href": f"../{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            *(
                {"rel": "item", "href": href, "type": GEOJSON_MEDIA_TYPE, "title": item["properties"]["title"]}
                for href, item in items
            ),
        ],
    }
    validate_collection(collection)
    return collection


def _union_bbox(bboxes: list[list[float]]) -> list[float]:
    """The box around every case; a box across the antimeridian counts by
    its own two edges, so the union of one is the whole longitude circle
    only when the cases really span it."""
    if any(box[0] > box[2] for box in bboxes):
        west, east = -180.0, 180.0
    else:
        west, east = min(box[0] for box in bboxes), max(box[2] for box in bboxes)
    return [west, min(box[1] for box in bboxes), east, max(box[3] for box in bboxes)]


def root_catalog() -> dict[str, Any]:
    """The root Catalog: one child per source with a live feed, in registry
    order, then one per point product and the showcase. A pure function of
    the registry and that fixed list, so every publish — of a run, of an
    hour of soundings, of a round of airports — rewrites the same bytes."""
    catalog: dict[str, Any] = {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": CATALOG_ID,
        "title": "Xue",
        "description": (
            "Global and regional weather forecast runs and radar observations, packed by Xue into "
            "Zarr v3 stores (docs/zarr-profile.md) for playback in a browser and reading with xarray, "
            "beside three point products in plain JSON: radiosonde soundings, airport reports and "
            "tropical cyclone tracks. One Collection per source or product, whose Item is the newest "
            "run or issue; the showcase Collection keeps historical cases."
        ),
        "links": [
            {"rel": "root", "href": f"./{CATALOG_FILENAME}", "type": STAC_JSON_MEDIA_TYPE},
            *(
                {
                    "rel": "child",
                    "href": f"{source.id}/{COLLECTION_FILENAME}",
                    "type": STAC_JSON_MEDIA_TYPE,
                    "title": _source_prose(source)["title"],
                }
                for source in SOURCES.values()
                if source.live
            ),
            *(
                {
                    "rel": "child",
                    "href": f"{product}/{COLLECTION_FILENAME}",
                    "type": STAC_JSON_MEDIA_TYPE,
                    "title": _point_product_prose(product)["title"],
                }
                for product in POINT_PRODUCTS
            ),
            {
                "rel": "child",
                "href": f"{SHOWCASE_COLLECTION_ID}/{COLLECTION_FILENAME}",
                "type": STAC_JSON_MEDIA_TYPE,
                "title": "Xue showcase cases",
            },
            {
                "rel": "describedby",
                "href": "https://github.com/ringsaturn/xue/blob/main/docs/stac.md",
                "type": "text/markdown",
                "title": "How this catalog is derived",
            },
        ],
    }
    validate_catalog(catalog)
    return catalog


# ---------------------------------------------------------------------------
# Validation — the structural contract every document is held to on write


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StacError(message)


def _validate_links(document: dict[str, Any], *required_rels: str) -> None:
    links = document.get("links")
    _require(isinstance(links, list), "links must be a list")
    rels: list[str] = []
    for link in links:
        _require(isinstance(link, dict), "a link must be an object")
        _require(isinstance(link.get("rel"), str) and bool(link["rel"]), "a link needs a rel")
        href = link.get("href")
        _require(isinstance(href, str) and bool(href), f"link {link.get('rel')} needs an href")
        rels.append(link["rel"])
    for rel in required_rels:
        _require(rel in rels, f"a {document['type']} needs a {rel} link")
    _require(rels.count("latest-version") <= 1, "at most one latest-version link")


def _validate_bbox(bbox: object) -> None:
    _require(
        isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(value, (int, float)) for value in bbox),
        "bbox must be four numbers",
    )
    assert isinstance(bbox, list)
    west, south, east, north = bbox
    _require(-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0, "bbox longitudes must lie in [-180, 180]")
    _require(-90.0 <= south <= north <= 90.0, "bbox latitudes must be ordered within [-90, 90]")


def _validate_common(document: dict[str, Any], kind: str) -> None:
    _require(document.get("type") == kind, f"type must be {kind}")
    _require(document.get("stac_version") == STAC_VERSION, f"stac_version must be {STAC_VERSION}")
    _require(isinstance(document.get("id"), str) and bool(document["id"]), "id must be a non-empty string")


def validate_item(item: dict[str, Any]) -> None:
    _validate_common(item, "Feature")
    _require(isinstance(item.get("stac_extensions"), list), "stac_extensions must be a list")
    geometry = item.get("geometry")
    if geometry is None:
        _require("bbox" not in item, "an item without geometry has no bbox")
    else:
        _require(isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon"), "geometry must be a polygon")
        _validate_bbox(item.get("bbox"))
    properties = item.get("properties")
    _require(isinstance(properties, dict), "properties must be an object")
    for key in ("datetime", "start_datetime", "end_datetime"):
        _require(isinstance(properties.get(key), str), f"properties.{key} must be a timestamp")
    _require(properties["start_datetime"] <= properties["end_datetime"], "start_datetime must not follow end_datetime")
    if DATACUBE_EXTENSION in item["stac_extensions"]:
        # A raster run or case is a cube; a point product's issue is a set
        # of stations and declares no dimensions.
        _require(isinstance(properties.get("cube:dimensions"), dict), "cube:dimensions must be an object")
    if FORECAST_EXTENSION in item["stac_extensions"]:
        _require(isinstance(properties.get("forecast:reference_datetime"), str), "forecast:reference_datetime is required")
    _validate_links(item, "root", "parent", "collection")
    assets = item.get("assets")
    # A run's Item names the manifest it derives from, a point product's
    # the index; either way the document it was derived from is an asset.
    _require(
        isinstance(assets, dict) and ("manifest" in assets or "index" in assets),
        "assets must include the manifest or the index",
    )
    for key, asset in assets.items():
        _require(isinstance(asset, dict) and isinstance(asset.get("href"), str), f"asset {key} needs an href")
        _require(not asset["href"].startswith(("/", "http:", "https:")), f"asset {key} href must be relative")
        _require(isinstance(asset.get("roles"), list) and bool(asset["roles"]), f"asset {key} needs roles")
        crc32 = asset.get("xue:crc32")
        if crc32 is not None:
            _require(isinstance(crc32, str) and bool(_CRC32_PATTERN.fullmatch(crc32)), f"asset {key} xue:crc32 is malformed")
        checksum = asset.get("file:checksum")
        if checksum is not None:
            _require(checksum == CRC32_MULTIHASH_PREFIX + crc32, f"asset {key} file:checksum must be its crc32 as a multihash")


def validate_collection(collection: dict[str, Any]) -> None:
    _validate_common(collection, "Collection")
    _require(isinstance(collection.get("description"), str) and bool(collection["description"]), "description is required")
    _require(isinstance(collection.get("license"), str) and bool(collection["license"]), "license is required")
    extent = collection.get("extent")
    _require(isinstance(extent, dict), "extent must be an object")
    bboxes = extent.get("spatial", {}).get("bbox")
    _require(isinstance(bboxes, list) and bool(bboxes), "extent.spatial.bbox must be a non-empty list")
    for bbox in bboxes:
        _validate_bbox(bbox)
    intervals = extent.get("temporal", {}).get("interval")
    _require(isinstance(intervals, list) and bool(intervals), "extent.temporal.interval must be a non-empty list")
    for interval in intervals:
        _require(isinstance(interval, list) and len(interval) == 2, "a temporal interval is two bounds")
    _validate_links(collection, "root", "parent")


def validate_catalog(catalog: dict[str, Any]) -> None:
    _validate_common(catalog, "Catalog")
    _require(isinstance(catalog.get("description"), str) and bool(catalog["description"]), "description is required")
    _validate_links(catalog, "root", "child")


# ---------------------------------------------------------------------------
# Writing


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _rebase_href(href: str, from_dir: str, to_dir: str) -> str:
    """``href``, written relative to ``from_dir``, rewritten relative to
    ``to_dir`` (both directories relative to the data root, ``""`` for the
    root itself). An absolute URL and a query string pass through."""
    if href.startswith(("http:", "https:", "/")):
        return href
    path, separator, query = href.partition("?")
    target = posixpath.normpath(posixpath.join(from_dir, path)) if from_dir else posixpath.normpath(path)
    rebased = posixpath.relpath(target, to_dir or ".")
    return f"{rebased}{separator}{query}"


def relocate_item(item: dict[str, Any], *, from_dir: str, to_dir: str) -> dict[str, Any]:
    """The same Item at another address: every relative asset and link href
    rewritten from ``from_dir`` to ``to_dir``, nothing else touched, so the
    live Item at ``<source>/item.json`` names exactly the objects the run's
    own Item does."""
    relocated = json.loads(json.dumps(item))
    for link in relocated["links"]:
        link["href"] = _rebase_href(link["href"], from_dir, to_dir)
    for asset in relocated["assets"].values():
        asset["href"] = _rebase_href(asset["href"], from_dir, to_dir)
    validate_item(relocated)
    return relocated


def write_run_documents(output_dir: Path, *, source: SourceSpec, manifest_path: Path) -> dict[str, str]:
    """Write the STAC documents a published run needs, from the manifest at
    ``manifest_path`` under ``output_dir`` (the data root): the run's
    ``item.json`` beside the manifest, the same Item relocated to the
    source's stable ``<source>/item.json``, the source's ``collection.json``
    and the root ``catalog.json``. Returns their paths by name.

    Called after the manifest and the pointer are written — by ``build-bin``
    for a run built whole (either encoder) and by ``assemble-run`` for one
    built in pieces — and never by the converter itself, so a partial build
    writes none: an Item describes a whole run."""
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    relative = manifest_path.relative_to(output_dir).as_posix()
    item = run_item(manifest, _crc32_of(manifest_bytes), source=source, manifest_relative_path=relative)
    item_path = manifest_path.with_name(ITEM_FILENAME)
    item_relative = item_path.relative_to(output_dir).as_posix()
    live_item = relocate_item(item, from_dir=posixpath.dirname(item_relative), to_dir=source.id)
    live_item_path = output_dir / source.id / ITEM_FILENAME
    collection = source_collection(source, item, item_relative)
    collection_path = output_dir / source.id / COLLECTION_FILENAME
    catalog_path = output_dir / CATALOG_FILENAME
    _write_json(item_path, item)
    _write_json(live_item_path, live_item)
    _write_json(collection_path, collection)
    _write_json(catalog_path, root_catalog())
    return {
        "item": str(item_path),
        "liveItem": str(live_item_path),
        "collection": str(collection_path),
        "catalog": str(catalog_path),
    }


def write_point_product_documents(
    output_dir: Path, *, product: str, index_path: Path, live: bool = True
) -> dict[str, str]:
    """Write the STAC documents one issue of a point product needs, from
    the ``index.json`` at ``index_path`` under ``output_dir`` (the data
    root): the issue's ``item.json`` beside the index and, when the issue
    is the live one, the same Item relocated to the product's stable
    ``<product>/item.json``, the product's ``collection.json`` and the root
    ``catalog.json``. Returns their paths by name.

    Called by ``sounding-build`` / ``airport-build`` / ``tc-build`` once
    the index is final, and by ``xue stac --product``. ``live`` is false
    when the build withheld the pointer: the issue still gets its Item —
    the directory is complete — but nothing names it, the way a Collection
    never names a run the pointer does not."""
    if product not in POINT_PRODUCTS:
        raise StacError(f"unknown point product {product!r}; choose from {', '.join(POINT_PRODUCTS)}")
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    relative = index_path.relative_to(output_dir).as_posix()
    item = point_product_item(
        index,
        len(index_bytes),
        _crc32_of(index_bytes),
        product=product,
        index_relative_path=relative,
    )
    item_path = index_path.with_name(ITEM_FILENAME)
    _write_json(item_path, item)
    written = {"item": str(item_path)}
    if not live:
        return written
    item_relative = item_path.relative_to(output_dir).as_posix()
    live_item_path = output_dir / product / ITEM_FILENAME
    collection_path = output_dir / product / COLLECTION_FILENAME
    catalog_path = output_dir / CATALOG_FILENAME
    _write_json(live_item_path, relocate_item(item, from_dir=posixpath.dirname(item_relative), to_dir=product))
    _write_json(collection_path, point_product_collection(product, item, item_relative))
    _write_json(catalog_path, root_catalog())
    return written | {
        "liveItem": str(live_item_path),
        "collection": str(collection_path),
        "catalog": str(catalog_path),
    }


def write_showcase_documents(output_dir: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    """Write one Item per case of ``catalog`` (the ``showcase.json``
    payload) beside its manifest, the showcase Collection, and the root
    catalog. Returns the paths written."""
    items: list[tuple[str, dict[str, Any]]] = []
    item_paths: list[str] = []
    for entry in catalog["cases"]:
        manifest_path = output_dir / entry["manifestPath"]
        manifest_bytes = manifest_path.read_bytes()
        item = case_item(entry, json.loads(manifest_bytes), _crc32_of(manifest_bytes))
        item_path = manifest_path.with_name(ITEM_FILENAME)
        _write_json(item_path, item)
        item_paths.append(str(item_path))
        items.append((f"{item_path.parent.name}/{ITEM_FILENAME}", item))
    collection_path = output_dir / SHOWCASE_COLLECTION_ID / COLLECTION_FILENAME
    _write_json(collection_path, showcase_collection(items))
    catalog_path = output_dir / CATALOG_FILENAME
    _write_json(catalog_path, root_catalog())
    return {"items": item_paths, "collection": str(collection_path), "catalog": str(catalog_path)}

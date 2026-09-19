# The STAC catalog

The shell reads the JSON it always has: a run's `manifest.json` (schema v5),
the per-model live pointer (schema v1), `showcase.json`. None of that
changes here. Beside them the encoder derives a static [SpatioTemporal
Asset Catalog](https://stacspec.org/) (STAC 1.1.0), so the Zarr stores can
be found the way that ecosystem finds data: `pystac` walks the catalog,
[`xpystac`](https://github.com/stac-utils/xpystac) / `odc-stac` open an
Item's `application/vnd.zarr` asset into xarray, and a STAC browser lists
the runs. This document is the contract: the layout, what each document
carries, and where it comes from.

The catalog is derived, not authoritative. Every document is a pure
function of a manifest, a point product's `index.json`, a `showcase.json`
row and the source registry
(`xuebuild/stac.py`), with no timestamps and no host names, which lets a
run built whole and a run built in pieces write the same bytes
(`tests/test_assemble.py`) and lets any document be regenerated from what
is on disk. The manifest stays what the shell validates and opens; a field
the catalog has and the manifest lacks is a summary. Should the manifest
itself become a STAC Item (a possible schema v6), this layer is what it
would become.

## Layout

At the data root (`https://dataset.ringsaturn.me/xue/`), beside the
pointers:

| Path | STAC object | Mutable | Written by |
|---|---|---|---|
| `catalog.json` | the root Catalog: one `child` per source with a live feed, one per point product, one for the showcase | yes (a pure function of the registry) | every publish, `showcase catalog` |
| `<source>/collection.json` | one Collection per source (`gfs`, `ecmwf`, `aifs`, `sflux`, `hrrr`, `mrms`, `jma`, `cma`), the STAC face of the live pointer | yes | `build-bin` (whole run), `assemble-run` |
| `<source>/item.json` | the live Item: the run's Item relocated to a path that never changes | yes, replaced by every publish | same |
| `<source>.<run>/item.json` | one Item per published run, beside its manifest | rewritten in place by a top-up, like the manifest | `build-bin` (whole run), `assemble-run` |
| `<source>.<run>/<HHMM>/item.json` | the Item of one round of a rolling window (MRMS, JMA, CMA radar) | no | `build-bin --round` |
| `<product>/collection.json` | one Collection per point product (`sounding`, `airport`, `tc`), the STAC face of its live pointer | yes | the product's build, `xue stac --product` |
| `<product>/item.json` | the live Item: the newest issue's Item relocated to a path that never changes | yes, replaced by every issue | same |
| `<product>.<issue>/item.json` | one Item per issue, beside its `index.json` | no | same |
| `showcase/collection.json` | the Collection of the historical cases | yes | `showcase build` / `refresh` / `catalog` |
| `showcase/<case>/item.json` | one Item per case, beside its manifest | rewritten by `showcase refresh` | same |
| `index.html` | not a STAC object: the root's landing page for a reader arriving in a browser, naming `catalog.json` and these documents, with a table of the live Collections filled from the catalog (`web/dataroot/index.html`) | yes | `make upload-r2-index`, by hand |

Mutable documents are served `no-cache`, like the pointer; an Item is too,
since a STAC client fetches it by its plain name rather than under a `?v=`
and a top-up rewrites it. Links are relative (`../catalog.json`,
`../gfs/collection.json`, `tmp2m.zarr`), so a client resolves them against
whichever origin it read the document from, and no document names a host.
There are no `self` links for the same reason.

The point products' documents are the same three shapes over an
`index.json` instead of a manifest; §"Point products" below.

Only the newest run per source is kept on the bucket (`prune-r2`), an hour
for HRRR and minutes for an MRMS round, so a link into `<source>.<run>/`
stops resolving when the run is pruned, and a client that bookmarked one,
or held a Collection in its cache, would get a 404. The Collection
therefore lists one Item at a stable path: `<source>/item.json`, the run's
Item with every relative href rewritten to reach into the run directory
(`../gfs.2026091512/tmp2m.zarr`) and nothing else changed
(`relocate_item`). Its `item` and `latest-version` links both name it; an
`alternate` link names the run's own copy beside its manifest. A client
that wants the live run follows `item`; one that wants the pointer's own
fields follows `xue:pointer` (`../latest.json`, `../latest-<source>.json`).
The live Item's assets still name objects that are deleted when the run is
superseded, so a client holds it no longer than the source's cadence, the
way the shell polls the pointer.

## The run Item

`<source>.<run>/item.json`, derived from `manifest.json` in the same
directory and nothing else. Id: the directory with `/` as `.`
(`gfs.2026091512`, `mrms.2026091509.1455`). Extensions declared:
[forecast v0.2.0](https://github.com/stac-extensions/forecast) (forecast
sources only), [datacube v2.3.0](https://github.com/stac-extensions/datacube),
[file v2.1.0](https://github.com/stac-extensions/file).

Time. The Item covers the run's whole axis: `start_datetime` /
`end_datetime` are the union of every axis the manifest's bundle metadata
carries (a rate that starts at the first step widens nothing; f240 is the
end), and `datetime` repeats the start so a client that sorts on it can.
`forecast:reference_datetime` is the cycle (`runTime`), `forecast:horizon`
the longest lead as an ISO 8601 duration (`PT240H`), `forecast:perturbed`
`false`, since every published run is a deterministic control. An
observation (MRMS) declares no forecast fields; its `xue:observation` is
`true`.

Space. The manifest names no grid; the Item reads one off the bundle
metadata a run's core scalars always carry: the H.264 companion's when
there is one (the full grid), else a poster's, which is the grid decimated
two to one (`GridInfo.decimated`: same origin, doubled step). So the
`cube:dimensions` `x` / `y` steps are exact, the origin is exact, and a
regional grid's far edge is right to within one full cell. A wrapping grid
is `[-180, -90, 180, 90]`. `bbox` and `geometry` follow (two polygons when a
window crosses the antimeridian). The authoritative grid is the store's own
`attributes.xue`; a manifest with no metadata at all (only the synthetic
ones in tests) gets a null geometry and a time dimension alone.

Variables. `cube:variables` has one entry per array the run publishes: a
scalar bundle is one, a vector bundle its two components (`ugrd10m` /
`vgrd10m` under `wind10m`, marked `xue:bundle`), each with the registry's
label and unit. A bundle the registry does not know (a manifest admits any
well-formed name) is listed by name alone.

Assets. One per artifact, keyed by bundle:

| Key | What | `type` | `roles` |
|---|---|---|---|
| `manifest` | `manifest.json?v=<crc32>`, the manifest under the `?v=` a viewer fetches it with | `application/json` | `metadata` |
| `<bundle>` | the Zarr store (`<bundle>.zarr`), or the `.xue` container when the entry ships no store | `application/vnd.zarr` / `application/octet-stream` | `data` |
| `<bundle>-xue` | the container, when it ships beside a store | `application/octet-stream` | `data` |
| `<bundle>-<tier>`, `-<tier>-xue` | each reduced-resolution tier, likewise: `-half` on every source, `-quarter` and `-eighth` too on the satellite disks (`SourceSpec.variant_factors`) | | `data`, `overview` |
| `<bundle>-poster` | the first-frame poster (`.poster.bin`) | `application/octet-stream` | `overview` |
| `<bundle>-video`, `-video-index` | the H.264 companion and its index | `video/H264`, `application/json` | `data`, `metadata` |

A store and a container beside it are not alternates in the
[alternate-assets](https://github.com/stac-extensions/alternate-assets)
sense (different bytes, different checksums), so each is an asset of its
own. Every asset carries `xue:kind` (`store`, `container`, `poster`,
`video`, `video-index`, `manifest`), data assets `xue:tier` (`full`, or
the rung's name `half` / `quarter` / `eighth`, read off the variant's
file suffix; the manifest lists a bundle's rungs in ascending factor
order), and reduced ones `xue:grid` (`width`, `height`). Sizes and
checksums use the file extension: `file:size` is the manifest's
`byteLength` (for a store, the sum of its objects), and `file:checksum` is
the manifest's `crc32` as a multihash: the multicodec table gives CRC-32
the code `0x0132`, so the value is `b20204` + the eight hex digits. A store
is many objects and its `crc32` is that of its root `zarr.json`, so it gets
no `file:checksum`; the bare `xue:crc32` on every asset is the artifact's
`?v=`.

`application/vnd.zarr` is the media type pystac 1.14 registers for a Zarr
store (`MediaType.VND_ZARR`); the `application/vnd+zarr` spelling older
catalogs carry is the same thing.

## The source Collection

`<source>/collection.json`. Id: the source id. `title`, `description`,
`license` (an SPDX id, `CC-BY-4.0` for ECMWF, or `other` with a `license`
link, the NOAA open data policy) and `providers` come from a table in
`xuebuild/stac.py`; `keywords` name the kind (`forecast` / `observation`)
and the model. `extent` is the live run's: its bbox, its
`[start_datetime, end_datetime]`. A forecast source's `summaries` carry
`forecast:reference_datetime`. Links: `root` and `parent` to the catalog,
`item` and `latest-version` to the live Item beside it, `alternate` to the
run's own Item, `xue:pointer` to the live pointer, `license`. `xue:live`
repeats the Item id and `xue:pointer` the pointer's file name.

## Point products

Three products beside the runs are not rasters and have no manifest: the
radiosonde soundings (`docs/sounding.md`), the airport reports
(`docs/airport.md`) and the tropical cyclone tracks (`docs/tc.md`). Each
publishes a mutable `latest-<product>.json` naming an immutable
`<product>.<issue>/index.json`, and each gets the same three documents a
source does — a Collection, a live Item, an Item per issue — derived from
that `index.json` alone and from nothing else.

`<product>/collection.json`. Id: the product id. `title`, `description`,
`license`, `keywords` and `providers` come from `_point_product_prose` in
`xuebuild/stac.py`; none of the three has an SPDX identifier to name, so
all three are `other` with the terms linked: the WMO Unified Data Policy
(Resolution 1, Cg-Ext(2021)) for the soundings, the NWS disclaimer for the
airport caches (a work of the US government is in the public domain, which
is not CC0), and both of those plus CC BY 4.0 for the tracks, whose
sources are several agencies at once. `extent` is the whole world and
`[[null, null]]`: every issue is a rolling window whose start moves, so a
Collection stating the live issue's bounds would be wrong as soon as the
next one lands, and the description says so. Links: `root` and `parent` to
the catalog, `item` and `latest-version` to the live Item beside it,
`alternate` to the issue's own copy, `xue:pointer` to the live pointer,
`describedby` to the product's document, and the `license` links.
`xue:live` repeats the Item id and `xue:pointer` the pointer's file name.

`<product>.<issue>/item.json`. Id: the directory
(`sounding.2026091402`, `airport.202609161430`, `tc.2026091301`).
Extensions: [file v2.1.0](https://github.com/stac-extensions/file) alone —
a point product is a set of stations, not a cube, and declares no
`cube:dimensions` and no forecast fields.

Space. `geometry` and `bbox` are the box around the stations the index
lists: the soundings' and the airports' own positions, a storm's headline
position. It is the plain minimum and maximum over those points, so two
storms either side of the Pacific make a box the long way round rather
than one across the antimeridian. An index whose storms have nothing
observed has no positions at all, and then `geometry` is `null` with no
`bbox`, which STAC allows.

Time. `datetime` is the issue (`issued`), what a client sorts on, and the
period it covers is in the two bounds: for a sounding issue the oldest
nominal time any station still carries to the newest ascent in it, for an
airport round the 24 hours of history it holds (`issued − 24 h` to the
newest observation), for a tc issue the earliest and latest headline
position. An empty index is the instant it was issued.

Properties. `xue:product`, `xue:schemaVersion`, `xue:issued`,
`xue:stations` (`xue:storms` for the tracks) and `xue:sources`, the
index's own `sources[]` reduced to `{id, ok}` so a client sees which
gateway or centre was down without reading the index. A sounding issue
also carries `xue:watermark`, the product's account per gateway of how
current it is.

Assets. One per file the issue publishes, each under the `?v=<crc32>` a
reader fetches it with, with `file:size` and `file:checksum` (the crc32 as
a multihash, as a run's artifacts carry):

| Key | What | `type` | `roles` |
|---|---|---|---|
| `index` | `index.json?v=<crc32>`, under the CRC the pointer carries | `application/json` | `metadata` |
| `soundings` / `history` | the product's one NDJSON file | `application/x-ndjson` | `data` |
| `<storm id>` | one JSON file per system a tc issue lists | `application/json` | `data` |

The NDJSON asset's `description` states the addressing rule, which is the
point of the layout: one station per line, sorted by id, and a station's
`offset` and `length` in the index are the byte span of its line's object
(the newline excluded), so one station is one `Range` request and the
whole file still streams. Assets carry `xue:kind` (`index`, `series`,
`storm`), and a storm's also `xue:level` and `xue:basin`.

The live Item at `<product>/item.json` is that Item relocated
(`relocate_item`), every href reaching into `../<product>.<issue>/`, so
the URL a client bookmarks still resolves after the issue it named is
pruned — two days for the soundings and the tracks, three hours for the
airport rounds.
An issue whose build withheld the pointer (no gateway contributed, both
observation sources failed) gets its own Item and nothing else: the
Collection and the live Item follow the pointer, as a source's do.

## The showcase

`showcase/collection.json` lists one `item` link per case in
`showcase.json`'s order (newest event first); its extent is the union of
theirs (first) followed by each case's own, and its `license` is `other`
because the cases come from several sources; each Item states its own
under `license`. `showcase/<case>/item.json` derives from the case's
`showcase.json` row and its manifest: `id` is the case id; `bbox` is the
row's `dataBbox` (exact); `datetime` is the `eventTime`, what a search
should find the case by, with the period in `start_datetime` /
`end_datetime`; `title` and `description` are the English strings, and the
whole eleven-locale maps sit under `xue:title` / `xue:summary`, with
`xue:tags`, `xue:credit`, `xue:defaultVariable`, `xue:eventTime`. A
forecast case declares the forecast fields like a run; an observation case
does not. Assets are the same per-bundle set as a run's.

## Producing it

`xuebuild build-bin` for a whole run and `xuebuild assemble-run` for one
built in pieces write the run's Item, the live Item, the Collection and the
catalog right after the manifest and the pointer (the report's `stac` names
the four files); a piece of a fanned-out publish (`build-bin --bundles`)
writes none, since an Item describes a whole run. `showcase build`,
`refresh` and `catalog` write the cases' documents whenever they rewrite
`showcase.json`. `make upload-r2` / `upload-r2-manifest` copy the run's
Item with the manifest, `make upload-r2-pointer` copies the live Item, the
Collection and the catalog with the pointer (the Item first, so the
Collection never names a live Item that is not there yet), and `make
upload-r2-showcase` the showcase's, all through the same targets the
scheduled workflows call. A publish that predates this document has no
Item, and that is not an error.

A point product's build (`xue sounding-build`, `airport-build`,
`tc-build`) writes the same four documents right after its index and
pointer, from the index on disk, and reports them the same way;
`xue stac --product sounding|airport|tc --issue <YYYYMMDDHH|YYYYMMDDHHMM>`
rewrites them for an issue already built. `make upload-r2-sounding`,
`upload-r2-airport` and `upload-r2-tc` copy the issue's Item with its
index (`no-cache`, `application/geo+json`, held out of the immutable
sync), then the pointer, then the live Item, the Collection and the
catalog through `upload-r2-stac-collection STAC_DIR=<product>` — the same
target and the same order as a run's.

Every document is validated on write against a structural contract
(`validate_item` / `validate_collection` / `validate_catalog` in
`xuebuild/stac.py`: the required fields, the link rels, a bbox in range,
the checksum matching the crc); the published JSON Schemas are not fetched
in the pipeline. `tests/test_stac.py` holds the shapes above and, when
`pystac` is installed, round-trips them through it.

## Reading it

```python
import pystac, xarray as xr

catalog = pystac.Catalog.from_file("https://dataset.ringsaturn.me/xue/catalog.json")
gfs = catalog.get_child("gfs")
run = next(gfs.get_items())                       # the live Item, …/gfs/item.json
store = run.assets["tmp2m"].get_absolute_href()   # …/gfs.<run>/tmp2m.zarr
ds = xr.open_zarr(store)                          # the profile in docs/zarr-profile.md
```

`run.properties["cube:variables"]` says what else the run carries and in
which unit; `run.assets["manifest"].href` is the manifest under its `?v=`,
for a client that wants the shell's own view of the same run.

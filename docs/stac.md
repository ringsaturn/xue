# The STAC catalog

Everything Xue publishes is described twice. The shell reads the JSON it
always has — a run's `manifest.json` (schema v5), the per-model live pointer
(schema v1), `showcase.json` — and none of that changes here. Beside them the
encoder now derives a static [SpatioTemporal Asset
Catalog](https://stacspec.org/) (STAC 1.1.0), so the Zarr stores can be
found the way that ecosystem finds data: `pystac` walks the catalog,
[`xpystac`](https://github.com/stac-utils/xpystac) / `odc-stac` open an
Item's `application/vnd.zarr` asset straight into xarray, and a STAC
browser lists the runs. This document is the contract: the layout, what each
document carries, and where it comes from.

The catalog is **derived, not authoritative**. Every document is a pure
function of a manifest, a `showcase.json` row and the source registry
(`xuebuild/stac.py`) — no timestamps, no host names — which is what lets a
run built whole and a run built in pieces write the same bytes
(`tests/test_assemble.py`) and lets any document be regenerated from what is
on disk. The manifest stays what the shell validates and opens; a field the
catalog has and the manifest lacks is a summary, never a second source of
truth. Should the manifest itself become a STAC Item one day (plan: a schema
v6), this layer is what it would become.

## Layout

At the data root (`https://dataset.ringsaturn.me/xue/`), beside the
pointers:

| Path | STAC object | Mutable | Written by |
|---|---|---|---|
| `catalog.json` | the root **Catalog**: one `child` per source with a live feed, one for the showcase | yes (rarely changes; a pure function of the registry) | every publish, `showcase catalog` |
| `<source>/collection.json` | one **Collection** per source (`gfs`, `ecmwf`, `sflux`, `hrrr`, `mrms`) | yes — the STAC face of the live pointer | `build-bin` (whole run), `assemble-run` |
| `<source>.<run>/item.json` | one **Item** per published run, beside its manifest | rewritten in place by a top-up, like the manifest | `build-bin` (whole run), `assemble-run` |
| `<source>.<run>/<HHMM>/item.json` | the Item of one round of the MRMS rolling window | no | `build-bin --round` |
| `showcase/collection.json` | the **Collection** of the historical cases | yes | `showcase build` / `refresh` / `catalog` |
| `showcase/<case>/item.json` | one Item per case, beside its manifest | rewritten by `showcase refresh` | same |

Mutable documents are served `no-cache`, like the pointer; an Item is too,
since a STAC client fetches it by its plain name rather than under a `?v=`
and a top-up rewrites it. Links are **relative** — `../catalog.json`,
`../gfs/collection.json`, `tmp2m.zarr` — so a client resolves them against
whichever origin it read the document from, and no document names a host.
There are no `self` links for the same reason.

Only the newest run per source is kept on the bucket (`prune-r2`), so a
source's Collection lists one Item: its `item` link and its
`latest-version` link both name the run the pointer names. A client that
wants the live run follows either; a client that wants the pointer's own
fields follows the `xue:pointer` link (`../latest.json`, `../latest-<source>.json`).

## The run Item

`<source>.<run>/item.json`, derived from `manifest.json` in the same
directory and nothing else. Id: the directory with `/` as `.`
(`gfs.2026091512`, `mrms.2026091509.1455`). Extensions declared:
[forecast v0.2.0](https://github.com/stac-extensions/forecast) (forecast
sources only), [datacube v2.3.0](https://github.com/stac-extensions/datacube),
[file v2.1.0](https://github.com/stac-extensions/file).

**Time.** The Item covers the run's whole axis: `start_datetime` /
`end_datetime` are the union of every axis the manifest's bundle metadata
carries (a rate that starts at the first step widens nothing; f240 is the
end), and `datetime` repeats the start so a client that sorts on it can.
`forecast:reference_datetime` is the cycle (`runTime`), `forecast:horizon`
the longest lead as an ISO 8601 duration (`PT240H`), `forecast:perturbed`
`false` — every published run is a deterministic control. An observation
(MRMS) declares no forecast fields; its `xue:observation` is `true`.

**Space.** The manifest names no grid; the Item reads one off the bundle
metadata a run's core scalars always carry — the H.264 companion's when
there is one (the full grid), else a poster's, which is the grid decimated
two to one (`GridInfo.decimated`: same origin, doubled step). So the
`cube:dimensions` `x` / `y` steps are exact, the origin is exact, and a
regional grid's far edge is right to within one full cell. A wrapping grid
is `[-180, -90, 180, 90]`. `bbox` and `geometry` follow (two polygons when a
window crosses the antimeridian). The authoritative grid is the store's own
`attributes.xue`; a manifest with no metadata at all (only the synthetic
ones in tests) gets a null geometry and a time dimension alone.

**Variables.** `cube:variables` has one entry per *array* the run
publishes: a scalar bundle is one, a vector bundle its two components
(`ugrd10m` / `vgrd10m` under `wind10m`, marked `xue:bundle`), each with the
registry's label and unit. A bundle the registry does not know — a manifest
admits any well-formed name — is listed by name alone.

**Assets.** One per artifact, keyed by bundle:

| Key | What | `type` | `roles` |
|---|---|---|---|
| `manifest` | `manifest.json?v=<crc32>` — the manifest under the `?v=` a viewer fetches it with | `application/json` | `metadata` |
| `<bundle>` | the Zarr store (`<bundle>.zarr`), or the `.xue` container when the entry ships no store | `application/vnd.zarr` / `application/octet-stream` | `data` |
| `<bundle>-xue` | the container, when it ships *beside* a store | `application/octet-stream` | `data` |
| `<bundle>-half`, `-half-xue` | the half-resolution tier, likewise | | `data`, `overview` |
| `<bundle>-poster` | the first-frame poster (`.poster.bin`) | `application/octet-stream` | `overview` |
| `<bundle>-video`, `-video-index` | the H.264 companion and its index | `video/H264`, `application/json` | `data`, `metadata` |

A store and a container beside it are *not* alternates in the
[alternate-assets](https://github.com/stac-extensions/alternate-assets)
sense — different bytes, different checksums — so each is an asset of its
own. Every asset carries `xue:kind` (`store`, `container`, `poster`,
`video`, `video-index`, `manifest`), data assets `xue:tier` (`full` /
`half`), and reduced ones `xue:grid` (`width`, `height`). Sizes and
checksums use the file extension: `file:size` is the manifest's
`byteLength` (for a store, the sum of its objects), and `file:checksum` is
the manifest's `crc32` as a multihash — the multicodec table gives CRC-32
the code `0x0132`, so the value is `b20204` + the eight hex digits. A store
is many objects and its `crc32` is that of its root `zarr.json`, so it gets
no `file:checksum`; the bare `xue:crc32` on every asset is the artifact's
`?v=`.

`application/vnd.zarr` is the media type pystac 1.14 registers for a Zarr
store (`MediaType.VND_ZARR`); the `application/vnd+zarr` spelling older
catalogs carry is the same thing.

## The source Collection

`<source>/collection.json`. Id: the source id. `title`, `description`,
`license` (an SPDX id — `CC-BY-4.0` for ECMWF — or `other` with a `license`
link, the NOAA open data policy) and `providers` come from a table in
`xuebuild/stac.py`; `keywords` name the kind (`forecast` / `observation`)
and the model. `extent` is the live run's: its bbox, its
`[start_datetime, end_datetime]`. A forecast source's `summaries` carry
`forecast:reference_datetime`. Links: `root` and `parent` to the catalog,
`item` and `latest-version` to the live run's Item, `xue:pointer` to the
live pointer, `license`. `xue:live` repeats the Item id and `xue:pointer`
the pointer's file name.

## The showcase

`showcase/collection.json` lists one `item` link per case in
`showcase.json`'s order (newest event first); its extent is the union of
theirs (first) followed by each case's own, and its `license` is `other`
because the cases come from several sources — each Item states its own
under `license`. `showcase/<case>/item.json` derives from the case's
`showcase.json` row and its manifest: `id` is the case id; `bbox` is the
row's `dataBbox` (exact); `datetime` is the `eventTime` — what a search
should find the case by — with the period in `start_datetime` /
`end_datetime`; `title` and `description` are the English strings, and the
whole eleven-locale maps sit under `xue:title` / `xue:summary`, with
`xue:tags`, `xue:credit`, `xue:defaultVariable`, `xue:eventTime`. A forecast
case declares the forecast fields like a run; an observation case does not.
Assets are the same per-bundle set as a run's.

## Producing it

Nothing here is a separate step to remember. `xuebuild build-bin` for a
whole run and `xuebuild assemble-run` for one built in pieces write the
Item, the Collection and the catalog right after the manifest and the
pointer (the report's `stac` names the three files); a piece of a fanned-out
publish (`build-bin --bundles`) writes none, since an Item describes a whole
run. `showcase build`, `refresh` and `catalog` write the cases' documents
whenever they rewrite `showcase.json`. On the way to the bucket, `make
upload-r2` / `upload-r2-manifest` copy the Item with the manifest, `make
upload-r2-pointer` copies the Collection and the catalog with the pointer,
and `make upload-r2-showcase` the showcase's — all through the same targets
the scheduled workflows call, so a publish that predates this document
simply has no Item, and that is not an error.

Every document is validated on write against a structural contract
(`validate_item` / `validate_collection` / `validate_catalog` in
`xuebuild/stac.py`: the required fields, the link rels, a bbox in range,
the checksum matching the crc); the published JSON Schemas are not fetched
in the pipeline. `tests/test_stac.py` holds the shapes above, and, when
`pystac` is installed, round-trips them through it.

## Reading it

```python
import pystac, xarray as xr

catalog = pystac.Catalog.from_file("https://dataset.ringsaturn.me/xue/catalog.json")
gfs = catalog.get_child("gfs")
run = next(gfs.get_items())                       # the live run
store = run.assets["tmp2m"].get_absolute_href()   # …/gfs.<run>/tmp2m.zarr
ds = xr.open_zarr(store, consolidated=False)      # the profile in docs/zarr-profile.md
```

`run.properties["cube:variables"]` says what else the run carries and in
which unit; `run.assets["manifest"].href` is the manifest under its `?v=`,
for a client that wants the shell's own view of the same run.

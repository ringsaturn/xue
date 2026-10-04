# Reading the catalog and the stores by hand

Everything is a plain file with open CORS under
`https://dataset.ringsaturn.me/xue/`. No API, no key, no listing: you
must know a path or follow links.

## Documents

```
catalog.json                       root; `child` links to every Collection
<source>/collection.json           one per source; `xue:live` names the run, `item` -> item.json
<source>/item.json                 the live run's Item (stable path; hrefs are ../<source>.<run>/…)
<source>.<run>/item.json           the run's own Item, beside manifest.json (gone when superseded)
<source>.<run>/<HHMM>/item.json    one round of a rolling window (mrms, jma, cma, satellites)
<product>/item.json                sounding / airport / tc live Item; assets `index` + the data file(s)
showcase/collection.json           historical cases; showcase/<case>/item.json
latest.json, latest-<source>.json  the raw live pointer the viewer polls (run, runTime, manifestPath, manifestCrc32)
```

Fetch `catalog.json`, `collection.json`, `item.json` and the pointers with
caching disabled (they change under the same URL). Everything under a run
directory is immutable and versioned by `?v=<crc32>`.

Item fields to read: `id`, `bbox`, `properties.datetime` /
`start_datetime` / `end_datetime`, `forecast:reference_datetime` (the
cycle), `forecast:horizon`, `xue:observation`, `xue:frameCount`,
`cube:dimensions` (x/y step and extent), `cube:variables` (every array with
label and unit; a vector component carries `xue:bundle`), and `assets`
(`xue:kind` = `store` | `poster` | `video` | `manifest`, `xue:tier` = `full`
| `half` | `quarter` | `eighth`, `file:size`). Use the `full` tier for
numbers; a `half`/`quarter` tier is the same codes subsampled and is enough
for a whole-hemisphere overview at a quarter of the bytes.

## Stores

A bundle is a Zarr v3 group `<bundle>.zarr/` with inline consolidated
metadata, so `xarray.open_zarr(url, consolidated=True)` opens it over HTTP
with no listing. Requirements: `xarray zarr>=3 fsspec aiohttp`. Facts:

- dims `(time, latitude, longitude)`; latitude runs **north to south**, so a
  latitude slice is `slice(north, south)`.
- `time` is a datetime64 coordinate (seconds since the run). Forecast
  frames are the valid times; `.sel(time=T, method="nearest")` after
  parsing T to `numpy.datetime64` (a bare string fails on a nearest lookup).
- Linear codebooks come back dequantized (xarray applies
  `scale_factor`/`add_offset`, `_FillValue` → NaN). The **log1p codebook**
  (`prate`) has no CF spelling and comes back as raw codes: decode with
  `p = scale·(exp(lo + (q−1)/span·(hi−lo)) − 1)`, `lo = ln(1+trace/scale)`,
  `hi = ln(1+maximum/scale)`, `span = maximumCode−1`, code 0 = 0 mm/h.
  `xue_stac.open_store` does both.
- One shard per array; reading one cell's series costs one range request
  per 6-frame time chunk (27 for GFS). Reading one frame over a region
  costs one request per tile row. Reading the whole globe for every frame
  of a bundle is 40 MB; do not do it for more than a couple of bundles.
- The group's `attributes.xue` is the bundle metadata: `grid`, `time`
  (`unitSeconds`, `frameOffsets`), `variables[]` with `parameter` (GRIB2
  discipline/category/number/surface) and `quantization`.
- Satellite disks that cross the antimeridian carry longitudes above 180
  (`80.72 … 200.68`); HRRR's grid is a rectangle whose off-domain corners
  repeat the edge.

With pystac:

```python
import pystac, xarray as xr
cat = pystac.Catalog.from_file("https://dataset.ringsaturn.me/xue/catalog.json")
item = next(cat.get_child("ecmwf").get_items())
ds = xr.open_zarr(item.assets["hgt500"].get_absolute_href(), consolidated=True)
```

## Point products by Range

```python
idx = requests.get(f"{root}sounding/item.json") …assets["index"]["href"]  # then the index JSON
line = requests.get(soundings_url, headers={"Range": f"bytes={offset}-{offset+length-1}"}).json()
```

`xue_stac.py sounding <wmo|wigos>`, `airport <ICAO>`, `storm <id>` and the
`nearest_*` helpers wrap this. A 206 answers the exact span; a 200 (a proxy
that ignored Range) must be sliced at the same offsets.

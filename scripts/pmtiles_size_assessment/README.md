# PMTiles size baseline

Backs the "Per-frame raster tiles" row of the size table in the main
[README](../../README.md). For each variable and forecast hour of run
`gfs.2026081506`, `measure.py` builds one frame the way a tile-pyramid
delivery would — select the GRIB band, convert to display units, warp
bilinearly to EPSG:3857 at 4096 × 4096, bake in a fixed color palette
with alpha, encode a PNG8 MBTiles pyramid (zoom 0–4), convert to
PMTiles — then records the file size and deletes everything. The byte
totals are the only output.

Recorded full-run result (2 variables × 121 hours = 242 files):

| Quantity | Bytes | Decimal MB |
|---|---:|---:|
| 242 per-frame PMTiles | 3,205,872,356 | 3,205.87 |
| Source GRIB2 (121 files) | 137,726,395 | 137.73 |

PNG encoding is not bit-stable across GDAL releases, so a re-run may
differ from the recorded total by around a percent; the magnitude is
what the measurement establishes.

Run it (needs the GDAL CLI tools and the
[pmtiles CLI](https://github.com/protomaps/go-pmtiles)):

```sh
python -m xue fetch --run 2026081506
python scripts/pmtiles_size_assessment/measure.py        # 6-hour sample, extrapolated
python scripts/pmtiles_size_assessment/measure.py --all  # exact 242-file total
```

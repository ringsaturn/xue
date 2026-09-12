# GRIB integration fixture

`gfs.2026081406.f000.crop.grib2` is an 80 by 80 cell crop of every record
the GFS source fetches from the 2026-08-14 06:00 UTC analysis: 2 m
temperature, surface precipitation rate, the 10 m wind pair, mean sea level
pressure, the 850 / 700 / 500 / 250 hPa geopotential heights, 925, 850 and
500 hPa temperature, 850, 700 and 500 hPa relative humidity, 850 hPa
specific humidity, the 925, 850 and 250 hPa wind pairs, then the surface
diagnostics (gust, total / low / middle / high cloud cover, CAPE,
visibility, 2 m dew point and apparent temperature), the 850 / 700 /
500 hPa vertical velocity, the surface temperature and the two sea ice
fields, and — appended by the fetcher from the cycle's GFS-Wave file — the
significant wave height, primary wave period and direction: forty records,
in the order `xuebuild/sources.py` lists them. It covers
approximately 118E to 138E and 18N to 38N, including the browser test's
initial viewport, so the record matchers, the GRIB2 header index, the vapour
flux derivation and the byte-identity parity test all run against real
records. About a sixth of the window is land, which the wave records leave
to their bitmap, so the nodata fill runs against real masked points too.

The fixture was recut from the downloaded analysis with:

```sh
python -m xuebuild fetch --run 2026081406 --hours 0 --raw-dir /tmp/raw
gdal_translate -srcwin 1192 208 80 80 -of GRIB \
  /tmp/raw/gfs.2026081406/gfs.2026081406.f000.grib2 \
  tests/fixtures/gfs.2026081406.f000.crop.grib2
```

Widening the GFS input list means recutting it the same way, so the parity
test keeps building the full published set.

`gfswave.2026091100.f000.jp2.crop.grib2` is the same window of the three
GFS-Wave records of the 2026-09-11 00:00 UTC analysis, packed the way
WAVEWATCH III publishes them: JPEG 2000 (DRS template 5.40) with a bitmap
over land. `gdal_translate` re-encodes what it crops and cannot write a
bitmap into a JPEG 2000 record, so the crop fixture above never carried
that packing; this one holds the wheel's GDAL — built with OpenJPEG for
exactly this — to the reference GDAL on the packing as published, in
`tests/test_native.py` and the wheel's release smoke test. Cut with:

```sh
# the three records, by byte range off the GFS-Wave .idx
curl -r <HTSGW offset>-<WVHGT offset - 1> \
  https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260911/00/wave/gridded/gfswave.t00z.global.0p25.f000.grib2 \
  -o /tmp/wave3.grib2
gdal_translate -srcwin 1192 208 80 80 -of GRIB /tmp/wave3.grib2 /tmp/wave.crop.grib2
grib_set -r -s packingType=grid_jpeg /tmp/wave.crop.grib2 \
  tests/fixtures/gfswave.2026091100.f000.jp2.crop.grib2
```

# Xue fixtures

`tests/prepare_bin_fixture.py` encodes the same cropped GRIB into per-variable
bundles `tests/fixtures/generated/tmp2m.xue` and `generated/prate.xue` plus
Python-decoded golden planes (`expected.<variable>.f000.bin`). The Rust tests
in `rust/xue/tests/golden.rs` and the browser tests in
`rust/xue-wasm/tests/web.rs` compare their decode output byte for byte
against those planes.

`tests/prepare_web_fixture.py` builds synthetic 121-frame per-variable bundles
on a coarse 144x73 global grid (`generated/web/tmp2m.xue`,
`generated/web/prate.xue`, half-resolution `<variable>.half.xue` variants,
posters, and a matching schema v5 `manifest.json` + `latest.json`) so
Playwright can exercise the full download, checksum, decode, on-demand variable
loading, and playback path with tiny payloads. It also builds a second,
independent ECMWF-identity dataset (`generated/web/ecmwf/` with 3-hourly
41-frame scalar bundles and `generated/web/latest-ecmwf.json`) so the model
switcher can be exercised end to end. Playwright global setup runs it
automatically. All generated files stay in ignored directories.

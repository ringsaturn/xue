# GRIB integration fixture

`gfs.2026081406.f000.crop.grib2` is an 80 by 80 cell crop of every record
the GFS source fetches from the 2026-08-14 06:00 UTC analysis: 2 m
temperature, surface precipitation rate, the 10 m wind pair, mean sea level
pressure, the 850 / 500 / 250 hPa geopotential heights, 850 and 500 hPa
temperature, 850 and 700 hPa relative humidity, 850 hPa specific humidity and
the 850 hPa wind pair — fifteen records, in the order `xuebuild/sources.py`
lists them. It covers approximately 118E to 138E and 18N to 38N, including
the browser test's initial viewport, so the record matchers, the GRIB2
header index, the vapour flux derivation and the byte-identity parity test
all run against real records.

The fixture was recut from the downloaded analysis with:

```sh
python -m xuebuild fetch --run 2026081406 --hours 0 --raw-dir /tmp/raw
gdal_translate -srcwin 1192 208 80 80 -of GRIB \
  /tmp/raw/gfs.2026081406/gfs.2026081406.f000.grib2 \
  tests/fixtures/gfs.2026081406.f000.crop.grib2
```

Widening the GFS input list means recutting it the same way, so the parity
test keeps building the full published set.

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

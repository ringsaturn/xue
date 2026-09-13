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

`hrrr.2026091100.f000.crop.grib2` is a 120 by 120 cell window of every
record the HRRR source fetches from the 2026-09-11 00:00 UTC analysis,
twenty-six records in `xuebuild/sources.py` order, over the Gulf coast
(roughly 88W to 84W, 27N to 31N) and still on the model's own 3 km Lambert
conformal grid — a crop keeps the projection, and GDAL's GRIB writer
carries it — so the WKT parsing, the footprint, the resampling onto the
regular 0.03° grid, the `MSLMA` / `REFC` aliases and the byte-identity
parity test all run against real projected records. Complex packing keeps
it a third of a megabyte. Cut from the records assembled by byte range off
the `.idx` (the order and phrases are the source's) with:

```sh
gdal_translate -srcwin 1220 800 120 120 -of GRIB -co DATA_ENCODING=COMPLEX_PACKING \
  /tmp/hrrr.f00.grib2 tests/fixtures/hrrr.2026091100.f000.crop.grib2
```

`ecmwf.2026091212.f000.crop.grib2` and `ecmwf.2026091212.f003.crop.grib2`
are an 80 by 80 cell window of the analysis and the first step of the
2026-09-12 12:00 UTC ECMWF open data cycle, every record the ECMWF source
fetches in `xuebuild/sources.py` order — the `oper` stream's, then the
three `wave` stream records the fetcher appends: thirty-three records at
the analysis, which carries no gust (the interval maximum is empty there
and the source lists it as optional), thirty-four at F003. The window is
the Kara Sea, 60E to 80E and 65N to 85N: sea ice (the ice thickness
carries a bitmap over land), open water (the wave records' bitmap covers
the ice and the land) and Novaya Zemlya and Yamal, so every fill rule meets
real masked points, and the records that arrive under another identity
than GFS's — the template-4.8 gust on the 10 m surface, the cloud cover as
ECMWF-local 0/6/192 in a 0–1 fraction, the most-unstable CAPE on surface
type 17, the skin temperature as 0/0/17, the peak wave period and mean
wave direction under 10/0/34 and 10/0/14 — are matched by both matchers
and built by both encoders (`tests/test_ecmwf.py`). The window sits away
from the GFS crop's because the sea ice thickness is all bitmap there,
which GDAL's GRIB writer cannot pack. Cut, after the fetcher had assembled
and repacked the frames, with:

```sh
python -m xuebuild fetch --model ecmwf --run 2026091212 --hours 3 --raw-dir /tmp/raw
for h in 000 003; do
  gdal_translate -srcwin 960 20 80 80 -of GRIB -co DATA_ENCODING=COMPLEX_PACKING \
    /tmp/raw/ecmwf.2026091212/ecmwf.2026091212.f$h.grib2 \
    tests/fixtures/ecmwf.2026091212.f$h.crop.grib2
done
```

`mrms.2026091300.t0000.crop.grib2` and `mrms.2026091300.t0002.crop.grib2`
are a 160 by 160 cell window of two consecutive frames of the MRMS mosaic,
each the composite reflectivity (`MergedReflectivityQCComposite_00.50`,
stamped 2026-09-13 00:00:42 and 00:02:42) followed by the precipitation
rate (`PrecipRate_00.00`, stamped 00:00 and 00:02) — two messages per
file, in the source's input order, the way the fetcher assembles a frame
from the two products' objects; the names are the fetcher's, the slot's
offset from the run (`t<HHMM>`). The window is the James Bay shore in
Quebec (77.6W to 76W, 51.8N to 50.2N), at the edge of the Canadian radar
coverage on a night with a rain band on the coast: about a quarter of it
is outside coverage (`-999`), half inside with no echo (`-99`), and a
quarter echo to 39.5 dBZ, so every sentinel rule, the block-maximum
thinning (160 cells become 80, two by two tiles at 64), the slot snapping
and the byte-identity parity test all run against real records. PNG
packing (DRS template 5.41), as MRMS publishes — which is what holds the
wheel's GDAL to the reference on that packing. Cut, from the four objects
gunzipped, with:

```sh
for f in MRMS_MergedReflectivityQCComposite_00.50_20260913-000042 \
         MRMS_MergedReflectivityQCComposite_00.50_20260913-000242 \
         MRMS_PrecipRate_00.00_20260913-000000 MRMS_PrecipRate_00.00_20260913-000200; do
  gdal_translate -srcwin 5240 320 160 160 -of GRIB -co DATA_ENCODING=PNG $f.grib2 crop.$f.grib2
done
cat crop.MRMS_MergedReflectivityQCComposite_00.50_20260913-000042.grib2 \
    crop.MRMS_PrecipRate_00.00_20260913-000000.grib2 > tests/fixtures/mrms.2026091300.t0000.crop.grib2
cat crop.MRMS_MergedReflectivityQCComposite_00.50_20260913-000242.grib2 \
    crop.MRMS_PrecipRate_00.00_20260913-000200.grib2 > tests/fixtures/mrms.2026091300.t0002.crop.grib2
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

# Tropical cyclone fixture

`tc/tc.2026091206/` is one issue hour's raw directory as `xue tc-build`
fetches it (`xuebuild/tc/fetch.py`), one subdirectory per source with the
`fetch.json` the fetcher leaves: JTWC's warning for 14E (Norbert) and the
formation alert for invest 97E, taken 2026-09-12 ~07 UTC; NHC's
`CurrentStorms.json`, the `EP142026` b-deck cut at the 2026-09-12 00Z
line and the a-deck cut to the `OFCL` / `AVNO` / `AEMN` / `EMXI` / `CARQ`
lines of the 2026-09-11 18Z and 2026-09-12 00Z bases (so "the newest
base wins" has two to choose from); the NCEP tracker's `avno` file of the
2026-09-12 00Z cycle and four of the GEFS member files (`ac00`, `aemn`,
`ap01`, `ap02`) of the same cycle; ECMWF's 2026-09-12 00Z `oper` `tf`
BUFR cut to `14E` and `70W` and the 2026-09-11 00Z `enfo` file cut to
`14E` (51 subsets, compressed), both with `bufr_filter`; and the IBTrACS
active list cut to Norbert and one other system. About 180 KB.

`tc/expected/` is the product built from it, offline, pretty-printed
with scalar arrays kept on one line; `tests/prepare_tc_golden.py`
regenerates it after a deliberate change, and `tests/test_tc.py` holds
the build to it (skipped without `bufr_dump`). `tc-registry.json` pins
the agency / model registry (`xuebuild/tc/registry.py`) the frontend's
table is held to.

The BUFR crops were cut with:

```sh
printf 'set unpack=1;\nif (stormIdentifier is "14E" || stormIdentifier is "70W") { write; }\n' > sel.filter
bufr_filter -o 2026091200-oper-tf.bufr sel.filter 20260912000000-360h-oper-tf.bufr
```

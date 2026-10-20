# River fixtures

Synthetic inputs for `tests/test_river.py`; nothing here comes from a real
network or gauge.

| File | What |
|---|---|
| `reaches.json` | 20 reaches as three-point WGS84 LineStrings on a 5 × 4 grid near 99° W, 30° N, with `feature_id` (ascending, 1000 + 3i), stream `order` and one `gage` (row 7) |
| `gauge.csv`, `gauge.stage.csv` | a `time,value` discharge series (one gap) and a stage series on partly different times, the work directory's gauge CSV format |
| `outflow.csv` | a second series, carried as a grid gauge's `outflow` |
| `extent.geojson` | one flooded-area polygon |

The test assembles a work directory from them in a temporary directory —
the GeoParquet (with pyarrow), deterministic `frames/*.npy` and
`static/baseflow.npy`, and a `case.json` in the shape the case adapters
write — and builds a `river/` directory through `xuebuild.river.build`.
Edit the files only on purpose; the expected values in the test follow
from them.

## `network/`: committed network stores

`network/delta.zarr` and `network/plain.zarr` are the synthetic track of
`tests/test_river.py` (8 hourly frames from runTime 2025-07-04T00:00:00Z at
offset 1, 20 reaches, `rowGroupRows` 8, so 2 × 3 inner chunks with padding
along both axes) written by the reference writer under the delta chain and
under `[bytes, zstd]`. They are committed, about 9 KB each, for the Rust
decoder's golden test (`rust/xue/tests/network.rs`), which holds every
frame and several series to the codes below. Regenerate them only on
purpose:

    .venv/bin/python tests/fixtures/river/network/prepare.py

The geometry block names a 35-byte stand-in `reaches.parquet` that is not
committed; no reader of these stores opens it. The side arrays hold
`feature_id` 1000 + 3i, `time` 1751590800 + 3600i (2025-07-04T01:00Z on)
and `baseflow` evenly spaced 0.1 to 2.0 m³/s with reach 13 `NaN`.

Codes per frame (rows) and reach (columns 0–19): a flood wave down the
reaches; reach 13 is a lake (255, no value, throughout), reach 4 is dry at
frame 2 (0) and reach 9 is past the codebook's maximum at frame 5 (254).
Under the log1p codebook code 10 is 0.523 m³/s and code 150 is 907.07 m³/s.

| frame | codes |
|---|---|
| 0 | 150 149 148 146 143 138 133 128 121 113 104 95 85 255 62 51 39 29 21 16 |
| 1 | 143 146 148 149 150 149 148 146 143 138 133 128 121 255 104 95 85 74 62 51 |
| 2 | 121 128 133 138 0 146 148 149 150 149 148 146 143 255 133 128 121 113 104 95 |
| 3 | 85 95 104 113 121 128 133 138 143 146 148 149 150 255 148 146 143 138 133 128 |
| 4 | 39 51 62 74 85 95 104 113 121 128 133 138 143 255 148 149 150 149 148 146 |
| 5 | 13 16 21 29 39 51 62 74 85 254 104 113 121 255 133 138 143 146 148 149 |
| 6 | 10 10 10 11 13 16 21 29 39 51 62 74 85 255 104 113 121 128 133 138 |
| 7 | 10 10 10 10 10 10 10 11 13 16 21 29 39 255 62 74 85 95 104 113 |

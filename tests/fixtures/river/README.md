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

"""Write the committed network stores the Rust decoder's golden test reads
(``rust/xue/tests/network.rs``): the synthetic 8-frame, 20-reach track of
``tests/test_river.py`` through the reference writer, once under the delta
chain and once under ``[bytes, zstd]``, and print the codes the README
lists.

    .venv/bin/python tests/fixtures/river/network/prepare.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3]))

import numpy as np  # noqa: E402

from tests.test_river import FEATURES, FRAMES, ROWS, RUN, synthetic_values  # noqa: E402
from xuebuild.river import network  # noqa: E402


def main() -> None:
    values = synthetic_values()
    ids = np.arange(FEATURES, dtype=np.int64) * 3 + 1000
    baseflow = np.linspace(0.1, 2.0, FEATURES, dtype=np.float32)
    baseflow[13] = np.nan
    times = [RUN + timedelta(hours=1 + i) for i in range(FRAMES)]
    with tempfile.TemporaryDirectory() as scratch:
        # The geometry is a stand-in: the decoder never opens the GeoParquet.
        parquet = Path(scratch) / "reaches.parquet"
        parquet.write_bytes(b"PAR1 stand-in for the geometry PAR1")
        geometry = network.geometry_descriptor(parquet, ROWS, FEATURES)
        for name, delta in (("delta", True), ("plain", False)):
            report = network.write_network_store(
                HERE / f"{name}.zarr",
                model="NWM v3.0",
                product="short_range",
                run_time=RUN,
                valid_times=times,
                values=values,
                feature_ids=ids,
                baseflow=baseflow,
                geometry=geometry,
                delta=delta,
            )
            print(f"{name}.zarr: {report.byte_length} bytes, crc32 {report.crc32}")
    codes = network.quantize(values)
    for frame, row in enumerate(codes):
        print(f"| {frame} | {' '.join(str(int(code)) for code in row)} |")


if __name__ == "__main__":
    main()

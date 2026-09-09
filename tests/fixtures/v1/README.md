# Frozen container v1 corpus

These bytes are committed, not generated. Nothing in the repository writes
container v1 any more — both encoders emit v2 — but published runs and
showcase cases on R2 carry v1 bytes and are never rebuilt, so **the decoders
must read v1 forever**. A format's compatibility promise is only as good as
the artifacts that hold it to account, and a promise about a layout no
encoder still produces cannot be tested by round-tripping.

| File | What it pins |
|---|---|
| `tmp2m.xue`, `prate.xue` | The reference encoder's v1 output for `tests/fixtures/gfs.2026081406.f000.crop.grib2`: an 80 x 80 regional grid, a single frame, both a residual-coded and a RAW variable. |
| `mixed.xue` | A synthetic mixed-step axis (hourly to f012, then three-hourly to f036) on a 16 x 8 grid, with ANCHOR groups that stop at the cadence change — the v1 grouping rule that v2 inherits. |
| `expected.*.bin` | The planes those files decode to, dumped by the Python reference reader at the time they were frozen. |

The `.xue` files and their `expected.*.bin` planes were frozen together, so
the pair stays self-consistent no matter what later changes to quantization
or grouping do to newly encoded data: the test asserts that *these* bytes
still decode to *these* planes.

Regenerating them is not a normal operation. If a change makes this corpus
fail, the change broke v1 reading — that is the finding, not a reason to
refresh the files.

Read by `rust/xue/tests/golden.rs` and `tests/test_bin.py`. The generated,
gitignored fixtures next door in `tests/fixtures/generated/` are v2 and are
rebuilt by `tests/prepare_bin_fixture.py`.

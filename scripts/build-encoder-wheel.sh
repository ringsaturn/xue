#!/usr/bin/env bash
# Build a self-contained wheel for the experimental native encoder.
#
# The wheel carries its own GDAL rather than linking whatever the machine has:
# see build-gdal-minimal.sh for why that GDAL is built from source with two
# drivers. Three things have to travel with the extension module:
#
#   * the shared libraries, copied in and re-pathed by delocate / auditwheel;
#   * GDAL's and PROJ's data directories — the GRIB driver cannot read a band
#     without grib2_center.csv, it reports zero matching records instead;
#   * the licence texts, since every bundled library is permissive but each
#     one asks for its notice to travel with a binary redistribution.
#
#   ./scripts/build-wheel.sh [output-dir]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="${1:-$ROOT/data/work/wheels}"
PREFIX="$ROOT/data/work/gdal-minimal"
PACKAGE="$ROOT/rust/xue-encode-py/pysrc/xue_encode_py"

if [ ! -f "$PREFIX/lib/pkgconfig/gdal.pc" ]; then
  echo "==> no minimal GDAL yet, building it"
  "$ROOT/scripts/build-gdal-minimal.sh" "$PREFIX"
fi

echo "==> staging GDAL and PROJ data into the package"
rm -rf "$PACKAGE/gdal-data" "$PACKAGE/proj-data" "$PACKAGE/licenses"
mkdir -p "$PACKAGE/gdal-data" "$PACKAGE/proj-data" "$PACKAGE/licenses"
cp -R "$PREFIX/share/gdal/." "$PACKAGE/gdal-data/"
# proj.db is the only PROJ datum PROJ needs to initialise; the optional grid
# shift files are a separate 500 MB download this encoder never reprojects with.
cp "$PREFIX/share/proj/proj.db" "$PACKAGE/proj-data/"

echo "==> collecting licence texts"
# build-gdal-minimal.sh stages these into the prefix so a cached prefix is
# enough; every bundled library is permissive but asks for its notice to
# travel with a binary redistribution.
cp "$PREFIX"/share/licenses/* "$PACKAGE/licenses/"
cp "$ROOT/LICENSE-MIT" "$PACKAGE/licenses/xue.LICENSE-MIT"
cp "$ROOT/LICENSE-APACHE" "$PACKAGE/licenses/xue.LICENSE-APACHE"
ls "$PACKAGE/licenses" | sed 's/^/    /'

echo "==> building the wheel"
mkdir -p "$OUTPUT"
rm -f "$OUTPUT"/*.whl
# maturin builds into a staging directory and the repair step writes the final
# wheel into $OUTPUT. Keeping them apart matters: repairing can change the
# platform tag — a library built for a newer OS than the deployment target
# drags it up — and then the unrepaired wheel would sit beside the repaired one
# under a different name.
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
cd "$ROOT/rust/xue-encode-py"
# build.rs adds the LC_RPATH that lets delocate resolve libgdal and vendor it.
PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" uvx maturin@1.9 build --profile encoder --out "$STAGING"

staged="$(ls "$STAGING"/*.whl)"
echo "==> vendoring the shared libraries into $(basename "$staged")"
case "$(uname -s)" in
  Darwin)
    # delocate follows the install names out of the extension module and
    # rewrites them to @loader_path, so the wheel needs no DYLD_ variables.
    uvx --from delocate delocate-wheel \
      --require-archs "$(uname -m)" --wheel-dir "$OUTPUT" "$staged"
    ;;
  Linux)
    # No --plat: auditwheel picks the lowest policy the binaries actually
    # satisfy, which is the glibc of whatever built them. Naming a lower one
    # only makes it refuse, since the symbols are already there. The wheel
    # therefore carries the builder's glibc floor — on ubuntu-24.04 that is
    # manylinux_2_39, which is fine for this project's own CI. Building inside
    # a manylinux_2_28 container is what a widely installable wheel would need.
    uvx --from auditwheel auditwheel repair --wheel-dir "$OUTPUT" "$staged"
    ;;
  *) echo "unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac

wheel="$(ls "$OUTPUT"/*.whl)"
echo
echo "$wheel"
ls -lh "$wheel" | awk '{print "    compressed: " $5}'
python3 - "$wheel" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    entries = archive.infolist()
    total = sum(entry.file_size for entry in entries)
    print(f"    uncompressed: {total / 1e6:.1f} MB in {len(entries)} files")
    for entry in sorted(entries, key=lambda item: -item.file_size)[:8]:
        print(f"      {entry.file_size / 1e6:7.1f} MB  {entry.filename}")
PY

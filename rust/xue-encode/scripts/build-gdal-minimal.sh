#!/usr/bin/env bash
# Build the smallest GDAL that can read what this encoder reads, into a
# self-contained prefix a wheel can bundle.
#
# A distribution-grade GDAL (the one a package manager installs) links Arrow,
# TileDB, OpenBLAS, Poppler, x265 and about 220 other libraries — 318 MB, and
# several of those are GPL or LGPL, which a redistributed binary wheel would
# have to answer for. The encoder needs exactly two drivers, GRIB and netCDF,
# and no OGR at all. Everything else is switched off here.
#
#   ./scripts/build-gdal-minimal.sh [prefix]
#
# Writes to $prefix (default: build/gdal-minimal) and prints the pkg-config
# path to export before building the crate. zlib and sqlite3 come from the
# macOS SDK, which every target already has; they are not bundled.
set -euo pipefail

PREFIX="${1:-$(cd "$(dirname "$0")/.." && pwd)/build/gdal-minimal}"
# Sources sit beside the prefix, not inside it: CI caches the prefix, and the
# unpacked trees are an order of magnitude larger than what they install.
# build-wheel.sh reads the licence texts back out of here.
WORK="${XUE_GDAL_SOURCES:-$(dirname "$PREFIX")/gdal-sources}"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
# The libraries find each other beside themselves, both in the prefix and once
# delocate/auditwheel has moved them into the wheel.
case "$(uname -s)" in
  Darwin) ORIGIN_RPATH="@loader_path" ;;
  *) ORIGIN_RPATH="\$ORIGIN" ;;
esac

# Pinned to the versions the byte-identity comparison was run against; a GDAL
# version bump can change GRIB band metadata, so it is a deliberate step.
AEC_VERSION=1.1.3
HDF5_VERSION=1.14.6
NETCDF_VERSION=4.9.3
PROJ_VERSION=9.8.1
GDAL_VERSION=3.13.1

mkdir -p "$WORK" "$PREFIX"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export CMAKE_PREFIX_PATH="$PREFIX"

fetch() { # url sha-less: these are release tarballs from the projects themselves
  local url="$1" name="${1##*/}"
  [ -f "$WORK/$name" ] || curl --fail --location --silent --show-error --output "$WORK/$name" "$url"
  local dir="$WORK/${2}"
  [ -d "$dir" ] || tar -xzf "$WORK/$name" -C "$WORK"
  echo "$dir"
}

cmake_build() { # marker source-dir extra-args...
  local marker="$1" source="$2"; shift 2
  # Reruns are common while tuning the driver set, and each of these takes
  # minutes; skip what is already installed into the prefix.
  if [ -e "$PREFIX/$marker" ]; then echo "    already installed, skipping"; return; fi
  cmake -S "$source" -B "$source/build-min" \
    -DCMAKE_BUILD_TYPE=Release \
    `# A developer machine usually has a second copy of these libraries from a
     # package manager, and CMake will happily compile against its headers
     # while linking the ones built here. That produced a netCDF built against
     # HDF5 2.1 headers over HDF5 1.14, which fails at run time with
     # "H5Pset_libver_bounds(): high bound is not valid" — an enum value that
     # only exists in the newer library. Only this prefix and the platform SDK
     # are in scope.` \
    -DCMAKE_IGNORE_PREFIX_PATH="${CMAKE_IGNORE_PREFIX_PATH:-/opt/homebrew;/usr/local;/opt/local}" \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_RPATH="$ORIGIN_RPATH" \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DBUILD_TESTING=OFF \
    "$@" >/dev/null
  cmake --build "$source/build-min" --parallel "$JOBS" >/dev/null
  cmake --install "$source/build-min" >/dev/null
}

echo "==> libaec ${AEC_VERSION} (CCSDS packing, GRIB template 5.42)"
cmake_build include/libaec.h "$(fetch "https://github.com/MathisRosenhauer/libaec/releases/download/v${AEC_VERSION}/libaec-${AEC_VERSION}.tar.gz" "libaec-${AEC_VERSION}")" \
  -DBUILD_SHARED_LIBS=ON

echo "==> HDF5 ${HDF5_VERSION} (under netCDF)"
cmake_build include/hdf5.h "$(fetch "https://github.com/HDFGroup/hdf5/releases/download/hdf5_${HDF5_VERSION}/hdf5-${HDF5_VERSION}.tar.gz" "hdf5-${HDF5_VERSION}")" \
  -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=OFF \
  -DHDF5_BUILD_TOOLS=OFF -DHDF5_BUILD_EXAMPLES=OFF -DHDF5_BUILD_UTILS=OFF \
  -DHDF5_BUILD_CPP_LIB=OFF -DHDF5_BUILD_HL_LIB=ON -DHDF5_ENABLE_Z_LIB_SUPPORT=ON

echo "==> netCDF ${NETCDF_VERSION} (the observation source's format)"
cmake_build include/netcdf.h "$(fetch "https://github.com/Unidata/netcdf-c/archive/refs/tags/v${NETCDF_VERSION}.tar.gz" "netcdf-c-${NETCDF_VERSION}")" \
  -DBUILD_SHARED_LIBS=ON -DENABLE_DAP=OFF -DENABLE_NCZARR=OFF \
  -DENABLE_BYTERANGE=OFF -DENABLE_LIBXML2=OFF -DENABLE_PLUGINS=OFF \
  -DBUILD_UTILITIES=OFF -DENABLE_EXAMPLES=OFF -DENABLE_TESTS=OFF \
  `# The observation files use HDF5's own shuffle and deflate; the optional
   # filter libraries would only add dependencies to vendor.` \
  -DENABLE_FILTER_SZIP=OFF -DENABLE_FILTER_BZ2=OFF -DENABLE_FILTER_ZSTD=OFF

echo "==> PROJ ${PROJ_VERSION} (mandatory for GDAL >= 3, never used for reprojection here)"
cmake_build lib/pkgconfig/proj.pc "$(fetch "https://download.osgeo.org/proj/proj-${PROJ_VERSION}.tar.gz" "proj-${PROJ_VERSION}")" \
  -DBUILD_SHARED_LIBS=ON -DBUILD_APPS=OFF -DENABLE_TIFF=OFF -DENABLE_CURL=OFF \
  -DBUILD_PROJSYNC=OFF -DBUILD_EXAMPLES=OFF

echo "==> GDAL ${GDAL_VERSION}, GRIB and netCDF only"
cmake_build lib/pkgconfig/gdal.pc "$(fetch "https://github.com/OSGeo/gdal/releases/download/v${GDAL_VERSION}/gdal-${GDAL_VERSION}.tar.gz" "gdal-${GDAL_VERSION}")" \
  -DBUILD_SHARED_LIBS=ON \
  -DGDAL_BUILD_OPTIONAL_DRIVERS=OFF \
  -DOGR_BUILD_OPTIONAL_DRIVERS=OFF \
  -DGDAL_ENABLE_DRIVER_GRIB=ON \
  -DGDAL_ENABLE_DRIVER_NETCDF=ON \
  `# Enumerating the codecs to switch off does not work: GDAL probes several
   # of them through pkg-config, which has its own search path, and a machine
   # with Homebrew ends up linking libjxl and Brotli into a build that has no
   # driver able to use them. The master switch turns every optional external
   # dependency off, and the two the encoder needs come back on by name.` \
  -DGDAL_USE_EXTERNAL_LIBS=OFF \
  -DGDAL_USE_INTERNAL_LIBS=ON \
  -DGDAL_USE_NETCDF=ON \
  `# GRIB template 5.42 is CCSDS-packed. The reference GDAL can read it, and
   # ECMWF open data arrives that way before the fetcher repacks it, so the
   # two must not differ here.` \
  -DGDAL_USE_LIBAEC=ON \
  `# GTiff is a core driver and always builds. Left to itself GDAL detects a
   # system libjpeg-turbo, sees its dual 8/12-bit mode, and then compiles
   # libtiff's 12-bit path against the *internal* 8-bit headers, which does
   # not compile. Internal-only keeps that combination from arising.` \
  -DGDAL_USE_JPEG=OFF -DGDAL_USE_JPEG_INTERNAL=ON -DGDAL_USE_JPEG12_INTERNAL=OFF \
  -DBUILD_APPS=OFF -DBUILD_PYTHON_BINDINGS=OFF -DBUILD_CSHARP_BINDINGS=OFF \
  -DBUILD_JAVA_BINDINGS=OFF -DBUILD_DOCS=OFF

# The licence texts belong to the prefix, not to the unpacked sources: CI
# caches the prefix and throws the sources away, and a binary redistribution
# has to carry these.
echo "==> collecting licence texts into the prefix"
mkdir -p "$PREFIX/share/licenses"
for source in "$WORK"/*/; do
  name="$(basename "$source")"
  for licence in "$source"LICENSE* "$source"COPYING* "$source"COPYRIGHT*; do
    [ -f "$licence" ] || continue
    cp "$licence" "$PREFIX/share/licenses/${name}.$(basename "$licence")"
  done
done

echo
echo "prefix: $PREFIX"
du -sh "$PREFIX/lib" "$PREFIX/share" 2>/dev/null || true
echo
echo "build the crate against it with:"
echo "  export PKG_CONFIG_PATH=\"$PREFIX/lib/pkgconfig\""

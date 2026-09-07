// This crate always enables the encoder feature on xue, so it always links
// GDAL and always needs the rpath.
include!("../xue/gdal_rpath.rs");

fn main() {
    emit_gdal_rpath();
}

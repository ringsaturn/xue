include!("gdal_rpath.rs");

fn main() {
    // A decode-only build links no GDAL and must not need pkg-config to find
    // one — that includes the wasm binding and every downstream consumer of
    // the published crate.
    if std::env::var_os("CARGO_FEATURE_ENCODER").is_some() {
        emit_gdal_rpath();
    }
}

// Give a binary an rpath to the GDAL it was linked against.
//
// `gdal-sys` resolves GDAL through pkg-config, which is fine for a system
// install but not for the private prefix `scripts/build-gdal-minimal.sh`
// creates: libgdal's install name is `@rpath/libgdal.NN.dylib`, so without
// this the CLI and the tests abort at startup with "Library not loaded".
//
// The Python extension module gets the same rpath, which is what lets
// `delocate` (macOS) and `auditwheel` (Linux) resolve the dependency and
// vendor it into the wheel; both strip the rpath afterwards.
//
// Included by two build scripts rather than being one, because a build
// script's link arguments do not propagate to a dependent crate.
fn emit_gdal_rpath() {
    println!("cargo:rerun-if-env-changed=PKG_CONFIG_PATH");
    let Ok(output) = std::process::Command::new("pkg-config")
        .args(["--variable=libdir", "gdal"])
        .output()
    else {
        return;
    };
    let Ok(libdir) = String::from_utf8(output.stdout) else {
        return;
    };
    let libdir = libdir.trim();
    if output.status.success() && !libdir.is_empty() {
        println!("cargo:rustc-link-arg=-Wl,-rpath,{libdir}");
    }
}

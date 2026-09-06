// The extension module needs the same rpath the core crate's build script
// adds, and a build script's link arguments do not propagate to a dependent
// crate — so the logic is shared by inclusion rather than duplicated.
include!("../build.rs");

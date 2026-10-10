//! The IANA time zone of a point, from the `tzf-rs` polygon index embedded
//! in the Worker. The finder is opened once per isolate over the bytes in
//! the wasm's data segment (no copy, no decode), so the first lookup costs
//! the file's validation pass and every later one a few microseconds.

use std::cell::OnceCell;

use tzf_rs::EmbeddedFinder;

thread_local! {
    static FINDER: OnceCell<EmbeddedFinder> = const { OnceCell::new() };
}

/// The zone covering `(lon, lat)`: a nautical `Etc/GMT±N` on open sea, `None`
/// only where no polygon covers the point (the bundled file leaves no gap,
/// so this is defensive). A longitude past 180 is wrapped the way the grid
/// probe wraps it.
pub fn name(lon: f64, lat: f64) -> Option<String> {
    let lon = if lon > 180.0 { lon - 360.0 } else { lon };
    FINDER.with(|cell| {
        let finder = cell.get_or_init(EmbeddedFinder::new);
        let name = finder.get_tz_name(lon, lat);
        (!name.is_empty()).then(|| name.to_owned())
    })
}

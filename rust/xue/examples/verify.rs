//! Open a bundle, decode every plane, and report per-frame decode timings.
//!
//! Usage: cargo run --release --example verify -- path/to/bundle.xue

use xue::{Bundle, FrameRequest};
use std::time::Instant;

fn main() {
    let path = std::env::args().nth(1).expect("usage: verify <bundle.xue>");
    let bytes = std::fs::read(&path).expect("read bundle");
    let open_start = Instant::now();
    let mut bundle = Bundle::open(&bytes).expect("bundle must validate");
    println!("opened {} bytes in {:?}", bytes.len(), open_start.elapsed());

    let metadata: serde_json::Value = serde_json::from_str(bundle.metadata_json()).unwrap();
    let first = metadata["time"]["firstForecastHour"].as_u64().unwrap() as u16;
    let step = metadata["time"]["stepHours"].as_u64().unwrap() as u16;
    let count = metadata["time"]["frameCount"].as_u64().unwrap() as u16;

    let mut timings_ms: Vec<f64> = Vec::new();
    for &variable_id in bundle.variable_ids().to_vec().iter() {
        for frame in 0..count {
            let hour = first + frame * step;
            let start = Instant::now();
            let plane = bundle
                .decode_frame(FrameRequest { variable_id, forecast_hour: hour })
                .expect("decode");
            let elapsed = start.elapsed().as_secs_f64() * 1000.0;
            timings_ms.push(elapsed);
            assert!(!plane.is_empty());
        }
        bundle.clear_cache();
    }
    timings_ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p = |q: f64| timings_ms[((timings_ms.len() - 1) as f64 * q) as usize];
    println!(
        "decoded {} planes: p50 {:.2} ms, p95 {:.2} ms, p99 {:.2} ms, max {:.2} ms",
        timings_ms.len(),
        p(0.50),
        p(0.95),
        p(0.99),
        timings_ms[timings_ms.len() - 1]
    );
}

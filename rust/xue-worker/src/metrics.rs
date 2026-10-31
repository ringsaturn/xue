//! Prometheus metrics for the native server (`xue-api-server`).
//!
//! Served on a listener of their own (`XUE_API_METRICS_LISTEN`), never on the
//! API's: the tunnel forwards everything that reaches the API's address, so
//! a `/metrics` route there would be public. Off unless that variable is set.
//!
//! The runtime is one thread, so the registry is thread-local and every
//! update is a plain add. The text is the Prometheus exposition format 0.0.4,
//! written by hand: a handful of counters and histograms do not need a crate.
//! Labels are bounded: a route is its template (`/v1/soundings/{station}`),
//! never the path a client sent.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fmt::Write;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The commit the binary was built from, when the build said so
/// (`XUE_API_REVISION`, set by the build-api-server workflow).
pub const REVISION: &str = match option_env!("XUE_API_REVISION") {
    Some(revision) => revision,
    None => "unknown",
};

/// Upper bounds in seconds. A cache hit is under a millisecond; a cold
/// multi-bundle cell or a long CFS series takes seconds.
const BUCKETS: [f64; 12] = [
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

#[derive(Default)]
struct Histogram {
    counts: [u64; BUCKETS.len()],
    sum: f64,
    count: u64,
}

impl Histogram {
    fn observe(&mut self, seconds: f64) {
        for (bound, count) in BUCKETS.iter().zip(self.counts.iter_mut()) {
            if seconds <= *bound {
                *count += 1;
            }
        }
        self.sum += seconds;
        self.count += 1;
    }

    fn write(&self, out: &mut String, name: &str, labels: &str) {
        let sep = if labels.is_empty() { "" } else { "," };
        for (bound, count) in BUCKETS.iter().zip(self.counts.iter()) {
            let _ = writeln!(out, "{name}_bucket{{{labels}{sep}le=\"{bound}\"}} {count}");
        }
        let _ = writeln!(
            out,
            "{name}_bucket{{{labels}{sep}le=\"+Inf\"}} {}",
            self.count
        );
        let braces = |labels: &str| {
            if labels.is_empty() {
                String::new()
            } else {
                format!("{{{labels}}}")
            }
        };
        let _ = writeln!(out, "{name}_sum{} {}", braces(labels), self.sum);
        let _ = writeln!(out, "{name}_count{} {}", braces(labels), self.count);
    }
}

#[derive(Default)]
struct Registry {
    /// `(route, outcome, status)` → requests.
    requests: BTreeMap<(&'static str, &'static str, u16), u64>,
    /// `(route, outcome)` → seconds from request to response.
    durations: BTreeMap<(&'static str, &'static str), Histogram>,
    /// `route` → body bytes sent, after compression.
    sent: BTreeMap<&'static str, u64>,
    /// `(kind, result)` → reads of the dataset origin.
    upstream: BTreeMap<(&'static str, &'static str), u64>,
    upstream_seconds: Histogram,
    upstream_bytes: u64,
}

thread_local! {
    static REGISTRY: RefCell<Registry> = RefCell::new(Registry::default());
}

/// The route template a normalized path answers on: a bounded label.
pub fn route_label(path: &str) -> &'static str {
    const EXACT: [&str; 11] = [
        "/",
        "/health",
        "/openapi.json",
        "/v1/openapi.json",
        "/v1/catalog",
        "/v1/point",
        "/v1/cell",
        "/v1/soundings",
        "/v1/airports",
        "/v1/synop",
        "/v1/storms",
    ];
    if let Some(route) = EXACT.iter().find(|route| **route == path) {
        return route;
    }
    const PARAMETERIZED: [(&str, &str); 5] = [
        ("/v1/sources/", "/v1/sources/{source}"),
        ("/v1/soundings/", "/v1/soundings/{station}"),
        ("/v1/airports/", "/v1/airports/{icao}"),
        ("/v1/synop/", "/v1/synop/{station}"),
        ("/v1/storms/", "/v1/storms/{storm}"),
    ];
    for (prefix, route) in PARAMETERIZED {
        if path
            .strip_prefix(prefix)
            .is_some_and(|rest| !rest.is_empty() && !rest.contains('/'))
        {
            return route;
        }
    }
    if path == "/docs" || path.starts_with("/docs/") {
        return "/docs";
    }
    "other"
}

/// One answered request. `outcome` is the server's own (`hit`, `miss`,
/// `error`, `asset`, `preflight`).
pub fn request(
    route: &'static str,
    outcome: &'static str,
    status: u16,
    elapsed: Duration,
    bytes: usize,
) {
    REGISTRY.with(|registry| {
        let mut registry = registry.borrow_mut();
        *registry
            .requests
            .entry((route, outcome, status))
            .or_default() += 1;
        registry
            .durations
            .entry((route, outcome))
            .or_default()
            .observe(elapsed.as_secs_f64());
        *registry.sent.entry(route).or_default() += bytes as u64;
    });
}

/// One read of the dataset origin: `kind` `whole` or `range`, `result` `ok`,
/// `not_found` or `error`.
pub fn upstream(kind: &'static str, result: &'static str, elapsed: Duration, bytes: usize) {
    REGISTRY.with(|registry| {
        let mut registry = registry.borrow_mut();
        *registry.upstream.entry((kind, result)).or_default() += 1;
        registry.upstream_seconds.observe(elapsed.as_secs_f64());
        registry.upstream_bytes += bytes as u64;
    });
}

/// What the server samples at scrape time rather than counts.
pub struct Gauges {
    pub inflight: usize,
    pub inflight_limit: usize,
    pub responses_entries: usize,
    pub responses_bytes: usize,
    pub responses_limit_bytes: usize,
    pub rate_tokens: Option<f64>,
    pub started: SystemTime,
}

fn help(out: &mut String, name: &str, kind: &str, text: &str) {
    let _ = writeln!(out, "# HELP {name} {text}");
    let _ = writeln!(out, "# TYPE {name} {kind}");
}

/// The exposition text.
pub fn render(gauges: &Gauges) -> String {
    let mut out = String::with_capacity(16 * 1024);
    help(
        &mut out,
        "xue_api_build_info",
        "gauge",
        "The running build: crate version and source revision.",
    );
    let _ = writeln!(
        out,
        "xue_api_build_info{{version=\"{}\",revision=\"{REVISION}\"}} 1",
        env!("CARGO_PKG_VERSION")
    );
    help(
        &mut out,
        "xue_api_start_time_seconds",
        "gauge",
        "When the server started, seconds since the epoch.",
    );
    let started = gauges
        .started
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_secs_f64())
        .unwrap_or(0.0);
    let _ = writeln!(out, "xue_api_start_time_seconds {started}");

    REGISTRY.with(|registry| {
        let registry = registry.borrow();
        help(
            &mut out,
            "xue_api_requests_total",
            "counter",
            "Requests answered, by route template, outcome (hit, miss, error, asset, preflight) and status.",
        );
        for ((route, outcome, status), count) in &registry.requests {
            let _ = writeln!(
                out,
                "xue_api_requests_total{{route=\"{route}\",outcome=\"{outcome}\",status=\"{status}\"}} {count}"
            );
        }
        help(
            &mut out,
            "xue_api_request_duration_seconds",
            "histogram",
            "Time from request to response, by route template and outcome.",
        );
        for ((route, outcome), histogram) in &registry.durations {
            histogram.write(
                &mut out,
                "xue_api_request_duration_seconds",
                &format!("route=\"{route}\",outcome=\"{outcome}\""),
            );
        }
        help(
            &mut out,
            "xue_api_response_bytes_total",
            "counter",
            "Body bytes sent, after compression, by route template.",
        );
        for (route, bytes) in &registry.sent {
            let _ = writeln!(out, "xue_api_response_bytes_total{{route=\"{route}\"}} {bytes}");
        }
        help(
            &mut out,
            "xue_api_upstream_requests_total",
            "counter",
            "Reads of the dataset origin, by kind (whole, range) and result (ok, not_found, error).",
        );
        for ((kind, result), count) in &registry.upstream {
            let _ = writeln!(
                out,
                "xue_api_upstream_requests_total{{kind=\"{kind}\",result=\"{result}\"}} {count}"
            );
        }
        help(
            &mut out,
            "xue_api_upstream_duration_seconds",
            "histogram",
            "Time of one read of the dataset origin.",
        );
        registry
            .upstream_seconds
            .write(&mut out, "xue_api_upstream_duration_seconds", "");
        help(
            &mut out,
            "xue_api_upstream_bytes_total",
            "counter",
            "Body bytes read from the dataset origin.",
        );
        let _ = writeln!(out, "xue_api_upstream_bytes_total {}", registry.upstream_bytes);
    });

    let gauge = |out: &mut String, name: &str, text: &str, value: f64| {
        help(out, name, "gauge", text);
        let _ = writeln!(out, "{name} {value}");
    };
    gauge(
        &mut out,
        "xue_api_inflight",
        "Requests being computed now.",
        gauges.inflight as f64,
    );
    gauge(
        &mut out,
        "xue_api_inflight_limit",
        "Requests computed at once before 503 overloaded (XUE_API_INFLIGHT).",
        gauges.inflight_limit as f64,
    );
    gauge(
        &mut out,
        "xue_api_response_cache_entries",
        "Answers held in the response cache.",
        gauges.responses_entries as f64,
    );
    gauge(
        &mut out,
        "xue_api_response_cache_bytes",
        "Bytes held in the response cache.",
        gauges.responses_bytes as f64,
    );
    gauge(
        &mut out,
        "xue_api_response_cache_limit_bytes",
        "The response cache's limit (XUE_API_CACHE_MB).",
        gauges.responses_limit_bytes as f64,
    );
    if let Some(tokens) = gauges.rate_tokens {
        gauge(
            &mut out,
            "xue_api_rate_tokens",
            "Computed answers the rate budget (XUE_API_RATE_PER_MIN) allows right now.",
            tokens,
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_are_templates() {
        assert_eq!(route_label("/v1/cell"), "/v1/cell");
        assert_eq!(route_label("/v1/sources/gfs"), "/v1/sources/{source}");
        assert_eq!(route_label("/v1/synop/amedas:44132"), "/v1/synop/{station}");
        assert_eq!(route_label("/v1/sources/gfs/x"), "other");
        assert_eq!(route_label("/v1/sources/"), "other");
        assert_eq!(route_label("/docs/swagger-ui.css"), "/docs");
        assert_eq!(route_label("/wp-login.php"), "other");
    }

    #[test]
    fn renders_counters_and_histograms() {
        request("/v1/cell", "miss", 200, Duration::from_millis(30), 1200);
        request("/v1/cell", "hit", 200, Duration::from_micros(200), 1200);
        upstream("range", "ok", Duration::from_millis(20), 4096);
        let text = render(&Gauges {
            inflight: 1,
            inflight_limit: 32,
            responses_entries: 1,
            responses_bytes: 1200,
            responses_limit_bytes: 64 << 20,
            rate_tokens: None,
            started: UNIX_EPOCH,
        });
        assert!(text.contains(
            "xue_api_requests_total{route=\"/v1/cell\",outcome=\"miss\",status=\"200\"} 1"
        ));
        assert!(text.contains(
            "xue_api_request_duration_seconds_bucket{route=\"/v1/cell\",outcome=\"miss\",le=\"0.05\"} 1"
        ));
        assert!(text.contains(
            "xue_api_request_duration_seconds_bucket{route=\"/v1/cell\",outcome=\"miss\",le=\"0.025\"} 0"
        ));
        assert!(text.contains("xue_api_response_bytes_total{route=\"/v1/cell\"} 2400"));
        assert!(text.contains("xue_api_upstream_requests_total{kind=\"range\",result=\"ok\"} 1"));
        assert!(text.contains("xue_api_upstream_duration_seconds_count 1"));
        assert!(text.contains("xue_api_inflight_limit 32"));
        assert!(!text.contains("xue_api_rate_tokens"));
    }
}

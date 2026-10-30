//! The data API as a native HTTP server (`xue-api-server`), for a self-hosted
//! origin behind a Cloudflare Tunnel.
//!
//! Same routes and answers as the Worker (`route.rs`), read from the public
//! origin over HTTP (`Data::from_origin`) instead of an R2 binding, so a
//! request that misses every cache costs this machine's CPU and never a
//! metered read. Two differences in framing:
//!
//! * A response cache in place of `caches.default`: a `200` is kept for its
//!   `max-age`, and a hit is answered with the *remaining* lifetime, so the
//!   Cloudflare edge in front never holds an answer longer than the Worker's
//!   would have been held.
//! * A cap on requests being computed at once. Above it the server answers
//!   `503 overloaded` with `Retry-After` straight away; a retry is likely to
//!   find the answer cached at the edge or here.
//!
//! The runtime is one thread: the handlers, the immutable-object cache
//! (`cache.rs`) and the generated contract (`docs.rs`) are thread-local, as in
//! an isolate, and a 1-vCPU origin gains nothing from more.
//!
//! Under systemd it reports readiness and pings the watchdog (`watchdog`).
//! Configuration is the environment: `XUE_API_LISTEN` (default
//! `127.0.0.1:8787`; keep it on loopback, the tunnel connects from the same
//! machine), `DATA_ORIGIN` (default the public dataset), `XUE_API_INFLIGHT`
//! (default 32), `XUE_API_CACHE_MB` (default 64) and `XUE_API_RATE_PER_MIN`
//! (default 0, no limit): a global budget of computed answers per minute,
//! above which the server answers `429 rate_limited`.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::process::ExitCode;
use std::rc::Rc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use http_body_util::Full;
use hyper::body::{Bytes, Incoming};
use hyper::header::{HeaderValue, CACHE_CONTROL};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

use crate::bucket::Data;
use crate::error::{self, HttpError};
use crate::route;

const DEFAULT_ORIGIN: &str = "https://dataset.ringsaturn.me/xue/";

/// The Swagger UI the Worker serves from `public/`, compiled in so the binary
/// is the whole deployment.
const ASSETS: &[(&str, &str, &[u8])] = &[
    (
        "/",
        "text/html; charset=utf-8",
        include_bytes!("../public/index.html"),
    ),
    (
        "/docs/",
        "text/html; charset=utf-8",
        include_bytes!("../public/docs/index.html"),
    ),
    (
        "/docs/swagger-ui-bundle.js",
        "text/javascript; charset=utf-8",
        include_bytes!("../public/docs/swagger-ui-bundle.js"),
    ),
    (
        "/docs/swagger-ui.css",
        "text/css; charset=utf-8",
        include_bytes!("../public/docs/swagger-ui.css"),
    ),
    (
        "/docs/favicon-32x32.png",
        "image/png",
        include_bytes!("../public/docs/favicon-32x32.png"),
    ),
    (
        "/docs/LICENSE",
        "text/plain; charset=utf-8",
        include_bytes!("../public/docs/LICENSE"),
    ),
    (
        "/docs/VERSION",
        "text/plain; charset=utf-8",
        include_bytes!("../public/docs/VERSION"),
    ),
];

type Body = Full<Bytes>;

/// The server's settings, from the environment (see the module docs).
pub struct Config {
    listen: SocketAddr,
    origin: String,
    inflight: usize,
    cache_bytes: usize,
    rate_per_minute: usize,
}

impl Config {
    pub fn from_env() -> Result<Config, String> {
        fn var(name: &str) -> Option<String> {
            std::env::var(name).ok().filter(|value| !value.is_empty())
        }
        fn number(name: &str, default: usize) -> Result<usize, String> {
            match var(name) {
                Some(value) => value
                    .parse()
                    .map_err(|_| format!("{name}={value} is not a number")),
                None => Ok(default),
            }
        }
        let listen = var("XUE_API_LISTEN").unwrap_or_else(|| "127.0.0.1:8787".to_owned());
        Ok(Config {
            listen: listen
                .parse()
                .map_err(|_| format!("XUE_API_LISTEN={listen} is not an address"))?,
            origin: var("DATA_ORIGIN").unwrap_or_else(|| DEFAULT_ORIGIN.to_owned()),
            inflight: number("XUE_API_INFLIGHT", 32)?.max(1),
            cache_bytes: number("XUE_API_CACHE_MB", 64)? * 1024 * 1024,
            rate_per_minute: number("XUE_API_RATE_PER_MIN", 0)?,
        })
    }
}

/// A cached `200`: the body, its extra headers and when it stops being fresh.
struct Held {
    body: Bytes,
    headers: Vec<(&'static str, String)>,
    expires: Instant,
}

/// The response cache, keyed by path and query as sent, evicted oldest
/// first. Answers are small (a point is a few KB), so insertion order is as
/// good as recency here.
struct Responses {
    map: HashMap<String, Held>,
    order: VecDeque<String>,
    bytes: usize,
    limit: usize,
}

impl Responses {
    fn get(&mut self, key: &str, now: Instant) -> Option<(&Held, u64)> {
        let fresh = match self.map.get(key) {
            Some(held) => held.expires > now,
            None => return None,
        };
        if !fresh {
            self.remove(key);
            return None;
        }
        let held = self.map.get(key)?;
        Some((held, held.expires.duration_since(now).as_secs()))
    }

    fn remove(&mut self, key: &str) {
        if let Some(held) = self.map.remove(key) {
            self.bytes -= held.body.len();
            if let Some(position) = self.order.iter().position(|held| held == key) {
                self.order.remove(position);
            }
        }
    }

    fn put(&mut self, key: String, held: Held) {
        if held.body.len() > self.limit / 8 {
            return;
        }
        self.remove(&key);
        self.bytes += held.body.len();
        self.order.push_back(key.clone());
        self.map.insert(key, held);
        while self.bytes > self.limit {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            if let Some(evicted) = self.map.remove(&oldest) {
                self.bytes -= evicted.body.len();
            }
        }
    }
}

struct State {
    data: Data,
    inflight: Cell<usize>,
    inflight_limit: usize,
    responses: RefCell<Responses>,
    rate: Option<RefCell<Bucket>>,
}

/// A global token bucket over computed answers: `per_minute` tokens refill
/// evenly and up to a minute's worth may be spent at once. Cache hits never
/// draw on it, so it caps what misses cost, not what clients see.
struct Bucket {
    per_minute: f64,
    tokens: f64,
    refilled: Instant,
}

impl Bucket {
    fn new(per_minute: usize) -> Bucket {
        Bucket {
            per_minute: per_minute as f64,
            tokens: per_minute as f64,
            refilled: Instant::now(),
        }
    }

    /// Spend a token, or the whole seconds until one is available.
    fn take(&mut self, now: Instant) -> Result<(), u64> {
        let per_second = self.per_minute / 60.0;
        let elapsed = now.duration_since(self.refilled).as_secs_f64();
        self.tokens = (self.tokens + elapsed * per_second).min(self.per_minute);
        self.refilled = now;
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            Ok(())
        } else {
            Err(((1.0 - self.tokens) / per_second).ceil().max(1.0) as u64)
        }
    }
}

pub fn main() -> ExitCode {
    let config = match Config::from_env() {
        Ok(config) => config,
        Err(message) => {
            eprintln!("xue-api-server: {message}");
            return ExitCode::from(2);
        }
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("a tokio runtime");
    let local = tokio::task::LocalSet::new();
    let served = local.block_on(&runtime, async {
        let client = reqwest::Client::builder()
            .user_agent(concat!("xue-api/", env!("CARGO_PKG_VERSION")))
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(20))
            .build()
            .map_err(|error| error.to_string())?;
        let data = Data::from_origin(client, &config.origin);
        serve(config, data).await
    });
    match served {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("xue-api-server: {message}");
            ExitCode::FAILURE
        }
    }
}

/// Serve `data` until SIGTERM or Ctrl-C. Must run inside a
/// `tokio::task::LocalSet` on a current-thread runtime, as `main` sets up: the
/// handlers and their caches are thread-local. A caller with its own
/// [`crate::ObjectSource`] builds `Data::from_source` and spawns any work of
/// its own on the same `LocalSet`.
pub async fn serve(config: Config, data: Data) -> Result<(), String> {
    let state = Rc::new(State {
        data,
        inflight: Cell::new(0),
        inflight_limit: config.inflight,
        rate: (config.rate_per_minute > 0)
            .then(|| RefCell::new(Bucket::new(config.rate_per_minute))),
        responses: RefCell::new(Responses {
            map: HashMap::new(),
            order: VecDeque::new(),
            bytes: 0,
            limit: config.cache_bytes,
        }),
    });
    let listener = TcpListener::bind(config.listen)
        .await
        .map_err(|error| format!("bind {}: {error}", config.listen))?;
    eprintln!(
        "xue-api-server {} on http://{} reading {}",
        env!("CARGO_PKG_VERSION"),
        config.listen,
        state.data.origin
    );
    tokio::task::spawn_local(watchdog());
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .map_err(|error| error.to_string())?;
    loop {
        let (stream, _) = tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok(accepted) => accepted,
                Err(error) => {
                    eprintln!("accept: {error}");
                    continue;
                }
            },
            _ = terminate.recv() => return Ok(()),
            _ = tokio::signal::ctrl_c() => return Ok(()),
        };
        let state = state.clone();
        tokio::task::spawn_local(async move {
            let service = service_fn(move |request| {
                let state = state.clone();
                async move { Ok::<_, Infallible>(respond(&state, request).await) }
            });
            let _ = http1::Builder::new()
                .timer(hyper_util::rt::TokioTimer::new())
                .header_read_timeout(Duration::from_secs(10))
                .serve_connection(TokioIo::new(stream), service)
                .await;
        });
    }
}

/// Tell systemd (`Type=notify`, `WatchdogSec=`) the server is ready, then
/// keep proving the event loop still turns: a wedged loop misses the pings
/// and systemd restarts it, which also takes the tunnel down with it
/// (`BindsTo=`), so Cloudflare stops sending requests to a dead origin.
/// Without `NOTIFY_SOCKET` (run by hand) this does nothing.
async fn watchdog() {
    let Some(socket) = std::env::var_os("NOTIFY_SOCKET") else {
        return;
    };
    let Ok(sender) = std::os::unix::net::UnixDatagram::unbound() else {
        return;
    };
    let notify = |message: &str| {
        let _ = sender.send_to(message.as_bytes(), &socket);
    };
    notify("READY=1");
    let period = std::env::var("WATCHDOG_USEC")
        .ok()
        .and_then(|usec| usec.parse::<u64>().ok())
        .map(|usec| Duration::from_micros(usec / 3));
    let Some(period) = period else {
        return;
    };
    loop {
        notify("WATCHDOG=1");
        tokio::time::sleep(period).await;
    }
}

fn now_ms() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64() * 1000.0)
        .unwrap_or(0.0)
}

async fn respond(state: &State, request: Request<Incoming>) -> Response<Body> {
    let started = Instant::now();
    let method = request.method().clone();
    let target = request
        .uri()
        .path_and_query()
        .map(|target| target.as_str().to_owned())
        .unwrap_or_else(|| "/".to_owned());
    let gzip = accepts_gzip(&request);
    let (mut response, outcome) = match handle(state, &method, &target).await {
        Ok(answered) => answered,
        Err(error) => (error_response(&error), "error"),
    };
    response = compress(response, gzip).await;
    let elapsed = started.elapsed().as_millis();
    set(&mut response, "x-xue-ms", &elapsed.to_string());
    eprintln!(
        "{method} {target} {} {outcome} {elapsed}ms",
        response.status().as_u16()
    );
    response
}

/// Whether the client takes `gzip` (and has not refused it with `q=0`).
/// Cloudflare asks the origin for it and recompresses for its own clients.
fn accepts_gzip(request: &Request<Incoming>) -> bool {
    request
        .headers()
        .get_all(hyper::header::ACCEPT_ENCODING)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .any(|coding| {
            let mut parts = coding.split(';').map(str::trim);
            let name = parts.next().unwrap_or_default();
            let refused = parts.any(|parameter| {
                parameter
                    .strip_prefix("q=")
                    .and_then(|q| q.parse::<f32>().ok())
                    == Some(0.0)
            });
            (name.eq_ignore_ascii_case("gzip") || name == "*") && !refused
        })
}

/// Gzip a `200` text body. A point answer is ~5 KB of JSON that gzips to
/// under 1 KB, and the tunnel's leg is this host's metered egress; bodies
/// under a packet are not worth the header.
async fn compress(response: Response<Body>, gzip: bool) -> Response<Body> {
    use http_body_util::BodyExt;
    use std::io::Write;

    let compressible = response.status() == StatusCode::OK
        && response
            .headers()
            .get(hyper::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                value.starts_with("application/json") || value.starts_with("text/")
            });
    if !compressible {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    let bytes = match body.collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(never) => match never {},
    };
    // Either form varies by the request's encoding, so a shared cache keeps
    // them apart.
    parts.headers.insert(
        hyper::header::VARY,
        HeaderValue::from_static("accept-encoding"),
    );
    if !gzip || bytes.len() < 1024 {
        return Response::from_parts(parts, Full::new(bytes));
    }
    let mut encoder = flate2::write::GzEncoder::new(
        Vec::with_capacity(bytes.len() / 4),
        flate2::Compression::default(),
    );
    let gzipped = encoder.write_all(&bytes).and_then(|()| encoder.finish());
    match gzipped {
        Ok(gzipped) => {
            parts.headers.insert(
                hyper::header::CONTENT_ENCODING,
                HeaderValue::from_static("gzip"),
            );
            parts.headers.remove(hyper::header::CONTENT_LENGTH);
            Response::from_parts(parts, Full::new(Bytes::from(gzipped)))
        }
        Err(_) => Response::from_parts(parts, Full::new(bytes)),
    }
}

async fn handle(
    state: &State,
    method: &Method,
    target: &str,
) -> Result<(Response<Body>, &'static str), HttpError> {
    if method == Method::OPTIONS {
        return Ok((preflight(), "preflight"));
    }
    if method != Method::GET && method != Method::HEAD {
        return Err(HttpError::new(
            405,
            "method_not_allowed",
            "only GET is supported",
        ));
    }
    let url = url::Url::parse(&format!("http://localhost{target}"))
        .map_err(|_| HttpError::new(400, "invalid_parameter", "the request target is not a URL"))?;
    let path = url.path();
    if let Some(response) = asset(path) {
        return Ok((response, "asset"));
    }
    let path = route::normalize(path);
    let cacheable = route::cacheable(path);
    if cacheable {
        if let Some((held, remaining)) = state.responses.borrow_mut().get(target, Instant::now()) {
            let cache_control = format!("public, max-age={remaining}");
            return Ok((
                json(
                    held.body.clone(),
                    StatusCode::OK,
                    &cache_control,
                    &held.headers,
                ),
                "hit",
            ));
        }
    }
    // Only computing a response holds a slot or draws on the rate; cache hits
    // and assets never wait behind a slow upstream.
    if let (Some(rate), true) = (&state.rate, path != "/health") {
        if let Err(seconds) = rate.borrow_mut().take(Instant::now()) {
            return Err(HttpError::new(
                429,
                "rate_limited",
                "the origin's request budget is spent; retry after Retry-After seconds",
            )
            .with_detail(serde_json::json!({ "retryAfter": seconds })));
        }
    }
    if state.inflight.get() >= state.inflight_limit {
        return Err(HttpError::new(
            503,
            "overloaded",
            "too many requests are being computed; retry shortly",
        ));
    }
    state.inflight.set(state.inflight.get() + 1);
    let answer = route::dispatch(path, &url, &state.data, now_ms()).await;
    state.inflight.set(state.inflight.get() - 1);
    let answer = answer?;
    let body = Bytes::from(answer.body);
    if cacheable {
        if let Some(seconds) = max_age(answer.cache_control) {
            state.responses.borrow_mut().put(
                target.to_owned(),
                Held {
                    body: body.clone(),
                    headers: answer.headers.clone(),
                    expires: Instant::now() + Duration::from_secs(seconds),
                },
            );
        }
    }
    Ok((
        json(body, StatusCode::OK, answer.cache_control, &answer.headers),
        "miss",
    ))
}

/// `max-age` of a `Cache-Control` the router wrote, when it allows caching.
fn max_age(cache_control: &str) -> Option<u64> {
    cache_control
        .split(',')
        .filter_map(|directive| directive.trim().strip_prefix("max-age="))
        .find_map(|seconds| seconds.parse().ok())
        .filter(|seconds| *seconds > 0)
}

fn set(response: &mut Response<Body>, name: &'static str, value: &str) {
    if let Ok(value) = HeaderValue::from_str(value) {
        response.headers_mut().insert(name, value);
    }
}

fn json(
    body: Bytes,
    status: StatusCode,
    cache_control: &str,
    extra: &[(&'static str, String)],
) -> Response<Body> {
    let mut response = Response::new(Full::new(body));
    *response.status_mut() = status;
    set(
        &mut response,
        "content-type",
        "application/json; charset=utf-8",
    );
    set(&mut response, "access-control-allow-origin", "*");
    set(&mut response, "x-xue-api", env!("CARGO_PKG_VERSION"));
    set(&mut response, "cache-control", cache_control);
    for (name, value) in extra {
        set(&mut response, name, value);
    }
    response
}

/// The contract's error document. `503 overloaded` carries `Retry-After`;
/// no error is cacheable.
fn error_response(error: &HttpError) -> Response<Body> {
    let body = serde_json::to_vec(&error::body(error)).unwrap_or_default();
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut response = json(Bytes::from(body), status, "no-store", &[]);
    if error.status == 503 {
        set(&mut response, "retry-after", "5");
    }
    if let Some(seconds) = error
        .detail
        .as_ref()
        .and_then(|detail| detail.get("retryAfter"))
        .and_then(serde_json::Value::as_u64)
    {
        set(&mut response, "retry-after", &seconds.to_string());
    }
    response
}

fn preflight() -> Response<Body> {
    let mut response = Response::new(Full::new(Bytes::new()));
    *response.status_mut() = StatusCode::NO_CONTENT;
    set(&mut response, "access-control-allow-origin", "*");
    set(
        &mut response,
        "access-control-allow-methods",
        "GET, HEAD, OPTIONS",
    );
    set(&mut response, "access-control-allow-headers", "*");
    set(&mut response, "access-control-max-age", "86400");
    response
}

/// An embedded asset, with the Workers assets server's trailing-slash
/// handling for the docs directory (`/docs` → `/docs/`).
fn asset(path: &str) -> Option<Response<Body>> {
    if path == "/docs" {
        let mut response = Response::new(Full::new(Bytes::new()));
        *response.status_mut() = StatusCode::TEMPORARY_REDIRECT;
        set(&mut response, "location", "/docs/");
        return Some(response);
    }
    let path = match path {
        "/index.html" => "/",
        "/docs/index.html" => "/docs/",
        path => path,
    };
    let (_, content_type, bytes) = ASSETS.iter().find(|(name, _, _)| *name == path)?;
    let mut response = Response::new(Full::new(Bytes::from_static(bytes)));
    set(&mut response, "content-type", content_type);
    response.headers_mut().insert(
        CACHE_CONTROL,
        HeaderValue::from_static("public, max-age=3600"),
    );
    Some(response)
}

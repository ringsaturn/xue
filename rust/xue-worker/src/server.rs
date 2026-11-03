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
//! `XUE_API_METRICS_LISTEN` (default unset, off) serves Prometheus metrics
//! (`metrics.rs`) at `/metrics` on an address of its own; never the API's,
//! which the tunnel makes public.
//!
//! Behind a proxy that authenticates clients: `XUE_API_REQUIRE_HEADER`
//! (`name: value`, default unset) answers `403 forbidden` to every request
//! but `/health` that lacks that header, so only the proxy is served; and
//! `XUE_API_CLIENT_HEADER` with `XUE_API_INFLIGHT_PER_CLIENT` (default 0, off)
//! caps the requests computed at once per value of a header the proxy sets,
//! above which that client alone gets `503 overloaded`.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::process::ExitCode;
use std::rc::Rc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use http_body_util::Full;
use hyper::body::{Bytes, Incoming};
use hyper::header::{HeaderName, HeaderValue, CACHE_CONTROL};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::reply::{self, Reply};
use crate::{docs, metrics, route};

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
    metrics: Option<SocketAddr>,
    origin: String,
    inflight: usize,
    cache_bytes: usize,
    rate_per_minute: usize,
    require_header: Option<(HeaderName, HeaderValue)>,
    client_header: Option<HeaderName>,
    inflight_per_client: usize,
}

/// `name: value` of `XUE_API_REQUIRE_HEADER`.
fn parse_required(text: &str) -> Result<(HeaderName, HeaderValue), String> {
    let invalid = || "XUE_API_REQUIRE_HEADER is not `name: value`".to_owned();
    let (name, value) = text.split_once(':').ok_or_else(invalid)?;
    let value = value.trim();
    if value.is_empty() {
        return Err(invalid());
    }
    Ok((
        HeaderName::from_bytes(name.trim().as_bytes()).map_err(|_| invalid())?,
        HeaderValue::from_str(value).map_err(|_| invalid())?,
    ))
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
        let metrics = var("XUE_API_METRICS_LISTEN")
            .map(|address| {
                address
                    .parse()
                    .map_err(|_| format!("XUE_API_METRICS_LISTEN={address} is not an address"))
            })
            .transpose()?;
        if metrics == listen.parse().ok() {
            return Err("XUE_API_METRICS_LISTEN must differ from XUE_API_LISTEN".to_owned());
        }
        let client_header = var("XUE_API_CLIENT_HEADER")
            .map(|name| {
                HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| format!("XUE_API_CLIENT_HEADER={name} is not a header name"))
            })
            .transpose()?;
        let inflight_per_client = number("XUE_API_INFLIGHT_PER_CLIENT", 0)?;
        if inflight_per_client > 0 && client_header.is_none() {
            return Err("XUE_API_INFLIGHT_PER_CLIENT needs XUE_API_CLIENT_HEADER".to_owned());
        }
        Ok(Config {
            listen: listen
                .parse()
                .map_err(|_| format!("XUE_API_LISTEN={listen} is not an address"))?,
            metrics,
            origin: var("DATA_ORIGIN").unwrap_or_else(|| DEFAULT_ORIGIN.to_owned()),
            inflight: number("XUE_API_INFLIGHT", 32)?.max(1),
            cache_bytes: number("XUE_API_CACHE_MB", 64)? * 1024 * 1024,
            rate_per_minute: number("XUE_API_RATE_PER_MIN", 0)?,
            require_header: var("XUE_API_REQUIRE_HEADER")
                .map(|text| parse_required(&text))
                .transpose()?,
            client_header,
            inflight_per_client,
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

/// Routes a caller serves beside the API's own ([`serve_with`]), asked
/// first. An answer from one is cached, rate-limited, compressed and framed
/// like any other `200` or error.
pub trait Routes {
    /// The route template a normalized `path` answers on (the metrics
    /// label), or `None` when the path is not one of these routes.
    fn route(&self, path: &str) -> Option<&'static str>;

    /// Answer a `GET` of `path`, one [`Routes::route`] claimed. `now` is
    /// milliseconds since the epoch. `data` is the server's own: an answer
    /// built on the API's may ask it with [`crate::dispatch`].
    fn answer<'a>(&'a self, path: &'a str, url: &'a url::Url, data: &'a Data, now: f64) -> Answering<'a>;

    /// OpenAPI documents describing these routes, merged into the served
    /// contract (`/v1/openapi.json`, and so `/docs/`): their `tags`, `paths`
    /// and `components` are added beside the API's own. Each may `$ref` the
    /// API's components. None by default.
    fn openapi(&self) -> Vec<&'static str> {
        Vec::new()
    }
}

/// What [`Routes::answer`] returns: the handlers run on one thread, so the
/// future need not be `Send`.
pub type Answering<'a> = futures_util::future::LocalBoxFuture<'a, Result<route::Answer, HttpError>>;

struct State {
    data: Data,
    routes: Option<Box<dyn Routes>>,
    started: SystemTime,
    slots: Slots,
    responses: RefCell<Responses>,
    rate: Option<RefCell<Bucket>>,
    require_header: Option<(HeaderName, HeaderValue)>,
    client_header: Option<HeaderName>,
}

/// Requests being computed: at most `limit` at once, and at most
/// `per_client` (0: off) for one client.
struct Slots {
    inflight: Cell<usize>,
    limit: usize,
    per_client: usize,
    /// Only clients with a request being computed.
    clients: RefCell<HashMap<String, usize>>,
}

impl Slots {
    fn new(limit: usize, per_client: usize) -> Slots {
        Slots {
            inflight: Cell::new(0),
            limit,
            per_client,
            clients: RefCell::new(HashMap::new()),
        }
    }
}

/// A computing slot: taken before a handler runs and given back when dropped,
/// so a request whose client goes away mid-answer (hyper drops its future)
/// still frees it.
struct Slot<'a> {
    slots: &'a Slots,
    client: Option<String>,
}

impl<'a> Slot<'a> {
    fn take(slots: &'a Slots, client: Option<&str>) -> Result<Slot<'a>, HttpError> {
        if slots.inflight.get() >= slots.limit {
            return Err(HttpError::new(
                503,
                "overloaded",
                "too many requests are being computed; retry shortly",
            ));
        }
        let client = match client.filter(|_| slots.per_client > 0) {
            Some(client) => {
                let mut clients = slots.clients.borrow_mut();
                let held = clients.entry(client.to_owned()).or_insert(0);
                if *held >= slots.per_client {
                    return Err(HttpError::new(
                        503,
                        "overloaded",
                        "too many of this client's requests are being computed; retry shortly",
                    ));
                }
                *held += 1;
                Some(client.to_owned())
            }
            None => None,
        };
        slots.inflight.set(slots.inflight.get() + 1);
        Ok(Slot { slots, client })
    }
}

impl Drop for Slot<'_> {
    fn drop(&mut self) {
        self.slots.inflight.set(self.slots.inflight.get() - 1);
        if let Some(client) = &self.client {
            let mut clients = self.slots.clients.borrow_mut();
            if let Some(held) = clients.get_mut(client) {
                *held -= 1;
                if *held == 0 {
                    clients.remove(client);
                }
            }
        }
    }
}

/// Compares in time independent of where the inputs first differ.
fn same_bytes(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |difference, (x, y)| difference | (x ^ y)) == 0
}

/// Whether `XUE_API_REQUIRE_HEADER`, when set, is satisfied. `/health` is
/// always served, for the machine's own probes.
fn admitted(state: &State, request: &Request<Incoming>) -> bool {
    let Some((name, value)) = &state.require_header else {
        return true;
    };
    request.uri().path() == "/health"
        || request
            .headers()
            .get(name)
            .is_some_and(|sent| same_bytes(sent.as_bytes(), value.as_bytes()))
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

    /// The tokens available at `now`.
    fn refill(&mut self, now: Instant) -> f64 {
        let elapsed = now.duration_since(self.refilled).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.per_minute / 60.0).min(self.per_minute);
        self.refilled = now;
        self.tokens
    }

    /// Spend a token, or the whole seconds until one is available.
    fn take(&mut self, now: Instant) -> Result<(), u64> {
        let per_second = self.per_minute / 60.0;
        self.refill(now);
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
    serve_routes(config, data, None).await
}

/// [`serve`], with `routes` answered before the API's own.
pub async fn serve_with(config: Config, data: Data, routes: Box<dyn Routes>) -> Result<(), String> {
    serve_routes(config, data, Some(routes)).await
}

async fn serve_routes(
    config: Config,
    data: Data,
    routes: Option<Box<dyn Routes>>,
) -> Result<(), String> {
    let state = Rc::new(State {
        data,
        routes,
        started: SystemTime::now(),
        slots: Slots::new(config.inflight, config.inflight_per_client),
        rate: (config.rate_per_minute > 0)
            .then(|| RefCell::new(Bucket::new(config.rate_per_minute))),
        require_header: config.require_header,
        client_header: config.client_header,

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
        "xue-api-server {} ({}) on http://{} reading {}",
        env!("CARGO_PKG_VERSION"),
        metrics::REVISION,
        config.listen,
        state.data.origin
    );
    if let Some(address) = config.metrics {
        let listener = TcpListener::bind(address)
            .await
            .map_err(|error| format!("bind {address}: {error}"))?;
        eprintln!("metrics on http://{address}/metrics");
        tokio::task::spawn_local(serve_metrics(listener, state.clone()));
    }
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

/// The metrics listener: `GET /metrics` and nothing else. Its own accept loop
/// on the same thread, so a scrape reads the state between two requests.
async fn serve_metrics(listener: TcpListener, state: Rc<State>) {
    loop {
        let stream = match listener.accept().await {
            Ok((stream, _)) => stream,
            Err(error) => {
                eprintln!("metrics accept: {error}");
                continue;
            }
        };
        let state = state.clone();
        tokio::task::spawn_local(async move {
            let service = service_fn(move |request: Request<Incoming>| {
                let state = state.clone();
                async move {
                    let mut response = if request.method() == Method::GET
                        && request.uri().path() == "/metrics"
                    {
                        let mut response =
                            Response::new(Full::new(Bytes::from(scrape(&state))));
                        set(
                            &mut response,
                            "content-type",
                            "text/plain; version=0.0.4; charset=utf-8",
                        );
                        response
                    } else {
                        let mut response = Response::new(Full::new(Bytes::new()));
                        *response.status_mut() = StatusCode::NOT_FOUND;
                        response
                    };
                    set(&mut response, "cache-control", "no-store");
                    Ok::<_, Infallible>(response)
                }
            });
            let _ = http1::Builder::new()
                .timer(hyper_util::rt::TokioTimer::new())
                .header_read_timeout(Duration::from_secs(10))
                .serve_connection(TokioIo::new(stream), service)
                .await;
        });
    }
}

fn scrape(state: &State) -> String {
    let responses = state.responses.borrow();
    let rate_tokens = state
        .rate
        .as_ref()
        .map(|rate| rate.borrow_mut().refill(Instant::now()));
    metrics::render(&metrics::Gauges {
        inflight: state.slots.inflight.get(),
        inflight_limit: state.slots.limit,
        responses_entries: responses.map.len(),
        responses_bytes: responses.bytes,
        responses_limit_bytes: responses.limit,
        rate_tokens,
        started: state.started,
    })
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
    let client = state
        .client_header
        .as_ref()
        .and_then(|name| request.headers().get(name))
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let handled = if admitted(state, &request) {
        handle(state, &method, &target, client.as_deref()).await
    } else {
        Err(HttpError::new(403, "forbidden", "this origin answers only its proxy"))
    };
    let (mut response, outcome) = match handled {
        Ok(answered) => answered,
        Err(error) => (hyper_response(error_reply(&error)), "error"),
    };
    response = compress(response, gzip).await;
    let path = route::normalize(target.split('?').next().unwrap_or("/"));
    let route = state
        .routes
        .as_ref()
        .and_then(|routes| routes.route(path))
        .unwrap_or_else(|| metrics::route_label(path));
    let sent = {
        use hyper::body::Body as _;
        response.body().size_hint().exact().unwrap_or(0) as usize
    };
    metrics::request(
        route,
        outcome,
        response.status().as_u16(),
        started.elapsed(),
        sent,
    );
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
    client: Option<&str>,
) -> Result<(Response<Body>, &'static str), HttpError> {
    if method == Method::OPTIONS {
        return Ok((hyper_response(reply::preflight()), "preflight"));
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
            let reply = reply::json(held.body.to_vec(), 200, &cache_control, &held.headers);
            return Ok((hyper_response(reply), "hit"));
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
    let slot = Slot::take(&state.slots, client)?;
    let answer = match state.routes.as_ref().filter(|routes| routes.route(path).is_some()) {
        Some(routes) => routes.answer(path, &url, &state.data, now_ms()).await,
        None => route::dispatch(path, &url, &state.data, now_ms()).await,
    };
    drop(slot);
    let mut answer = answer?;
    if let (Some(routes), "/openapi.json" | "/v1/openapi.json") = (&state.routes, path) {
        let extra = routes.openapi();
        if !extra.is_empty() {
            answer.body = docs::merge(&answer.body, &extra)?;
        }
    }
    let reply = reply::json(answer.body, 200, answer.cache_control, &answer.headers);
    if cacheable {
        if let Some(seconds) = max_age(answer.cache_control) {
            state.responses.borrow_mut().put(
                target.to_owned(),
                Held {
                    body: Bytes::copy_from_slice(&reply.body),
                    headers: answer.headers.clone(),
                    expires: Instant::now() + Duration::from_secs(seconds),
                },
            );
        }
    }
    Ok((hyper_response(reply), "miss"))
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

/// A [`Reply`] as a hyper response.
fn hyper_response(reply: Reply) -> Response<Body> {
    let mut response = Response::new(Full::new(Bytes::from(reply.body)));
    *response.status_mut() = StatusCode::from_u16(reply.status).unwrap_or(StatusCode::BAD_GATEWAY);
    for (name, value) in &reply.headers {
        set(&mut response, name, value);
    }
    response
}

/// The contract's error document. `503 overloaded` carries `Retry-After`;
/// no error is cacheable.
fn error_reply(error: &HttpError) -> Reply {
    let mut reply = reply::error_reply(error).with_header("cache-control", "no-store");
    if error.status == 503 {
        reply = reply.with_header("retry-after", "5");
    }
    if let Some(seconds) = error
        .detail
        .as_ref()
        .and_then(|detail| detail.get("retryAfter"))
        .and_then(serde_json::Value::as_u64)
    {
        reply = reply.with_header("retry-after", seconds.to_string());
    }
    reply
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn required_header() {
        let (name, value) = parse_required("X-Proxy-Secret: a:b c").unwrap();
        assert_eq!(name.as_str(), "x-proxy-secret");
        assert_eq!(value, "a:b c");
        assert!(parse_required("x-proxy-secret").is_err());
        assert!(parse_required("x-proxy-secret:  ").is_err());
        assert!(parse_required("bad name: v").is_err());
    }

    #[test]
    fn same_bytes_compares_whole_values() {
        assert!(same_bytes(b"secret", b"secret"));
        assert!(!same_bytes(b"secret", b"secreT"));
        assert!(!same_bytes(b"secret", b"secret2"));
    }

    #[test]
    fn slots_cap_all_and_each_client() {
        let slots = Slots::new(3, 2);
        let a1 = Slot::take(&slots, Some("a")).unwrap();
        let _a2 = Slot::take(&slots, Some("a")).unwrap();
        assert_eq!(Slot::take(&slots, Some("a")).err().unwrap().status, 503);
        let _b = Slot::take(&slots, Some("b")).unwrap();
        // The global cap holds whoever asks.
        assert_eq!(Slot::take(&slots, None).err().unwrap().status, 503);
        drop(a1);
        assert_eq!(slots.inflight.get(), 2);
        assert_eq!(slots.clients.borrow()["a"], 1);
        let _a3 = Slot::take(&slots, Some("a")).unwrap();
    }

    #[test]
    fn slots_forget_idle_clients_and_ignore_them_when_off() {
        let slots = Slots::new(8, 1);
        drop(Slot::take(&slots, Some("a")).unwrap());
        assert!(slots.clients.borrow().is_empty());
        assert_eq!(slots.inflight.get(), 0);

        let off = Slots::new(8, 0);
        let _x = Slot::take(&off, Some("a")).unwrap();
        let _y = Slot::take(&off, Some("a")).unwrap();
        assert!(off.clients.borrow().is_empty());
    }
}

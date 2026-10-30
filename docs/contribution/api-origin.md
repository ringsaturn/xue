# The data API on a self-hosted origin

The data API (`rust/xue-worker`, contract in [api.md](../api.md)) runs on
the Workers runtime and, with the same routes, as a native binary
`xue-api-server`. The binary is for a self-hosted origin: a fixed-price
machine behind Cloudflare's proxy, so the worst case under load is a slow
or unavailable API, never a metered bill.

## What differs from the Worker

| | Worker | `xue-api-server` |
|---|---|---|
| Data | R2 binding (`DATA`), or the public origin with `DATA_SOURCE=cdn` | the public origin over HTTP (`DATA_ORIGIN`); a cache miss costs CPU here, not an R2 read |
| Response cache | `caches.default`, per data center | in process, for the answer's `max-age`; a hit answers with the remaining lifetime |
| Object cache | per isolate (`cache.rs`) | the same module, one per process |
| Overload | Workers limits | `XUE_API_INFLIGHT` requests computed at once, then `503 overloaded` with `Retry-After` |
| Rate | the zone's per-IP rule | optionally a global budget, `XUE_API_RATE_PER_MIN` computed answers a minute, then `429 rate_limited`; Cloudflare's free rate-limit rules count per IP only, so a global cap has to live here |
| Swagger UI | static assets from `public/` | the same files compiled into the binary |
| Compression | Cloudflare's | gzip for a client that takes it (Cloudflare does), `Vary: accept-encoding`; a point answer goes out at about a sixth of its size |

`route.rs` decides which handler answers a path and its `Cache-Control`;
`runtime.rs` (Worker) and `server.rs` (binary) only frame the answer. A
route added to one runtime is in both.

Configuration is the environment, read from `/etc/xue-api/env` when it
exists: `XUE_API_LISTEN` (default `127.0.0.1:8787`), `DATA_ORIGIN`,
`XUE_API_INFLIGHT` (32), `XUE_API_CACHE_MB` (64), `XUE_API_RATE_PER_MIN`
(0, off), `XUE_API_METRICS_LISTEN` (unset, off).

## Metrics

With `XUE_API_METRICS_LISTEN` set (e.g. `127.0.0.1:9787`) the binary serves
Prometheus metrics at `/metrics` on that address, and only there (`metrics.rs`).
It must not be the API's address: the tunnel forwards everything that
reaches that one, so a metrics route there would be public. Keep it on
loopback, or on a private interface a scraper reaches; the tunnel never
connects to it.

| Metric | What |
|---|---|
| `xue_api_requests_total{route,outcome,status}` | answers, by route template (`/v1/soundings/{station}`, `other` for anything else), outcome (`hit`, `miss`, `error`, `asset`, `preflight`) and status |
| `xue_api_request_duration_seconds{route,outcome}` | histogram, request to response |
| `xue_api_response_bytes_total{route}` | body bytes sent, after gzip |
| `xue_api_upstream_requests_total{kind,result}` | reads of the dataset origin (`whole`, `range`; `ok`, `not_found`, `error`) |
| `xue_api_upstream_duration_seconds`, `xue_api_upstream_bytes_total` | their time and bytes |
| `xue_api_inflight`, `xue_api_inflight_limit` | requests computed now, and the `503` threshold |
| `xue_api_response_cache_entries`, `_bytes`, `_limit_bytes` | the response cache |
| `xue_api_rate_tokens` | the global budget left, when `XUE_API_RATE_PER_MIN` is set |
| `xue_api_build_info{version}`, `xue_api_start_time_seconds` | the build, and when it started |

`hit` is this process's response cache; Cloudflare's edge hits never reach
the origin, so they are not counted here.

Measured on a 1-vCPU host (2026-10-30, single-variable GFS points, every
request a miss): 96 req/s at 16 in flight (p50 0.14 s), 124 req/s at 32
(p50 0.24 s, 71 % of the core); past that the single-threaded loop
saturates and latency, not throughput, grows. A miss is mostly waiting on
the range reads from the dataset origin, so a request for n bundles costs
about n times one.

## Another data source

The binary reads the public origin. A program that keeps the objects
elsewhere (a mirror on local disk, another store) depends on `xue-worker`
with `features = ["server"]`, implements `xue_worker::ObjectSource` (read an
object, or a byte range of it, by its path under the data root), and calls
`server::serve(Config::from_env()?, Data::from_source(…))` inside a
`LocalSet` on a current-thread runtime. Routes, caches, compression, limits
and the watchdog are the same; work of its own (refreshing the source) runs
as tasks on that `LocalSet`.

## Reaching it without exposing the host

The origin is reached only through a Cloudflare Tunnel. `cloudflared` on
the host dials out to Cloudflare; the API listens on loopback; the host
opens no port for it and no DNS record names its address. The public
hostname is a proxied CNAME to `<tunnel-id>.cfargotunnel.com`, which only
Cloudflare can route.

The alternative, a proxied A record plus a firewall that admits only
Cloudflare's address ranges, was rejected:

- The address is in DNS (and in DNS history, and in certificate scans if
  the origin serves TLS for the hostname).
- The ranges are shared by every Cloudflare customer: anyone can point a
  zone of their own at the address and reach the origin without this
  zone's WAF and rate limit, unless Authenticated Origin Pulls are added.
- The ranges change, and every origin has to follow.

Putting the Worker in front and calling the origin through the tunnel
(Workers VPC) would keep Worker-side control, but every request would
count as a Worker request again, which is the cost this setup exists to
cap.

What the tunnel does not hide: the origin's own outbound reads of the
public dataset come from the host's address, and other services on the
same host may already make that address public. Neither links the
address to the API's hostname.

## Units

`rust/xue-worker/origin/`:

- `xue-api.service`: `Type=notify` with `WatchdogSec=30` (the server pings
  from its event loop, so a wedged loop is restarted), `DynamicUser`,
  `MemoryMax=256M`, `CPUWeight=50` so a shared host's other services keep
  their share.
- `xue-api-tunnel.service`: `cloudflared tunnel run` with the token from
  `/etc/xue-api/tunnel.env` (`TUNNEL_TOKEN=…`, mode 600). `BindsTo=` the
  API: when the API stops, the connector stops, and Cloudflare stops
  sending requests to the host. With one connector that turns a `502` into
  Cloudflare's own error page; with several it moves traffic to the others.
- `install.sh`: installs the binary and the units, `cloudflared` from
  Cloudflare's apt repository if missing, restarts and checks `/health`.

## Deploy

```sh
# cargo-zigbuild cross-compiles for linux x86_64 (glibc 2.35+) from macOS
nix shell nixpkgs#zig nixpkgs#cargo-zigbuild -c \
  rust/xue-worker/origin/deploy.sh <user>@<host>
```

The first deploy needs the tunnel token: set `XUE_API_TUNNEL_TOKEN` for
that run (`cloudflared tunnel token <name>` prints it). Later deploys keep
the token on the host.

## Cloudflare side

One tunnel; the connector sends every hostname routed to it to
`http://127.0.0.1:8787` (`--url`), so the tunnel needs no ingress rules.
A hostname is a proxied CNAME to the tunnel (`cloudflared tunnel route dns
<tunnel> <hostname>`). More hosts run `deploy.sh` with the same token: each is another connector
of the same tunnel, and Cloudflare sends a request to the nearest one that
is up (no weights, no application health check; `BindsTo=` stands in for
the latter).

Zone settings the API depends on, all in the dashboard:

- A Cache Rule for the API hostname and `/v1/`: eligible for cache, edge
  TTL from the origin's `Cache-Control`, query string sorted in the cache
  key (so `lat=…&source=…` and `source=…&lat=…` share an entry).
  Cloudflare does not cache JSON without one, and then every request
  reaches the origin.
- Tiered Cache (Smart Tiered Cache on the free plan), so an edge miss
  asks an upper tier before the origin.
- The per-IP rate-limit rule [api.md](../api.md) mentions.

## Cutting over from the Worker

The Worker holds `xue-api.ringsaturn.me` as a Workers custom domain, and
`deploy-api.yml` redeploys it with that route on every push that touches
the crate. In order:

1. Bring the origin up on a second hostname of the same tunnel and compare
   it with the Worker.
2. Remove the custom domain from the Worker (`routes` in
   `wrangler.jsonc`), deploy, and point the hostname at the tunnel.
3. Add the Cache Rule for the hostname.

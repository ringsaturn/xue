# Building a site on Xue

How to put Xue's viewer on a page of your own, over your own data, with
your own layers and controls on top, without forking. The shell is not a
package: you pin this repository and build `web/` with a Vite config of
your own. [Hong](https://hong.ringsaturn.me/) is built this way.

## What you can rely on

| Surface | Status |
|---|---|
| The data formats ([`docs/`](../README.md)) | Versioned, with the compatibility rules in [`docs/README.md`](../README.md#compatibility) |
| `window.xueViewer`, `window.xuePalettes` | The embed contract: [frontend.md, "Embedding"](frontend.md#embedding) |
| The build-time variables [below](#build-time-variables) | Kept; one may be added |
| URL parameters | May change with any commit |
| DOM ids and classes, CSS, the map's layer and source ids, module paths under `web/src/` | Not an interface |
| `xuebuild` (Python) | Not a library: no stable module API, not on PyPI |

Everything in the last three rows changes without notice, so a site pins a
commit and re-checks what it touches when it moves the pin.

## Layout and build

```
your-site/
  vendor/xue/           this repository, a git submodule
  web/
    vite.config.mjs     runs Xue's shell with your module injected
    .env                the build-time variables
    overlay/main.ts     your module
  dist/web/             the static site
```

Node.js ≥ 22, and a Rust toolchain with `wasm-pack`: the decoder is built
from the pinned source, there is no prebuilt one.

```sh
git submodule add https://github.com/ringsaturn/xue vendor/xue
(cd vendor/xue && npm ci && make wasm)
vendor/xue/node_modules/.bin/vite --config web/vite.config.mjs         # http://localhost:5173
vendor/xue/node_modules/.bin/vite build --config web/vite.config.mjs   # dist/web/
```

`web/vite.config.mjs`, a plain object because it is run by the submodule's
Vite, which your directory cannot import:

```js
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..");
const xueWeb = path.join(repo, "vendor/xue/web");
const overlay = path.join(here, "overlay/main.ts");

function overlayPlugin() {
  let serving = false;
  return {
    name: "site:overlay",
    configureServer() {
      serving = true;
    },
    transformIndexHtml: {
      // Before Vite reads the page's scripts: a tag added later is copied
      // into the build as written and never bundled.
      order: "pre",
      handler(html, ctx) {
        if (ctx.filename !== path.join(xueWeb, "index.html")) return html;
        const src = serving ? `/@fs${overlay}` : path.relative(xueWeb, overlay);
        return [{ tag: "script", attrs: { type: "module", src }, injectTo: "body" }];
      },
    },
  };
}

export default {
  root: xueWeb,
  publicDir: "public",
  base: "./",
  envDir: here,
  plugins: [overlayPlugin()],
  server: { host: "localhost", port: 5173, strictPort: true, fs: { allow: [repo] } },
  build: {
    outDir: path.join(repo, "dist/web"),
    emptyOutDir: true,
    rollupOptions: { input: { main: path.join(xueWeb, "index.html") } },
  },
};
```

- Only `index.html` is an input, so the build ships the viewer and none of
  the shell's other pages (showcase list, comparison, typhoon, metrics, API
  console), and none of Xue's per-language or per-case copies. Without
  them the shell keeps the language out of the address (a link's
  `?lang=`, else the stored choice, else the browser's) and a case on the
  page as `?case=<id>` ([Language paths](frontend.md#language-paths)).
- `localhost:5173` exactly: the shell's Protomaps key serves `localhost`
  but not `127.0.0.1`, and Xue's public bucket admits that origin
  ([Data](#data)).
- To type-check the overlay, point `typeRoots` and `paths` of its
  `tsconfig.json` at `vendor/xue/node_modules` (`maplibre-gl`, `@types`).

## Build-time variables

Read from `.env` in `envDir` and inlined by Vite, so a change is a rebuild.
Unset means the default.

| Variable | Effect | Default |
|---|---|---|
| `VITE_DATA_BASE_URL` | The data root: pointers, manifests, stores, `showcase.json`. Ends with `/` | `/data/`, at the site root |
| `VITE_SOURCES` | Comma-separated source ids the model switch and the dataset paths (`/<id>`) offer | every source |
| `VITE_POINT_PRODUCTS` | Comma-separated point products the shell asks the root for (`tc`, `sounding`, `airport`, `synop`, `fire`, `aq`, `alerts`), or `none` | every product |
| `VITE_PROTOMAPS_KEY` | The Protomaps key bound to your origin | the shell's, which serves only its own origins |
| `VITE_BASEMAP_TILES` | Vector tile template of a basemap mirror, absolute | Protomaps' API |
| `VITE_BASEMAP_ASSETS` | Root holding Protomaps' `fonts/` and `sprites/`, absolute | `protomaps.github.io/basemaps-assets` |
| `VITE_BASEMAP_HIDE` | `labels` (every name, and the peaks), `boundaries`, or both | nothing hidden |
| `VITE_TERRAIN_TILE_URL` | Tile template of a Mapterhorn mirror | `tiles.mapterhorn.com` |
| `VITE_RIVER_COLOR` | `field`: colour a case's river reaches from the selected scalar field ([frontend](frontend.md)) | the catalog's `river.color`, or the ratio to usual flow |

## Data

The data root is static files. Its origin must:

- answer byte ranges with `206`, suffix ranges included (`bytes=-N`, how
  the reader asks for a shard index);
- answer a missing object with `404` or `403`, never a fallback page: the
  shell asks for pointers a root may not publish;
- when it is another origin than the page, admit the page's origin by CORS
  for `GET` with a `Range` header;
- serve the mutable objects (the `latest*.json` pointers, `showcase.json`)
  revalidated. Everything else is addressed with `?v=<crc32>` and may be
  cached forever, so nothing is ever overwritten under an unchanged `?v=`
  and a mutable object is written last.

Xue's public bucket admits browser reads from an allow-list of origins and
from `http://localhost:5173`. A page deployed on another origin cannot
read it and serves a root of its own; code that is not a browser reads it
freely ([README, "Using the data"](../../README.md#using-the-data)).

**Runs of a source Xue knows.** Run the pipeline into your root:
[setup.md](setup.md#building-a-run), [publishing.md](publishing.md). Name
the sources in `VITE_SOURCES` and the point products in
`VITE_POINT_PRODUCTS`.

**A dataset of your own.** Publish it as showcase cases
([`showcase/README.md`](../../showcase/README.md)):

```
<root>/showcase.json                  the catalog, the one mutable object
<root>/showcase/<id>/manifest.json    a schema 5 manifest
<root>/showcase/<id>/<bundle>.zarr/   stores, per docs/zarr-profile.md
```

- A catalog row may name a dataset the shell has no entry for: a `modelId`
  slug (`[a-z][a-z0-9]*`, not a built-in id), a `model` label no other
  dataset uses, a `product`. The shell admits it as case-only, absent from
  the model switch and the dataset paths, and draws its bundles generically; give
  them colours with `window.xuePalettes`.
- The stores must satisfy [`zarr-profile.md`](../zarr-profile.md) and
  [`bundle.md`](../bundle.md) (shapes in [`docs/schemas/`](../schemas/)).
  The writer in `xuebuild` produces them; install it from the pin (with
  uv: `[tool.uv.sources] xuebuild = { path = "vendor/xue", editable = true }`)
  and expect its modules to move.
- A time axis holds offsets up to 65534 in units of at most an hour
  ([`bundle.md`](../bundle.md)), about seven and a half years. A longer
  record is several cases, joined by the catalog's `parts` or by your
  module calling `openCase` on the last frame.
- A root with cases only has no live pointer for a bare visit to open. Send
  a visit without `?case=` to one before the shell's module runs (below).

## Your module

It runs beside the shell's and reaches it through `window.xueViewer`
([the contract](frontend.md#embedding)). Declare the part of the interface
you use rather than importing the shell's module:

```ts
interface XueViewer {
  time(): number | null;
  source(): { model: string; variable: string; caseId: string | null } | null;
  on(event: "time" | "source", callback: () => void): () => void;
}
declare global {
  interface Window {
    xueViewer?: XueViewer;
  }
}

function viewer(): Promise<XueViewer> {
  if (window.xueViewer) return Promise.resolve(window.xueViewer);
  return new Promise((resolve) => {
    window.addEventListener("xue:ready", () => resolve(window.xueViewer!), { once: true });
  });
}

const xue = await viewer();
xue.on("source", () => console.log("showing", xue.source()));
xue.on("time", () => console.log("valid time", xue.time()));
```

- Draw on `xue.map` with layer and source ids under a prefix of your own;
  add, never edit or remove, the shell's.
- Read your own files from the same data root
  (`import.meta.env.VITE_DATA_BASE_URL`).
- What must exist before the shell's module runs goes into an inline
  script, a second tag from the plugin with `injectTo: "head-prepend"`:
  `window.xuePalettes`, the redirect of a bare visit to a case, and a copy
  of any query parameter of your own, since the shell rewrites the query
  string to its view state.
- The shell's elements are yours to hide with CSS but not to remove: its
  script reaches them. Ink for anything over the map follows
  `body[data-ground]`.

## The page's identity

The shell names its own site in three places, and a build on another host
corrects each:

- `index.html`'s static head (title, description, canonical and `hreflang`
  links, social card, JSON-LD, `noscript`): rewrite the HTML in
  `transformIndexHtml`, which may return `{ html, tags }`.
- `public/` (`llms.txt`, `robots.txt`, `_headers`, `.well-known/`,
  `og-image.jpg`, the icons), copied
  into the build as is: replace or delete them in the plugin's
  `closeBundle`.
- The head the shell rewrites as the view changes (`pagemeta.ts`, against
  `site.ts`): observe `document.head` from your module and put your
  origin and name back.

## Moving the pin

- Rebuild the decoder (`make wasm`) with every move: it comes from the same
  commit as the shell.
- Shell before data. A shell refuses a manifest or metadata version above
  the ones it implements, so move the pin and deploy before publishing
  data written by a newer `xuebuild`. Data already published stays
  readable: nothing is rebuilt for a newer shell.
- Re-check the selectors you hide or mount into, and type-check your module.

## Licences

The code is MIT or Apache-2.0. Published data keeps its sources' terms
([README](../../README.md#data-and-licensing)). The basemap and the relief
are third-party services with their own terms and attribution.

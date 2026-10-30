import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";

import {
  cellOf,
  cellQuery,
  coordinateError,
  encodeQueryValue,
  groupByGrid,
  manifestGrids,
  runDirectory,
  type Cell,
  type CellGrid,
  type GridGroup,
} from "./apiconsole/locate";
import { FORECAST_MODEL_IDS } from "./manifest";
import { DATA_ORIGIN } from "./site";
import { applyTheme, toggleTheme } from "./theme";

/**
 * The API console (api.html): build a request to the data API, send it from
 * the browser and read the answer, status and headers beside it.
 *
 * Grid reads go through `/v1/cell`, which addresses a grid cell, so the
 * console finds the cell for a latitude and longitude itself
 * (`apiconsole/locate.ts`), from the run's manifest on the dataset origin.
 * `/v1/point` is left out: a client that repeats a place asks by cell, and
 * the console shows it doing so. Everything else is a form over the route's
 * parameters, kept in the page's query so a request is a link.
 *
 * English only, like the API's own documentation: parameter names, codes
 * and JSON are the content here.
 */

const DEFAULT_API = "https://xue-api.ringsaturn.me";

interface Param {
  name: string;
  in: "path" | "query";
  required?: boolean;
  placeholder?: string;
}

interface Route {
  path: string;
  params: Param[];
  /** The bundle chips and the lat/lon → cell locator. */
  locator?: boolean;
}

const SOURCE: Param = { name: "source", in: "query", required: true };
const ISSUE: Param = { name: "issue", in: "query", placeholder: "live" };
const TIME: Param = { name: "time", in: "query", placeholder: "ISO 8601, nearest frame" };
const SPATIAL: Param[] = [
  { name: "lat", in: "query" },
  { name: "lon", in: "query" },
  { name: "radius", in: "query", placeholder: "km" },
  { name: "bbox", in: "query", placeholder: "west,south,east,north" },
  { name: "limit", in: "query" },
];

const CELL_ROUTE: Route = {
  path: "/v1/cell",
  locator: true,
  params: [
    SOURCE,
    { name: "run", in: "query", required: true, placeholder: "latest" },
    { name: "row", in: "query", required: true },
    { name: "column", in: "query", required: true },
    { name: "variables", in: "query", placeholder: "the run's first bundle" },
    TIME,
  ],
};

const ROUTES: readonly Route[] = [
  CELL_ROUTE,
  { path: "/v1/sources/{source}", params: [{ ...SOURCE, in: "path" }] },
  { path: "/v1/catalog", params: [] },
  { path: "/v1/soundings", params: [...SPATIAL, ISSUE] },
  { path: "/v1/soundings/{station}", params: [{ name: "station", in: "path", required: true, placeholder: "47401" }, TIME, ISSUE] },
  { path: "/v1/airports", params: [...SPATIAL, { name: "category", in: "query", placeholder: "VFR, MVFR, IFR, LIFR" }, ISSUE] },
  { path: "/v1/airports/{icao}", params: [{ name: "icao", in: "path", required: true, placeholder: "RJTT" }, TIME, ISSUE] },
  { path: "/v1/synop", params: [...SPATIAL, { name: "network", in: "query" }, ISSUE] },
  { path: "/v1/synop/{station}", params: [{ name: "station", in: "path", required: true, placeholder: "amedas:44132" }, TIME, ISSUE] },
  { path: "/v1/storms", params: [{ name: "basin", in: "query" }, { name: "level", in: "query" }, ISSUE] },
  { path: "/v1/storms/{storm}", params: [{ name: "storm", in: "path", required: true }, { name: "include", in: "query" }, ISSUE] },
  { path: "/v1/openapi.json", params: [] },
  { path: "/health", params: [] },
];

/** Locator inputs on `/v1/cell`: they find `row` and `column`, they are not
 * parameters of the request. */
const LOCATOR_KEYS = ["lat", "lon"] as const;

function byId<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

applyTheme();
byId("theme-toggle").addEventListener("click", () => toggleTheme());

const apiInput = byId<HTMLInputElement>("api-base");
const routeSelect = byId<HTMLSelectElement>("api-route");
const fieldsBox = byId<HTMLDivElement>("api-fields");
const bundlesBox = byId<HTMLDivElement>("api-bundles");
const locatorBox = byId<HTMLElement>("api-locator");
const requestUrl = byId<HTMLAnchorElement>("api-request-url");
const sendButton = byId<HTMLButtonElement>("api-send");
const copyUrlButton = byId<HTMLButtonElement>("api-copy-url");
const copyCurlButton = byId<HTMLButtonElement>("api-copy-curl");
const formError = byId<HTMLParagraphElement>("api-form-error");
const responseBox = byId<HTMLElement>("api-response");
const docsLink = byId<HTMLAnchorElement>("api-docs-link");

for (const route of ROUTES) routeSelect.append(new Option(route.path, route.path));

// One value per parameter name across routes, so switching routes keeps
// the source, the coordinate and the times already typed.
const values = new Map<string, string>();
let route: Route = CELL_ROUTE;

function readQuery(): void {
  const params = new URLSearchParams(window.location.search);
  apiInput.value = params.get("api") || DEFAULT_API;
  route = ROUTES.find((candidate) => candidate.path === params.get("route")) ?? CELL_ROUTE;
  for (const [key, value] of params) if (key !== "api" && key !== "route") values.set(key, value);
  if (!values.has("source")) values.set("source", "gfs");
  if (!values.has("lat") && !values.has("lon")) {
    values.set("lat", "35.68");
    values.set("lon", "139.77");
  }
}

/** The page's own query, spelled like the request's so it stays readable
 * (`route=/v1/cell`, `variables=tmp2m,wind10m`). */
function writeQuery(): void {
  const pairs: [string, string][] = [["route", route.path]];
  if (apiInput.value.trim() !== DEFAULT_API) pairs.push(["api", apiInput.value.trim()]);
  const keys = new Set(route.params.map((param) => param.name));
  if (route.locator) for (const key of LOCATOR_KEYS) keys.add(key);
  for (const key of keys) {
    const value = values.get(key)?.trim();
    if (value) pairs.push([key, value]);
  }
  const search = pairs.map(([key, value]) => `${key}=${encodeQueryValue(value)}`).join("&");
  window.history.replaceState(null, "", `${window.location.pathname}?${search}${window.location.hash}`);
}

function apiBase(): string {
  return apiInput.value.trim().replace(/\/+$/, "") || DEFAULT_API;
}

// ---------------------------------------------------------------------------
// Metadata: the catalog from the API, a run's grids from the dataset origin.

interface Catalog {
  dataOrigin: string;
  sources: { id: string; title: string }[];
}

interface RunGrids {
  /** The run as `/v1/cell` takes it: the manifest's directory. */
  run: string;
  grids: Map<string, CellGrid | null>;
}

let catalog: Promise<Catalog> | null = null;
let catalogFor = "";
const runs = new Map<string, Promise<RunGrids>>();

async function getJson(url: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`${response.status} from ${url}`);
  return response.json();
}

function loadCatalog(): Promise<Catalog> {
  if (catalog && catalogFor === apiBase()) return catalog;
  catalogFor = apiBase();
  runs.clear();
  catalog = getJson(`${catalogFor}/v1/catalog`).then(
    (body) => {
      const value = body as { dataOrigin?: unknown; collections?: { id?: unknown; title?: unknown }[] };
      return {
        dataOrigin: typeof value.dataOrigin === "string" ? value.dataOrigin : DATA_ORIGIN,
        sources: (value.collections ?? [])
          .filter((entry) => typeof entry.id === "string")
          .map((entry) => ({ id: entry.id as string, title: typeof entry.title === "string" ? entry.title : "" })),
      };
    },
    () => ({ dataOrigin: DATA_ORIGIN, sources: [] }),
  );
  return catalog;
}

/** The live run (Collection → pointer → manifest) or a pinned one. The
 * Collection and the pointer are mutable, so a live lookup is held for a
 * minute; a pinned manifest never changes. */
function loadRun(source: string, run: string): Promise<RunGrids> {
  const live = run === "" || run === "latest";
  const key = `${source}|${live ? "latest" : run}`;
  const cached = runs.get(key);
  if (cached) return cached;
  const promise = (async (): Promise<RunGrids> => {
    const { dataOrigin } = await loadCatalog();
    let directory: string;
    if (live) {
      const collection = (await getJson(`${dataOrigin}${source}/collection.json`, { cache: "no-cache" })) as Record<string, unknown>;
      const pointerPath = collection["xue:pointer"];
      if (typeof pointerPath !== "string") throw new Error(`no_live_run: ${source} names no live pointer`);
      const pointer = (await getJson(`${dataOrigin}${pointerPath}`, { cache: "no-cache" })) as Record<string, unknown>;
      if (typeof pointer.manifestPath !== "string") throw new Error(`not_a_grid: ${source} is a point product`);
      directory = pointer.manifestPath.replace(/\/manifest\.json$/, "");
    } else {
      const resolved = runDirectory(run);
      if (resolved === null) throw new Error(`invalid_parameter: ${run} is not a run name`);
      directory = resolved;
    }
    const manifest = await getJson(`${dataOrigin}${directory}/manifest.json`);
    return { run: directory, grids: manifestGrids(manifest) };
  })();
  runs.set(key, promise);
  promise.catch(() => runs.delete(key));
  if (live) window.setTimeout(() => runs.delete(key), 60_000);
  return promise;
}

// ---------------------------------------------------------------------------
// The form.

function selectedBundles(): string[] {
  return (values.get("variables") ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function renderFields(): void {
  fieldsBox.replaceChildren();
  // On `/v1/cell` the locator's lat/lon sit after the source, before the
  // row and column they fill.
  const params: Param[] =
    route.locator
      ? [SOURCE, { name: "lat", in: "query" }, { name: "lon", in: "query" }, ...route.params.slice(1)]
      : route.params;
  for (const param of params) {
    const label = element("label", "api-field");
    const locator = route.locator && (LOCATOR_KEYS as readonly string[]).includes(param.name);
    const name = element("span", undefined, param.name);
    if (param.required) name.append(element("span", "api-required", " *"));
    if (locator) name.append(element("span", "api-field-note", " → row, column"));
    label.append(name);
    const input = element("input");
    input.name = param.name;
    input.value = values.get(param.name) ?? "";
    input.placeholder = param.placeholder ?? "";
    input.spellcheck = false;
    input.autocomplete = "off";
    if (param.name === "source") input.setAttribute("list", "api-source-list");
    if (param.name === "variables" || param.name === "bbox") label.classList.add("is-wide");
    input.addEventListener("input", () => {
      values.set(param.name, input.value);
      update();
    });
    label.append(input);
    fieldsBox.append(label);
  }
}

function setField(name: string, value: string): void {
  values.set(name, value);
  const input = fieldsBox.querySelector<HTMLInputElement>(`input[name="${name}"]`);
  if (input && input.value !== value) input.value = value;
}

function renderBundles(grids: Map<string, CellGrid | null> | null): void {
  bundlesBox.replaceChildren();
  bundlesBox.hidden = grids === null || grids.size === 0;
  if (!grids) return;
  const chosen = new Set(selectedBundles());
  for (const bundle of grids.keys()) {
    const chip = element("button", "api-chip", bundle);
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(chosen.has(bundle)));
    chip.addEventListener("click", () => {
      const next = selectedBundles();
      const at = next.indexOf(bundle);
      if (at >= 0) next.splice(at, 1);
      else next.push(bundle);
      setField("variables", next.join(","));
      update();
    });
    bundlesBox.append(chip);
  }
}

// ---------------------------------------------------------------------------
// The locator: lat/lon → row/column on each grid the chosen bundles use.

interface Located {
  run: string;
  groups: { group: GridGroup; cell: Cell | null }[];
  /** The one cell every chosen bundle shares, when they share a grid. */
  cell: Cell | null;
}

let located: Located | null = null;
let locateToken = 0;

function gridLabel(grid: CellGrid): string {
  return `${grid.width} × ${grid.height}, ${Math.abs(grid.longitudeStep)}° from ${grid.firstLongitude}, ${grid.firstLatitude}`;
}

function cellLabel(cell: Cell): string {
  return `row ${cell.row}, column ${cell.column} · centre ${cell.latitude}, ${cell.longitude}`;
}

async function locate(): Promise<void> {
  const token = ++locateToken;
  located = null;
  locatorBox.replaceChildren();
  locatorBox.hidden = !route.locator;
  if (!route.locator) {
    renderBundles(null);
    return;
  }
  const source = values.get("source")?.trim() ?? "";
  const run = values.get("run")?.trim() ?? "";
  if (!source) {
    renderBundles(null);
    return;
  }
  const status = element("p", "api-locator-status", `Reading ${source}'s ${run || "live"} run…`);
  locatorBox.append(status);
  let grids: RunGrids;
  try {
    grids = await loadRun(source, run);
  } catch (error) {
    if (token !== locateToken) return;
    renderBundles(null);
    status.textContent = error instanceof Error ? error.message : String(error);
    status.classList.add("is-error");
    return;
  }
  if (token !== locateToken) return;
  renderBundles(grids.grids);

  // Without a coordinate the locator only lists the grids, and a row and
  // column typed by hand stand.
  const latText = values.get("lat")?.trim() ?? "";
  const lonText = values.get("lon")?.trim() ?? "";
  const hasCoordinate = latText !== "" && lonText !== "";
  const latitude = Number(latText);
  const longitude = Number(lonText);
  const asked = selectedBundles();
  const bundles = asked.length > 0 ? asked : [...grids.grids.keys()].slice(0, 1);
  const unknown = bundles.filter((bundle) => !grids.grids.has(bundle));
  const unsupported = bundles.filter((bundle) => grids.grids.get(bundle) === null);
  const groups = groupByGrid(grids.grids, bundles).map((group) => ({
    group,
    cell: hasCoordinate ? cellOf(group.grid, latitude, longitude) : null,
  }));
  located = { run: grids.run, groups, cell: groups.length === 1 ? (groups[0]?.cell ?? null) : null };

  status.textContent = `run ${grids.run}${run === "" || run === "latest" ? " (live)" : ""}`;
  const coordinate = hasCoordinate ? coordinateError(latitude, longitude) : null;
  if (coordinate) locatorBox.append(element("p", "api-locator-status is-error", coordinate));
  if (unknown.length > 0) locatorBox.append(element("p", "api-locator-status is-error", `unknown_variable: ${unknown.join(", ")}`));
  if (unsupported.length > 0) {
    locatorBox.append(element("p", "api-locator-status is-error", `grid order this page cannot address: ${unsupported.join(", ")}`));
  }

  const list = element("ul", "api-groups");
  for (const { group, cell } of groups) {
    const item = element("li", "api-group");
    item.append(element("span", "api-group-bundles", group.bundles.join(", ")));
    item.append(element("span", "api-group-grid", gridLabel(group.grid)));
    if (cell) item.append(element("span", "api-group-cell", cellLabel(cell)));
    else if (hasCoordinate) item.append(element("span", "api-group-cell is-error", "point_off_grid"));
    if (route.locator && cell && groups.length > 1) {
      const use = element("button", "api-chip", "use");
      use.type = "button";
      use.addEventListener("click", () => {
        setField("variables", group.bundles.join(","));
        update();
      });
      item.append(use);
    }
    list.append(item);
  }
  locatorBox.append(list);
  if (route.locator && groups.length > 1) {
    locatorBox.append(
      element("p", "api-locator-status is-error", "grid_mismatch: these bundles are on different grids, one /v1/cell request each"),
    );
  }
  // The cell follows the coordinate; one that is off this grid, or a lookup
  // that splits across grids, leaves no stale cell of another source behind.
  if (route.locator && hasCoordinate) {
    const cell = coordinate ? null : located.cell;
    setField("row", cell ? String(cell.row) : "");
    setField("column", cell ? String(cell.column) : "");
  }
  renderRequest();
}

// ---------------------------------------------------------------------------
// The request.

interface Built {
  url: string;
  error: string | null;
}

function buildRequest(): Built {
  const base = apiBase();
  if (route.locator) {
    const source = values.get("source")?.trim() ?? "";
    const typed = values.get("run")?.trim() ?? "";
    const live = typed === "" || typed === "latest";
    const run = live ? (located?.run ?? "") : (runDirectory(typed) ?? typed);
    const row = values.get("row")?.trim() ?? "";
    const column = values.get("column")?.trim() ?? "";
    const query = cellQuery({
      source,
      run: run || "latest",
      row,
      column,
      variables: selectedBundles(),
      time: values.get("time")?.trim() || undefined,
    });
    const missing = !source ? "source" : !run ? "run (resolving the live run…)" : !/^\d+$/.test(row) ? "row" : !/^\d+$/.test(column) ? "column" : null;
    return { url: `${base}/v1/cell?${query}`, error: missing ? `missing ${missing}` : null };
  }
  let path = route.path;
  const query: string[] = [];
  let error: string | null = null;
  for (const param of route.params) {
    const value = values.get(param.name)?.trim() ?? "";
    if (!value) {
      if (param.required && !error) error = `missing ${param.name}`;
      continue;
    }
    if (param.in === "path") path = path.replace(`{${param.name}}`, encodeURIComponent(value));
    else query.push(`${param.name}=${encodeQueryValue(value)}`);
  }
  return { url: `${base}${path}${query.length > 0 ? `?${query.join("&")}` : ""}`, error };
}

function renderRequest(): void {
  const built = buildRequest();
  requestUrl.textContent = built.url;
  requestUrl.href = built.url;
  formError.textContent = built.error ?? "";
  formError.hidden = built.error === null;
  sendButton.disabled = built.error !== null;
  docsLink.href = `${apiBase()}/docs/`;
  writeQuery();
}

function update(): void {
  renderRequest();
  void locate();
}

// ---------------------------------------------------------------------------
// The response.

const SHOWN_HEADERS = ["cache-control", "x-xue-run", "x-xue-issue", "x-xue-ms", "x-xue-api", "cf-cache-status", "age", "retry-after"];

function checkCell(body: unknown, sent: Built): string | null {
  const cell = (body as { cell?: { row?: unknown; column?: unknown } } | null)?.cell;
  if (!cell || typeof cell.row !== "number" || typeof cell.column !== "number") return null;
  if (!route.locator) return null;
  const params = new URL(sent.url).searchParams;
  const same = String(cell.row) === params.get("row") && String(cell.column) === params.get("column");
  return same ? "cell matches the request" : `cell (${cell.row}, ${cell.column}) differs from the request`;
}

async function send(): Promise<void> {
  const built = buildRequest();
  if (built.error) return;
  sendButton.disabled = true;
  responseBox.hidden = false;
  responseBox.replaceChildren(element("p", "api-locator-status", `GET ${built.url}`));
  const started = performance.now();
  let response: Response;
  let text: string;
  try {
    response = await fetch(built.url, { cache: "no-store" });
    text = await response.text();
  } catch (error) {
    responseBox.replaceChildren(element("p", "api-locator-status is-error", `request failed: ${error instanceof Error ? error.message : String(error)}`));
    sendButton.disabled = false;
    return;
  }
  const elapsed = Math.round(performance.now() - started);
  sendButton.disabled = false;

  const head = element("div", "api-status");
  head.append(element("span", response.ok ? "api-code" : "api-code is-error", `${response.status} ${response.statusText}`.trim()));
  head.append(element("span", "api-meta", `${elapsed} ms in the browser`));
  head.append(element("span", "api-meta", `${new TextEncoder().encode(text).length.toLocaleString("en")} bytes`));

  const headers = element("dl", "api-headers");
  for (const name of SHOWN_HEADERS) {
    const value = response.headers.get(name);
    if (value === null) continue;
    headers.append(element("dt", undefined, name), element("dd", undefined, value));
  }

  let body: unknown = null;
  let pretty = text;
  try {
    body = JSON.parse(text);
    pretty = JSON.stringify(body, null, 2);
  } catch {
    // Not JSON (a proxy's error page): shown as it came.
  }
  const nodes: Node[] = [head, headers];
  const check = checkCell(body, built);
  if (check) nodes.push(element("p", check.includes("matches") ? "api-locator-status" : "api-locator-status is-error", check));
  nodes.push(element("pre", "api-body", pretty));
  responseBox.replaceChildren(...nodes);
}

function curlCommand(url: string): string {
  return `curl -sS --compressed '${url.replace(/'/g, "'\\''")}'`;
}

async function copy(button: HTMLButtonElement, text: string): Promise<void> {
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "copied";
  } catch {
    button.textContent = "copy failed";
  }
  window.setTimeout(() => (button.textContent = label), 1200);
}

routeSelect.addEventListener("change", () => {
  route = ROUTES.find((candidate) => candidate.path === routeSelect.value) ?? CELL_ROUTE;
  renderFields();
  void fillSources();
  update();
});
apiInput.addEventListener("change", () => {
  catalog = null;
  void fillSources();
  update();
});
byId<HTMLFormElement>("api-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void send();
});
copyUrlButton.addEventListener("click", () => void copy(copyUrlButton, buildRequest().url));
copyCurlButton.addEventListener("click", () => void copy(copyCurlButton, curlCommand(buildRequest().url)));

/** The source suggestions. `/v1/cell` reads grids only, so it is offered
 * the gridded sources the viewer knows, not the point products (soundings,
 * airports, stations, storms, fire detections); any id can still be typed. */
async function fillSources(): Promise<void> {
  const list = byId<HTMLDataListElement>("api-source-list");
  const { sources } = await loadCatalog();
  const grids = new Set<string>(FORECAST_MODEL_IDS);
  const offered = route.locator ? sources.filter((source) => grids.has(source.id)) : sources;
  list.replaceChildren(...offered.map((source) => new Option(source.title, source.id)));
}

readQuery();
routeSelect.value = route.path;
renderFields();
void fillSources();
update();

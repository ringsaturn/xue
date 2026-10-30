// Find a cell: resolve a coordinate to a grid cell of a run the way a client
// should, then read it through GET /v1/cell, whose answer is cached for a
// day. Everything except the last request is the static dataset (the
// Collection, the live pointer, the run manifest); the cell rule is the
// server's (xue::zarr::probe_cell), so the row and column match the ones
// /v1/point reports for the same point.
(() => {
  const form = document.getElementById("cell-form");
  if (!form) return;
  const $ = (id) => document.getElementById(id);
  const sourceSelect = $("cell-source");
  const variableList = $("cell-variables");
  const output = $("cell-output");
  const POINTER_TTL_MS = 60_000;
  const cache = new Map();

  // The pointer is mutable (re-read after a minute); a manifest is
  // immutable under its path, so it is held for the page's lifetime.
  async function json(url, ttl) {
    const held = cache.get(url);
    if (held && (ttl === undefined || Date.now() - held.at < ttl)) return held.value;
    const response = await fetch(url, { cache: "no-cache" });
    if (!response.ok) throw new Error(`${response.status} ${url}`);
    const value = await response.json();
    cache.set(url, { at: Date.now(), value });
    return value;
  }

  let dataOrigin = "https://dataset.ringsaturn.me/xue/";

  async function manifestOf(source, run) {
    let directory = run ? runDirectory(run) : null;
    if (run && !directory) throw new Error(`not a run name: ${run}`);
    if (!directory) {
      const collection = await json(`${dataOrigin}${source}/collection.json`, POINTER_TTL_MS);
      const pointerPath = collection["xue:pointer"];
      if (!pointerPath) throw new Error(`${source} has no live run`);
      const pointer = await json(`${dataOrigin}${pointerPath}`, POINTER_TTL_MS);
      if (!pointer.manifestPath) throw new Error(`${source} is a point product, not a grid`);
      directory = pointer.manifestPath.replace(/\/[^/]*$/, "");
    }
    const manifest = await json(`${dataOrigin}${directory}/manifest.json`);
    if (manifest.schemaVersion !== 5) throw new Error(`manifest schemaVersion ${manifest.schemaVersion} is not 5`);
    return { run: directory, manifest };
  }

  // xue::zarr::run_directory: an Item id whose last dot-part is four digits
  // splits there (mrms.2026092509.1455 is mrms.2026092509/1455).
  function runDirectory(run) {
    if (!run || !/^[A-Za-z0-9._/-]+$/.test(run)) return null;
    if (run.split("/").some((segment) => segment === "" || /^\.+$/.test(segment))) return null;
    if (run.includes("/")) return run;
    const parts = run.split(".");
    const last = parts[parts.length - 1];
    return parts.length >= 2 && /^[0-9]{4}$/.test(last) ? `${parts.slice(0, -1).join(".")}/${last}` : run;
  }

  function gridOf(record) {
    const grid = {
      width: record.width ?? 0,
      height: record.height ?? 0,
      firstLongitude: record.firstLongitude ?? -180,
      firstLatitude: record.firstLatitude ?? 90,
      longitudeStep: record.longitudeStep ?? 0.25,
      latitudeStep: record.latitudeStep ?? -0.25,
    };
    grid.wrapLongitude = record.wrapLongitude ?? Math.abs(grid.width * grid.longitudeStep - 360) < 1e-6;
    return grid;
  }

  // The rule, in the server's order of operations. JavaScript's % is fmod.
  const wrap = (value, modulus) => ((value % modulus) + modulus) % modulus;
  function probe(grid, lon, lat) {
    if (!grid.width || !grid.height || !grid.longitudeStep || !grid.latitudeStep) return null;
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
    let column = Math.floor(wrap(lon - grid.firstLongitude, 360) / grid.longitudeStep + 0.5);
    if (grid.wrapLongitude) column = wrap(column, grid.width);
    else if (column < 0 || column >= grid.width) return null;
    const row = Math.floor((lat - grid.firstLatitude) / grid.latitudeStep + 0.5);
    if (row < 0 || row >= grid.height) return null;
    return {
      row,
      column,
      longitude: wrap(grid.firstLongitude + column * grid.longitudeStep + 180, 360) - 180,
      latitude: grid.firstLatitude + row * grid.latitudeStep,
    };
  }

  // The canonical spelling: every client asking for this cell builds the
  // same URL, which is what lets them share one cached answer.
  const encode = (value) =>
    Array.from(new TextEncoder().encode(String(value)), (byte) => {
      const char = String.fromCharCode(byte);
      return /[A-Za-z0-9\-._~,/:]/.test(char) ? char : `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
    }).join("");
  const cellUrl = (source, run, cell, bundles) =>
    `${location.origin}/v1/cell?source=${encode(source)}&run=${encode(run)}&row=${cell.row}&column=${cell.column}` +
    `&variables=${encode([...new Set(bundles)].sort().join(","))}`;

  const element = (tag, attributes = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    node.append(...children);
    return node;
  };

  async function loadSources() {
    try {
      const catalog = await json(`${location.origin}/v1/catalog`, POINTER_TTL_MS);
      if (catalog.dataOrigin) dataOrigin = catalog.dataOrigin.endsWith("/") ? catalog.dataOrigin : `${catalog.dataOrigin}/`;
      for (const collection of catalog.collections ?? []) {
        sourceSelect.append(element("option", { value: collection.id }, `${collection.id} — ${collection.title ?? ""}`));
      }
      sourceSelect.value = [...sourceSelect.options].some((option) => option.value === "gfs") ? "gfs" : sourceSelect.value;
      await loadVariables();
    } catch (error) {
      output.replaceChildren(element("p", { class: "cell-error" }, `Could not read the catalog: ${error.message}`));
    }
  }

  async function loadVariables() {
    variableList.replaceChildren(element("span", { class: "cell-muted" }, "Reading the run manifest…"));
    try {
      const { manifest } = await manifestOf(sourceSelect.value, $("cell-run").value.trim());
      variableList.replaceChildren(
        ...manifest.bundles.map((bundle, index) =>
          element(
            "label",
            {},
            element("input", Object.assign({ type: "checkbox", value: bundle.variable }, index === 0 ? { checked: "" } : {})),
            ` ${bundle.variable}`,
          ),
        ),
      );
    } catch (error) {
      variableList.replaceChildren(element("span", { class: "cell-error" }, error.message));
    }
  }

  async function fetchCell(url, target) {
    target.textContent = "…";
    const started = performance.now();
    const response = await fetch(url);
    const elapsed = Math.round(performance.now() - started);
    const body = await response.json();
    const status = [
      `HTTP ${response.status}`,
      `edge cache ${response.headers.get("cf-cache-status") ?? "–"}`,
      `server ${response.headers.get("x-xue-ms") ?? "–"} ms`,
      `round trip ${elapsed} ms`,
      `Cache-Control: ${response.headers.get("cache-control") ?? "–"}`,
    ].join(" · ");
    target.replaceChildren(element("div", { class: "cell-muted" }, status), element("pre", {}, JSON.stringify(body, null, 2)));
  }

  async function find(event) {
    event.preventDefault();
    const lat = Number($("cell-lat").value);
    const lon = Number($("cell-lon").value);
    if (!(lat >= -90 && lat <= 90) || !(lon >= -180 && lon <= 360)) {
      output.replaceChildren(element("p", { class: "cell-error" }, "Latitude must be within [-90, 90] and longitude within [-180, 360]."));
      return;
    }
    const wanted = [...variableList.querySelectorAll("input:checked")].map((input) => input.value);
    if (!wanted.length) {
      output.replaceChildren(element("p", { class: "cell-error" }, "Pick at least one variable."));
      return;
    }
    output.replaceChildren(element("p", { class: "cell-muted" }, "Resolving…"));
    try {
      const source = sourceSelect.value;
      const { run, manifest } = await manifestOf(source, $("cell-run").value.trim());
      // One request per grid: the bundles of a run need not share one.
      const groups = new Map();
      for (const bundle of manifest.bundles.filter((bundle) => wanted.includes(bundle.variable))) {
        const grid = gridOf(bundle.grid ?? {});
        const key = JSON.stringify(grid);
        if (!groups.has(key)) groups.set(key, { grid, bundles: [] });
        groups.get(key).bundles.push(bundle.variable);
      }
      const blocks = [element("p", {}, `Run `, element("code", {}, run), ` · ${groups.size} grid${groups.size === 1 ? "" : "s"}`)];
      for (const { grid, bundles } of groups.values()) {
        const cell = probe(grid, lon, lat);
        const heading = `${bundles.join(", ")} — ${grid.width} × ${grid.height} grid`;
        if (!cell) {
          blocks.push(element("div", { class: "cell-group" }, element("strong", {}, heading), element("p", { class: "cell-muted" }, "The point is outside this grid.")));
          continue;
        }
        const url = cellUrl(source, run, cell, bundles);
        const result = element("div");
        const button = element("button", { type: "button", class: "btn" }, "Fetch");
        button.addEventListener("click", () => fetchCell(url, result).catch((error) => (result.textContent = error.message)));
        blocks.push(
          element(
            "div",
            { class: "cell-group" },
            element("strong", {}, heading),
            element("p", {}, `row ${cell.row}, column ${cell.column} · cell centre ${cell.latitude.toFixed(4)}, ${cell.longitude.toFixed(4)}`),
            element("p", {}, element("a", { href: url, target: "_blank", rel: "noopener" }, url), " ", button),
            result,
          ),
        );
      }
      output.replaceChildren(...blocks);
    } catch (error) {
      output.replaceChildren(element("p", { class: "cell-error" }, error.message));
    }
  }

  form.addEventListener("submit", find);
  sourceSelect.addEventListener("change", loadVariables);
  $("cell-run").addEventListener("change", loadVariables);
  loadSources();
})();

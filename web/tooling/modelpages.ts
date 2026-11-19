/** The datasets' pages: one copy of the built viewer per live dataset,
 * `<id>.html` beside `index.html`, with that dataset's head (`modelpage.ts`).
 *
 * Cloudflare Pages serves `hrrr.html` at `/hrrr` (and answers `/hrrr.html`
 * with a 308 to it), the path the shell reads its dataset from
 * (`modelpaths.ts`). One segment deep and without a trailing slash, so the
 * viewer's relative `./assets/…` still resolves to `/assets/…` under the
 * build's `base: "./"`; an `<id>/index.html` would not.
 *
 * A page name may not shadow anything else the site serves at the root — a
 * page, a directory, a public file — so the build fails on a dataset id
 * that would. The names are read off the build's own output and inputs.
 *
 * Node-only: imported by vite.config.ts, never by the page. */

import { existsSync, readdirSync } from "node:fs";
import { extname } from "node:path";
import type { Connect, Plugin } from "vite";

import { liveModelIds, type ForecastModelId } from "../src/manifest";
import { modelPageHead, PERSON_ID, SITE_DATASET_ID } from "../src/modelpage";
import { modelFromPathname } from "../src/modelpaths";
import { alternateUrls, pageUrl } from "../src/pagemeta";
import { SITE_NAME } from "../src/site";
import { archivePagePath, PAGE_PREFIXES } from "../src/tc/pages";

/** The datasets with a page on a build serving `sources` (the build's
 * `VITE_SOURCES`, comma-separated; empty serves every one) — the set the
 * page computes for itself from the same variable. */
export function modelPageIds(sources: string | undefined): ForecastModelId[] {
  const served = new Set(
    (sources ?? "")
      .split(",")
      .map((id) => id.trim())
      .filter((id) => id !== ""),
  );
  return liveModelIds((id) => served.size === 0 || served.has(id));
}

/** The root-relative paths of the datasets' pages, for the sitemap. */
export function modelPagePaths(ids: readonly string[]): string[] {
  return ids.map((id) => `/${id}`);
}

/** The root name an output or input path takes: its first segment, without
 * the extension a file at the root has (`showcase.html` → `showcase`,
 * `assets/x.js` → `assets`, `/zh/tc/` → `zh`). */
export function rootStem(path: string): string {
  const first = path.replace(/^\/+/, "").split("/")[0] ?? "";
  return first.slice(0, first.length - extname(first).length).toLowerCase();
}

/** Every root name the site has besides the datasets' pages: the build's
 * output (the pages, `assets/`, the storm pages, the discovery files), the
 * public directory's entries, the HTML pages of the source tree, and the
 * storm pages' prefixes, which a build without the storm archive does not
 * emit but the deployed site may still link to. */
export function reservedStems(outputFiles: Iterable<string>, publicEntries: Iterable<string>, sourcePages: Iterable<string>): Set<string> {
  const stems = new Set<string>();
  for (const path of [...outputFiles, ...publicEntries, ...sourcePages]) stems.add(rootStem(path));
  for (const locale of Object.keys(PAGE_PREFIXES)) stems.add(rootStem(archivePagePath(locale)));
  stems.delete("");
  return stems;
}

/** Throws when a dataset's page would take a name the site already uses. */
export function checkModelPageNames(ids: readonly string[], reserved: ReadonlySet<string>): void {
  const taken = ids.filter((id) => reserved.has(id.toLowerCase()));
  if (taken.length > 0) {
    throw new Error(`dataset page ${taken.map((id) => `/${id}`).join(", ")} collides with a page, directory or public file of the same name`);
  }
}

function escapeAttribute(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeText(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Replace exactly one match, or fail: a head the build cannot find its
 * way around must not ship half-rewritten. */
function replaceOnce(html: string, pattern: RegExp, replace: (match: string) => string, what: string): string {
  const matches = html.match(new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`));
  if (!matches || matches.length !== 1) throw new Error(`dataset page: expected one ${what} in index.html, found ${matches?.length ?? 0}`);
  return html.replace(pattern, replace);
}

function setMetaContent(html: string, attribute: "name" | "property", key: string, content: string): string {
  const pattern = new RegExp(`<meta ${attribute}="${key}"[^>]*>`);
  return replaceOnce(
    html,
    pattern,
    (tag) => tag.replace(/ data-i18n-content="[^"]*"/, "").replace(/ content="[^"]*"/, ` content="${escapeAttribute(content)}"`),
    `${key} meta`,
  );
}

/** The viewer's `index.html` as one dataset's page: its title, description,
 * canonical, hreflang set and social card name the dataset's path, and its
 * `Dataset` joins the page's JSON-LD graph. The description loses its i18n
 * key, so the shell's static-message pass leaves the dataset's own copy in
 * place (the shell rewrites the head itself once it knows the view). */
export function renderModelPage(indexHtml: string, id: ForecastModelId): string {
  const head = modelPageHead(id);
  const title = `${head.title} · ${SITE_NAME}`;
  let html = replaceOnce(indexHtml, /<title>[^<]*<\/title>/, () => `<title>${escapeText(title)}</title>`, "title");
  html = setMetaContent(html, "name", "description", head.description);
  html = setMetaContent(html, "property", "og:title", title);
  html = setMetaContent(html, "property", "og:description", head.description);
  html = setMetaContent(html, "property", "og:url", pageUrl(head.path, null));
  html = setMetaContent(html, "name", "twitter:title", title);
  html = setMetaContent(html, "name", "twitter:description", head.description);
  html = replaceOnce(
    html,
    /<link rel="canonical" href="[^"]*" \/>/,
    () => `<link rel="canonical" href="${escapeAttribute(pageUrl(head.path, null))}" />`,
    "canonical link",
  );
  for (const { hreflang, href } of alternateUrls(head.path)) {
    html = replaceOnce(
      html,
      new RegExp(`<link rel="alternate" hreflang="${hreflang}" href="[^"]*" />`),
      () => `<link rel="alternate" hreflang="${hreflang}" href="${escapeAttribute(href)}" />`,
      `${hreflang} alternate`,
    );
  }
  html = replaceOnce(
    html,
    /(<script type="application\/ld\+json">)([\s\S]*?)(<\/script>)/,
    (block) => {
      const [, open, json, close] = /(<script type="application\/ld\+json">)([\s\S]*?)(<\/script>)/.exec(block)!;
      const document = JSON.parse(json!) as { "@graph"?: Record<string, unknown>[] };
      const graph = document["@graph"];
      if (!Array.isArray(graph)) throw new Error("dataset page: index.html's JSON-LD has no @graph");
      for (const needed of [PERSON_ID, SITE_DATASET_ID]) {
        if (!graph.some((node) => node["@id"] === needed)) throw new Error(`dataset page: index.html's JSON-LD has no ${needed}`);
      }
      graph.push(head.dataset);
      // `<` inside a script would end it early in a hostile string; none of
      // the table's strings has one, but the escape costs nothing.
      const body = JSON.stringify(document, null, 2).replace(/</g, "\\u003c");
      return `${open}\n${body}\n    ${close}`;
    },
    "JSON-LD block",
  );
  return html;
}

interface ModelPagesOptions {
  /** The datasets to write a page for (`modelPageIds`). */
  ids: readonly ForecastModelId[];
  /** The public directory, whose entries are served at the root too. */
  publicDir: string;
  /** The source tree's page directory (`web/`), whose HTML files are pages. */
  pagesDir: string;
}

/** Vite plugin: emit `<id>.html` per dataset after the viewer's
 * `index.html` exists (the hook runs after Vite's own HTML step), failing
 * the build on a name collision. Build only. */
export function modelPages({ ids, publicDir, pagesDir }: ModelPagesOptions): Plugin {
  return {
    name: "xue:model-pages",
    apply: "build",
    enforce: "post",
    generateBundle: {
      order: "post",
      handler(_options, bundle) {
        const index = bundle["index.html"];
        if (!index || index.type !== "asset") throw new Error("dataset pages: the build emitted no index.html");
        const publicEntries = existsSync(publicDir) ? readdirSync(publicDir) : [];
        const sourcePages = readdirSync(pagesDir).filter((name) => name.endsWith(".html"));
        checkModelPageNames(ids, reservedStems(Object.keys(bundle), publicEntries, sourcePages));
        const indexHtml = typeof index.source === "string" ? index.source : new TextDecoder().decode(index.source);
        for (const id of ids) this.emitFile({ type: "asset", fileName: `${id}.html`, source: renderModelPage(indexHtml, id) });
        this.info(`dataset pages: ${ids.length}`);
      },
    },
  };
}

/** Vite plugin: serve a dataset's path on the dev and preview servers the
 * way Pages does — the viewer on the dev server, where `index.html` is the
 * source, and the built `<id>.html` on the preview server. Only the
 * datasets' own ids are rewritten; everything else reaches the server as
 * it was. */
export function modelPathRewrite(ids: readonly string[]): Plugin {
  const pathIds = new Set(ids);
  const rewrite =
    (target: (id: string) => string): Connect.NextHandleFunction =>
    (req, _res, next) => {
      if (req.url) {
        const url = new URL(req.url, "http://localhost");
        // Only the canonical spelling, as on Pages: a spelling of it is the
        // shell's to tidy, from the viewer at whatever path served it.
        const id = modelFromPathname(url.pathname, pathIds);
        if (id !== null && url.pathname === `/${id}`) req.url = `${target(id)}${url.search}`;
      }
      next();
    };
  return {
    name: "xue:model-paths",
    configureServer(server) {
      server.middlewares.use(rewrite(() => "/index.html"));
    },
    configurePreviewServer(server) {
      server.middlewares.use(rewrite((id) => `/${id}.html`));
    },
  };
}

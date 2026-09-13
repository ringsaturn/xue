/** The discovery files a crawler or an agent reads before the page: the
 * sitemap and `llms-full.txt`, emitted by the production build.
 *
 * Both are derived, not written: the sitemap's case pages come from the
 * *published* showcase catalog (`showcase.json` at the data root — the same
 * mutable index the showcase page lists), and `llms-full.txt` is the
 * project's own documentation — README, the format and encoder specs, the
 * case authoring guide — concatenated with its relative links rewritten to
 * the repository, so a model reading one file follows none into the void.
 * `llms.txt` (the short index) and `robots.txt` are prose and live as static
 * files in `web/public/`.
 *
 * The catalog, not the checked-in definitions under `showcase/cases/`, is
 * the source because a definition is only an input: it is authored first
 * and built and uploaded later, sometimes much later (a case pulls a whole
 * archived run), and a sitemap naming `/?case=<id>` for a case the viewer
 * cannot find sends every crawler to an error card.
 *
 * Node-only: imported by vite.config.ts, never by the page. */

import { existsSync, readFileSync } from "node:fs";
import { join, posix } from "node:path";
import type { Plugin } from "vite";

import { REPO_URL, SITE_ORIGIN } from "../src/site";

/** Root-relative pages every deploy has, before the cases. */
export const STATIC_PAGES: readonly string[] = ["/", "/showcase.html"];

/** The sitemap's URL set: the two pages plus one per case, absolute. */
export function sitemapUrls(caseIds: readonly string[]): string[] {
  return [
    ...STATIC_PAGES.map((path) => new URL(path, SITE_ORIGIN).href),
    ...caseIds.map((id) => `${SITE_ORIGIN}/?case=${encodeURIComponent(id)}`),
  ];
}

function escapeXml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      default:
        return "&apos;";
    }
  });
}

export function renderSitemap(urls: readonly string[]): string {
  const entries = urls.map((url) => `  <url>\n    <loc>${escapeXml(url)}</loc>\n  </url>`).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${entries}\n</urlset>\n`;
}

/** The catalog's filename at the data root; `xuebuild/showcase.py` writes it
 * and `web/src/showcase-catalog.ts` reads it. Repeated here rather than
 * imported because that module's graph reaches `window` at load time. */
export const CATALOG_FILENAME = "showcase.json";

/** Case ids from the published catalog, in catalog order. Only the shape the
 * sitemap needs is checked — the id must be the slug the viewer's `?case=`
 * accepts — and anything else is a build error: a catalog the page could
 * not list must not be advertised either. */
export function catalogCaseIds(catalogJson: string): string[] {
  const catalog = JSON.parse(catalogJson) as { schemaVersion?: unknown; cases?: unknown };
  if (catalog.schemaVersion !== 1) throw new Error("showcase catalog: unsupported schema version");
  if (!Array.isArray(catalog.cases)) throw new Error("showcase catalog: no case list");
  const ids = catalog.cases.map((entry: unknown, index) => {
    const id = (entry as { id?: unknown } | null)?.id;
    if (typeof id !== "string" || !/^[a-z0-9-]+$/.test(id)) throw new Error(`showcase catalog: case ${index} has no slug id`);
    return id;
  });
  if (new Set(ids).size !== ids.length) throw new Error("showcase catalog: duplicate case ids");
  return ids;
}

/** Fetch the catalog the deployed page will read. `dataBaseUrl` is the
 * build's `VITE_DATA_BASE_URL`: the public bucket on a deploy build, unset
 * (the local `web/public/data/` directory) otherwise. A local build without
 * a built case has no catalog and lists no case pages; a deploy whose bucket
 * does not answer fails, because a sitemap silently missing every case is
 * the wrong thing to ship. */
export async function loadCatalogCaseIds(dataBaseUrl: string | undefined, publicDir: string): Promise<string[]> {
  if (dataBaseUrl && /^https?:\/\//.test(dataBaseUrl)) {
    const url = new URL(CATALOG_FILENAME, dataBaseUrl);
    const response = await fetch(url, { cache: "no-cache" });
    if (!response.ok) throw new Error(`showcase catalog request failed (${response.status}) for ${url}`);
    return catalogCaseIds(await response.text());
  }
  const path = join(publicDir, dataBaseUrl || "data/", CATALOG_FILENAME);
  return existsSync(path) ? catalogCaseIds(readFileSync(path, "utf8")) : [];
}

/** One document folded into `llms-full.txt`. */
export interface DocSource {
  /** Repository-relative path, for the section heading and link rewriting. */
  repoPath: string;
  markdown: string;
}

/** Point a document's relative links at the repository so they survive
 * being read away from it. Absolute URLs, mail links and in-page anchors
 * are left alone; a relative target is resolved against the document's own
 * directory (`../README.md` from `docs/` is `README.md`). */
export function absolutizeLinks(markdown: string, repoPath: string): string {
  const dir = posix.dirname(repoPath);
  return markdown.replace(/\]\((?![a-z][a-z0-9+.-]*:|#)([^)\s]+)\)/gi, (_match, target: string) => {
    const resolved = posix.normalize(posix.join(dir, target)).replace(/^\.\//, "");
    return `](${REPO_URL}/blob/main/${resolved})`;
  });
}

export function renderLlmsFull(docs: readonly DocSource[]): string {
  const header = [
    "# Xue — full documentation",
    "",
    `> Concatenated from the repository (${REPO_URL}) at build time: the README, the`,
    "> normative format specification, the native encoder notes, the tropical",
    `> cyclone product specification and the case authoring guide. The short index is ${SITE_ORIGIN}/llms.txt.`,
    "",
  ].join("\n");
  const sections = docs.map(
    ({ repoPath, markdown }) =>
      `\n\n---\n\n<!-- source: ${repoPath} -->\n\n${absolutizeLinks(markdown, repoPath).trimEnd()}\n`,
  );
  return header + sections.join("");
}

/** The documents `llms-full.txt` carries, in reading order. */
export const LLMS_FULL_SOURCES: readonly string[] = [
  "README.md",
  "docs/format.md",
  "docs/encoder.md",
  "docs/tc.md",
  "showcase/README.md",
];

export interface DiscoveryOptions {
  /** Repository root: where the documents live. */
  repoRoot: string;
  /** The page's `VITE_DATA_BASE_URL` for this build mode, if set. */
  dataBaseUrl: string | undefined;
}

/** Vite plugin: emit `sitemap.xml` and `llms-full.txt` next to the pages.
 * Build only — the dev server serves the static `web/public/` files and
 * nothing here matters before deploy. */
export function discoveryFiles({ repoRoot, dataBaseUrl }: DiscoveryOptions): Plugin {
  return {
    name: "xue:discovery-files",
    apply: "build",
    async generateBundle() {
      const caseIds = await loadCatalogCaseIds(dataBaseUrl, join(repoRoot, "web", "public"));
      this.emitFile({
        type: "asset",
        fileName: "sitemap.xml",
        source: renderSitemap(sitemapUrls(caseIds)),
      });
      this.emitFile({
        type: "asset",
        fileName: "llms-full.txt",
        source: renderLlmsFull(
          LLMS_FULL_SOURCES.map((repoPath) => ({ repoPath, markdown: readFileSync(join(repoRoot, repoPath), "utf8") })),
        ),
      });
    },
  };
}

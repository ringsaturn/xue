/** The pages and discovery files the production build derives: every
 * language's copy of the shell's pages, the datasets' and the cases'
 * (`sitepages.ts`), the storm and airport pages, the sitemap and
 * `llms-full.txt`.
 *
 * All are derived, not written: the case pages come from the
 * *published* showcase catalog (`showcase.json` at the data root — the same
 * mutable index the showcase page lists), and `llms-full.txt` is the
 * project's user-facing documentation — README, the source notes, the specs,
 * the case authoring guide — concatenated with its relative links rewritten to
 * the repository, so a model reading one file follows none into the void.
 * `llms.txt` (the short index) and `robots.txt` are prose and live as static
 * files in `web/public/`.
 *
 * The catalog, not the checked-in definitions under `showcase/cases/`, is
 * the source because a definition is only an input: it is authored first
 * and built and uploaded later, sometimes much later (a case pulls a whole
 * archived run), and a page for a case the viewer cannot find sends every
 * crawler to an error card.
 *
 * Node-only: imported by vite.config.ts, never by the page. */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, posix } from "node:path";
import type { Plugin } from "vite";

import type { ForecastModelId } from "../src/manifest";
import { CATALOG_FILENAME, validateCatalog, type ShowcaseCase } from "../src/showcase-catalog";
import { REPO_URL, SITE_ORIGIN } from "../src/site";
import { splitLocalePath } from "../src/sitepaths";
import { buildAirportSite, liveScript, loadAirportCards, loadAirports } from "./airportpages";
import { checkModelPageNames, datasetProvenance, readStacProse, reservedStems } from "./modelpages";
import { buildSitePages, UNLISTED_PATHS } from "./sitepages";
import { buildStormSite, loadStorms } from "./stormpages";

/** The sitemap's URL set: every language's copy of the shell's pages
 * (`sitepages.ts`) but the unlisted ones, then the generated storm and
 * airport pages, absolute. */
export function sitemapUrls(sitePageUrls: readonly string[], pageUrls: readonly string[] = []): string[] {
  const listed = sitePageUrls.filter((url) => {
    return !UNLISTED_PATHS.has(splitLocalePath(new URL(url).pathname).path);
  });
  return [...listed, ...pageUrls];
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

export function renderSitemap(urls: readonly string[], lastmod: Readonly<Record<string, string>> = {}): string {
  const entries = urls
    .map((url) => {
      const day = lastmod[url];
      const modified = day === undefined ? "" : `\n    <lastmod>${escapeXml(day)}</lastmod>`;
      return `  <url>\n    <loc>${escapeXml(url)}</loc>${modified}\n  </url>`;
    })
    .join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${entries}\n</urlset>\n`;
}

/** The catalog's filename at the data root; `xuebuild/showcase.py` writes it
 * and `web/src/showcase-catalog.ts` reads it. */
export { CATALOG_FILENAME };

/** The catalog's text, as the deployed page will read it. `dataBaseUrl` is
 * the build's `VITE_DATA_BASE_URL`: the public bucket on a deploy build,
 * unset (the local `web/public/data/` directory) otherwise. A local build
 * without a built case has no catalog (null) and writes no case pages; a
 * deploy whose bucket does not answer fails, because a site silently
 * missing every case is the wrong thing to ship. */
export async function loadCatalogJson(dataBaseUrl: string | undefined, publicDir: string): Promise<string | null> {
  if (dataBaseUrl && /^https?:\/\//.test(dataBaseUrl)) {
    const url = new URL(CATALOG_FILENAME, dataBaseUrl);
    const response = await fetch(url, { cache: "no-cache" });
    if (!response.ok) throw new Error(`showcase catalog request failed (${response.status}) for ${url}`);
    return response.text();
  }
  const path = join(publicDir, dataBaseUrl || "data/", CATALOG_FILENAME);
  return existsSync(path) ? readFileSync(path, "utf8") : null;
}

/** One document folded into `llms-full.txt`. */
interface DocSource {
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
    "> per-source notes, the normative format and Zarr store specifications, the",
    "> native encoder notes, the tropical cyclone, airport and sounding product",
    "> specifications, the STAC catalog contract and the case authoring guide.",
    `> The short index is ${SITE_ORIGIN}/llms.txt.`,
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
  "docs/sources.md",
  "docs/zarr-profile.md",
  "docs/bundle.md",
  "docs/format.md",
  "docs/encoder.md",
  "docs/tc.md",
  "docs/airport.md",
  "docs/sounding.md",
  "docs/stac.md",
  "docs/api.md",
  "showcase/README.md",
];

interface DiscoveryOptions {
  /** Repository root: where the documents live. */
  repoRoot: string;
  /** The page's `VITE_DATA_BASE_URL` for this build mode, if set. */
  dataBaseUrl: string | undefined;
  /** The datasets with a page on this build (`modelpages.ts::modelPageIds`). */
  modelIds: readonly ForecastModelId[];
}

/** The catalog's cases and the day it was written, or none. */
function readCatalog(json: string | null): { cases: ShowcaseCase[]; day: string | null } {
  if (json === null) return { cases: [], day: null };
  // The same reading the shell does: a case it could not play gets no page.
  const catalog = validateCatalog(JSON.parse(json));
  const day = catalog.generatedAt.slice(0, 10);
  return { cases: catalog.cases, day: /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null };
}

/** Vite plugin: after Vite's own HTML step (the hook runs last), write every
 * language's copy of the shell's pages, the storm and airport pages,
 * `sitemap.xml` and `llms-full.txt`. Fails on a dataset id that would
 * shadow another root name. Build only — the dev server serves the source
 * pages (`sitepages.ts::sitePathRewrite`) and nothing here matters before
 * deploy. */
export function discoveryFiles({ repoRoot, dataBaseUrl, modelIds }: DiscoveryOptions): Plugin {
  return {
    name: "xue:discovery-files",
    apply: "build",
    enforce: "post",
    generateBundle: {
      order: "post",
      async handler(_options, bundle) {
        const publicDir = join(repoRoot, "web", "public");
        const sourcePages = readdirSync(join(repoRoot, "web")).filter((name) => name.endsWith(".html"));
        checkModelPageNames(modelIds, reservedStems(Object.keys(bundle), existsSync(publicDir) ? readdirSync(publicDir) : [], sourcePages));
        const built: Record<string, string> = {};
        for (const [fileName, output] of Object.entries(bundle)) {
          if (output.type !== "asset" || !fileName.endsWith(".html") || fileName.includes("/")) continue;
          built[fileName] = typeof output.source === "string" ? output.source : new TextDecoder().decode(output.source);
        }
        const catalog = readCatalog(await loadCatalogJson(dataBaseUrl, publicDir));
        const prose = readStacProse(join(repoRoot, "xuebuild", "stac_prose.json"));
        const site = buildSitePages({
          built,
          models: modelIds.map((id) => ({ id, provenance: datasetProvenance(prose, id) })),
          provenanceOf: (id) => datasetProvenance(prose, id),
          cases: catalog.cases,
          catalogDay: catalog.day,
        });
        // The built pages are replaced by their English copies, which name
        // their assets root-absolute.
        for (const file of site.files) {
          const existing = bundle[file.fileName];
          if (existing?.type === "asset") existing.source = file.source;
          else this.emitFile({ type: "asset", fileName: file.fileName, source: file.source });
        }
        this.info(`site pages: ${site.files.length} (${modelIds.length} datasets, ${catalog.cases.length} cases, every language)`);
        // The storm pages need the published archive; a build without a data
        // root (a local one) or with the archive unreachable ships the shell
        // without them rather than failing.
        let stormUrls: string[] = [];
        let stormLastmod: Record<string, string> = {};
        if (dataBaseUrl && /^https?:/.test(dataBaseUrl)) {
          try {
            const { storms, seasonPictures } = await loadStorms(dataBaseUrl);
            const stormSite = buildStormSite(storms, seasonPictures);
            for (const file of stormSite.files) this.emitFile({ type: "asset", fileName: file.fileName, source: file.source });
            stormUrls = stormSite.urls;
            stormLastmod = stormSite.lastmod;
            this.info(`storm pages: ${storms.length} storms, ${stormSite.files.length - 1} pages`);
          } catch (error) {
            this.warn(`storm pages skipped: ${error instanceof Error ? error.message : String(error)}`);
          }
        }
        // The airport pages come from a checked-in list, so every build has
        // them; their live block is the `airport` entry of this same build.
        // Their share cards are published apart; without a data root or the
        // card index the pages keep the site's card.
        const cards = dataBaseUrl && /^https?:/.test(dataBaseUrl) ? await loadAirportCards(dataBaseUrl) : new Map<string, string>();
        const airports = buildAirportSite(loadAirports(join(repoRoot, "web", "tooling", "airports.json")), liveScript(bundle), cards);
        for (const file of airports.files) this.emitFile({ type: "asset", fileName: file.fileName, source: file.source });
        this.emitFile({
          type: "asset",
          fileName: "sitemap.xml",
          source: renderSitemap(sitemapUrls(site.urls, [...stormUrls, ...airports.urls]), {
            ...site.lastmod,
            ...stormLastmod,
            ...airports.lastmod,
          }),
        });
        this.emitFile({
          type: "asset",
          fileName: "llms-full.txt",
          source: renderLlmsFull(
            LLMS_FULL_SOURCES.map((repoPath) => ({ repoPath, markdown: readFileSync(join(repoRoot, repoPath), "utf8") })),
          ),
        });
      },
    },
  };
}

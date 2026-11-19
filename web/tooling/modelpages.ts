/** The datasets' pages: which datasets get one (`/<id>`, and `/<prefix>/<id>`
 * in every other language), what each says about its producers and
 * licence, and the names a page may not take. `sitepages.ts` writes them:
 * a copy of the built viewer per dataset and language with that dataset's
 * head (`modelpage.ts`).
 *
 * Cloudflare Pages serves `hrrr.html` at `/hrrr` (and answers `/hrrr.html`
 * with a 308 to it), the path the shell reads its dataset from
 * (`modelpaths.ts`).
 *
 * A page name may not shadow anything else the site serves at the root — a
 * page, a directory, a public file, a language's prefix — so the build
 * fails on a dataset id that would. The names are read off the build's own
 * output and inputs.
 *
 * Node-only: imported by vite.config.ts, never by the page. */

import { readFileSync } from "node:fs";
import { extname } from "node:path";

import { FORECAST_MODELS, mosaicMembers, pageModelIds, type ForecastModelId } from "../src/manifest";
import type { DatasetProvenance } from "../src/modelpage";
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
  return pageModelIds((id) => served.size === 0 || served.has(id));
}

/** The part of `xuebuild/stac_prose.json` a page reads: per source, its
 * licence (an SPDX id or `other`), its links and its providers. */
export interface StacProse {
  sources: Record<
    string,
    {
      license?: string;
      links?: readonly { rel: string; href: string }[];
      providers?: readonly { name: string; roles: readonly string[]; url?: string }[];
    }
  >;
}

/** Licence URLs for the SPDX ids a source may carry without a licence
 * link of its own. */
const SPDX_LICENSE_URLS: Readonly<Record<string, string>> = {
  "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
};

/** A dataset's producers and licence from its STAC prose, or undefined for
 * one the prose has no entry for (a view the shell composes). The licence
 * is the prose's `license` link, else the URL of its SPDX id; a mosaic,
 * whose members each carry their own licence, names its members'
 * producers and no licence. */
export function datasetProvenance(prose: StacProse, id: ForecastModelId): DatasetProvenance | undefined {
  const producersOf = (sourceId: string) =>
    (prose.sources[sourceId]?.providers ?? [])
      .filter((provider) => provider.roles.includes("producer"))
      .map(({ name, url }) => (url === undefined ? { name } : { name, url }));
  if (FORECAST_MODELS[id].mosaic) {
    const producers = new Map<string, { name: string; url?: string }>();
    for (const member of mosaicMembers(id)) for (const producer of producersOf(member.id)) producers.set(producer.name, producer);
    return producers.size === 0 ? undefined : { producers: [...producers.values()] };
  }
  const entry = prose.sources[id];
  if (entry === undefined) return undefined;
  const link = entry.links?.find((candidate) => candidate.rel === "license")?.href;
  const license = link ?? (entry.license === undefined ? undefined : SPDX_LICENSE_URLS[entry.license]);
  return { ...(license === undefined ? {} : { license }), producers: producersOf(id) };
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
 * public directory's entries, the HTML pages of the source tree, and every
 * language's prefix (`sitepaths.ts`), which a build without the storm
 * archive may not otherwise emit. */
export function reservedStems(outputFiles: Iterable<string>, publicEntries: Iterable<string>, sourcePages: Iterable<string>): Set<string> {
  const stems = new Set<string>();
  for (const path of [...outputFiles, ...publicEntries, ...sourcePages]) stems.add(rootStem(path));
  for (const prefix of Object.values(PAGE_PREFIXES)) stems.add(rootStem(prefix));
  stems.add(rootStem(archivePagePath("en")));
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

/** `xuebuild/stac_prose.json`, the part a page reads. */
export function readStacProse(path: string): StacProse {
  return JSON.parse(readFileSync(path, "utf8")) as StacProse;
}

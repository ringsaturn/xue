/** The discovery files a crawler or an agent reads before the page: the
 * sitemap and `llms-full.txt`, emitted by the production build.
 *
 * Both are derived, not written: the sitemap's case pages come from the
 * checked-in case definitions (`showcase/cases/*.json`), and `llms-full.txt`
 * is the project's own documentation — README, the format and encoder
 * specs, the case authoring guide — concatenated with its relative links
 * rewritten to the repository, so a model reading one file follows none
 * into the void. `llms.txt` (the short index) and `robots.txt` are prose and
 * live as static files in `web/public/`.
 *
 * Node-only: imported by vite.config.ts, never by the page. */

import { readdirSync, readFileSync } from "node:fs";
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

/** Case ids from a directory of definitions, in filename order so the
 * output is stable across builds. The `id` field is authoritative; the
 * filename is the convention (`<id>.json`) and only a fallback. */
export function readCaseIds(casesDir: string): string[] {
  return readdirSync(casesDir)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => {
      const parsed = JSON.parse(readFileSync(join(casesDir, name), "utf8")) as { id?: unknown };
      return typeof parsed.id === "string" && parsed.id !== "" ? parsed.id : name.slice(0, -".json".length);
    });
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
    "> normative format specification, the native encoder notes and the case",
    `> authoring guide. The short index is ${SITE_ORIGIN}/llms.txt.`,
    "",
  ].join("\n");
  const sections = docs.map(
    ({ repoPath, markdown }) =>
      `\n\n---\n\n<!-- source: ${repoPath} -->\n\n${absolutizeLinks(markdown, repoPath).trimEnd()}\n`,
  );
  return header + sections.join("");
}

/** The documents `llms-full.txt` carries, in reading order. */
export const LLMS_FULL_SOURCES: readonly string[] = ["README.md", "docs/format.md", "docs/encoder.md", "showcase/README.md"];

export interface DiscoveryOptions {
  /** Repository root: where `showcase/cases/` and the documents live. */
  repoRoot: string;
}

/** Vite plugin: emit `sitemap.xml` and `llms-full.txt` next to the pages.
 * Build only — the dev server serves the static `web/public/` files and
 * nothing here matters before deploy. */
export function discoveryFiles({ repoRoot }: DiscoveryOptions): Plugin {
  return {
    name: "xue:discovery-files",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "sitemap.xml",
        source: renderSitemap(sitemapUrls(readCaseIds(join(repoRoot, "showcase", "cases")))),
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

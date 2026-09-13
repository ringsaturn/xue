import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  absolutizeLinks,
  catalogCaseIds,
  LLMS_FULL_SOURCES,
  loadCatalogCaseIds,
  renderLlmsFull,
  renderSitemap,
  sitemapUrls,
} from "../../web/tooling/discovery";
import { REPO_URL, SITE_ORIGIN } from "../../web/src/site";

const scratch: string[] = [];

afterEach(() => {
  vi.unstubAllGlobals();
  for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true });
});

/** A `web/public/`-shaped directory, with `data/showcase.json` when given. */
function publicDir(catalog?: string): string {
  const dir = mkdtempSync(join(tmpdir(), "xue-public-"));
  scratch.push(dir);
  if (catalog !== undefined) {
    mkdirSync(join(dir, "data"));
    writeFileSync(join(dir, "data", "showcase.json"), catalog);
  }
  return dir;
}

const CATALOG = JSON.stringify({
  schemaVersion: 1,
  generatedAt: "2026-09-10T10:18:43Z",
  cases: [{ id: "shadel-2026", title: {} }, { id: "pnw-heat-dome-2021", title: {} }],
});

describe("sitemap", () => {
  it("lists the two pages and one URL per case", () => {
    expect(sitemapUrls(["zhengzhou-2021", "shadel-2026"])).toEqual([
      `${SITE_ORIGIN}/`,
      `${SITE_ORIGIN}/showcase.html`,
      `${SITE_ORIGIN}/?case=zhengzhou-2021`,
      `${SITE_ORIGIN}/?case=shadel-2026`,
    ]);
  });

  it("escapes the query in XML", () => {
    const xml = renderSitemap([`${SITE_ORIGIN}/?case=a&b`]);
    expect(xml).toContain("<loc>https://xue.ringsaturn.me/?case=a&amp;b</loc>");
    expect(xml).toMatch(/^<\?xml version="1.0" encoding="UTF-8"\?>\n<urlset xmlns="http:\/\/www.sitemaps.org\/schemas\/sitemap\/0.9">/);
    expect(xml.trimEnd().endsWith("</urlset>")).toBe(true);
  });

  it("takes case ids from the published catalog, in catalog order", () => {
    expect(catalogCaseIds(CATALOG)).toEqual(["shadel-2026", "pnw-heat-dome-2021"]);
  });

  it("rejects a catalog the page could not list", () => {
    expect(() => catalogCaseIds(JSON.stringify({ schemaVersion: 2, cases: [] }))).toThrow(/schema version/);
    expect(() => catalogCaseIds(JSON.stringify({ schemaVersion: 1 }))).toThrow(/no case list/);
    expect(() => catalogCaseIds(JSON.stringify({ schemaVersion: 1, cases: [{ id: "Not A Slug" }] }))).toThrow(/slug/);
    expect(() => catalogCaseIds(JSON.stringify({ schemaVersion: 1, cases: [{ id: "a" }, { id: "a" }] }))).toThrow(
      /duplicate/,
    );
  });

  it("fetches the catalog from the bucket on a deploy build", async () => {
    const requested: string[] = [];
    vi.stubGlobal("fetch", async (url: URL) => {
      requested.push(url.href);
      return new Response(CATALOG, { status: 200 });
    });
    await expect(loadCatalogCaseIds("https://dataset.example/xue/", publicDir())).resolves.toEqual([
      "shadel-2026",
      "pnw-heat-dome-2021",
    ]);
    expect(requested).toEqual(["https://dataset.example/xue/showcase.json"]);
  });

  it("fails a deploy build whose bucket does not answer", async () => {
    vi.stubGlobal("fetch", async () => new Response("", { status: 503 }));
    await expect(loadCatalogCaseIds("https://dataset.example/xue/", publicDir())).rejects.toThrow(/503/);
  });

  it("reads the local catalog otherwise, and lists no case without one", async () => {
    await expect(loadCatalogCaseIds(undefined, publicDir(CATALOG))).resolves.toEqual([
      "shadel-2026",
      "pnw-heat-dome-2021",
    ]);
    await expect(loadCatalogCaseIds(undefined, publicDir())).resolves.toEqual([]);
  });
});

describe("llms-full", () => {
  it("rewrites relative links against the document's directory", () => {
    const text = absolutizeLinks(
      "see [spec](docs/format.md#v2), [up](../README.md), [abs](https://x.test/), [mail](mailto:a@b.c), [here](#top), ![img](./img.png)",
      "docs/encoder.md",
    );
    expect(text).toBe(
      `see [spec](${REPO_URL}/blob/main/docs/docs/format.md#v2), [up](${REPO_URL}/blob/main/README.md), [abs](https://x.test/), [mail](mailto:a@b.c), [here](#top), ![img](${REPO_URL}/blob/main/docs/img.png)`,
    );
  });

  it("leaves a root document's links at the repository root", () => {
    expect(absolutizeLinks("[x](docs/format.md)", "README.md")).toBe(`[x](${REPO_URL}/blob/main/docs/format.md)`);
  });

  it("concatenates every source under one header, in order", () => {
    const text = renderLlmsFull([
      { repoPath: "README.md", markdown: "# A\n\n[spec](docs/format.md)\n" },
      { repoPath: "docs/format.md", markdown: "# B\n" },
    ]);
    expect(text.startsWith("# Xue — full documentation\n")).toBe(true);
    expect(text).toContain(`${SITE_ORIGIN}/llms.txt`);
    expect(text.indexOf("<!-- source: README.md -->")).toBeLessThan(text.indexOf("<!-- source: docs/format.md -->"));
    expect(text).toContain(`[spec](${REPO_URL}/blob/main/docs/format.md)`);
  });

  it("names documents that exist in the repository", () => {
    expect(LLMS_FULL_SOURCES).toEqual([
      "README.md",
      "docs/format.md",
      "docs/encoder.md",
      "docs/tc.md",
      "showcase/README.md",
    ]);
  });
});

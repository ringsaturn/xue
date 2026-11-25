import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  absolutizeLinks,
  LLMS_FULL_SOURCES,
  loadCatalogJson,
  renderLlmsFull,
  renderSitemap,
  sitemapUrls,
} from "../../web/tooling/discovery";
import { REPO_URL, SITE_ORIGIN } from "../../web/src/site";

const REPO_ROOT = resolve(__dirname, "../..");

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
  it("lists every language's copy of the site's pages, then the generated pages", () => {
    const site = [`${SITE_ORIGIN}/`, `${SITE_ORIGIN}/ja/`, `${SITE_ORIGIN}/showcase/shadel-2026`, `${SITE_ORIGIN}/ja/showcase/shadel-2026`];
    expect(sitemapUrls(site, [`${SITE_ORIGIN}/tc/`])).toEqual([...site, `${SITE_ORIGIN}/tc/`]);
  });

  it("leaves the API console out in every language", () => {
    expect(sitemapUrls([`${SITE_ORIGIN}/api`, `${SITE_ORIGIN}/de/api`, `${SITE_ORIGIN}/de/compare`])).toEqual([`${SITE_ORIGIN}/de/compare`]);
  });

  it("names no ?lang= or ?case= address", () => {
    for (const url of sitemapUrls([`${SITE_ORIGIN}/zh-hant/hrrr`, `${SITE_ORIGIN}/showcase`])) expect(url).not.toMatch(/[?&](lang|case)=/);
  });

  it("escapes the query in XML", () => {
    const xml = renderSitemap([`${SITE_ORIGIN}/?a=1&b=2`]);
    expect(xml).toContain("<loc>https://xue.ringsaturn.me/?a=1&amp;b=2</loc>");
    expect(xml).toMatch(/^<\?xml version="1.0" encoding="UTF-8"\?>\n<urlset xmlns="http:\/\/www.sitemaps.org\/schemas\/sitemap\/0.9">/);
    expect(xml.trimEnd().endsWith("</urlset>")).toBe(true);
  });

  it("dates an address only when it has a day", () => {
    const xml = renderSitemap([`${SITE_ORIGIN}/`, `${SITE_ORIGIN}/tc/`], { [`${SITE_ORIGIN}/tc/`]: "2026-11-18" });
    expect(xml).toContain("<loc>https://xue.ringsaturn.me/tc/</loc>\n    <lastmod>2026-11-18</lastmod>");
    expect(xml.match(/<lastmod>/g)).toHaveLength(1);
  });

  it("fetches the catalog from the bucket on a deploy build", async () => {
    const requested: string[] = [];
    vi.stubGlobal("fetch", async (url: URL) => {
      requested.push(url.href);
      return new Response(CATALOG, { status: 200 });
    });
    await expect(loadCatalogJson("https://dataset.example/xue/", publicDir())).resolves.toBe(CATALOG);
    expect(requested).toEqual(["https://dataset.example/xue/showcase.json"]);
  });

  it("fails a deploy build whose bucket does not answer", async () => {
    vi.stubGlobal("fetch", async () => new Response("", { status: 503 }));
    await expect(loadCatalogJson("https://dataset.example/xue/", publicDir())).rejects.toThrow(/503/);
  });

  it("reads the local catalog otherwise, and none without one", async () => {
    await expect(loadCatalogJson(undefined, publicDir(CATALOG))).resolves.toBe(CATALOG);
    await expect(loadCatalogJson(undefined, publicDir())).resolves.toBeNull();
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
    ]);
    for (const path of LLMS_FULL_SOURCES) expect(existsSync(join(REPO_ROOT, path)), path).toBe(true);
  });
});

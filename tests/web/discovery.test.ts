import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  absolutizeLinks,
  LLMS_FULL_SOURCES,
  readCaseIds,
  renderLlmsFull,
  renderSitemap,
  sitemapUrls,
} from "../../web/tooling/discovery";
import { REPO_URL, SITE_ORIGIN } from "../../web/src/site";

const scratch: string[] = [];

afterEach(() => {
  for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function casesDir(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "xue-cases-"));
  scratch.push(dir);
  for (const [name, content] of Object.entries(files)) writeFileSync(join(dir, name), content);
  return dir;
}

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

  it("reads case ids from the definitions, in filename order, id field first", () => {
    const dir = casesDir({
      "b-case.json": JSON.stringify({ id: "b-case", title: {} }),
      "a-case.json": JSON.stringify({ title: {} }),
      "notes.md": "not a case",
    });
    expect(readCaseIds(dir)).toEqual(["a-case", "b-case"]);
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
    expect(LLMS_FULL_SOURCES).toEqual(["README.md", "docs/format.md", "docs/encoder.md", "showcase/README.md"]);
  });
});

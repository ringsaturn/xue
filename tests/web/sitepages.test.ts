/** Every page in every language (`/ja/hrrr`, `/showcase/<id>`): the path
 * mapping the shell and the build share, the language chooser, the
 * move of old `?lang=` / `?case=` links, and the copies
 * the build writes. */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { LOCALES, normalizeLocale } from "../../web/src/i18n";
import { FORECAST_MODEL_IDS } from "../../web/src/modelpaths";
import type { ShowcaseCase } from "../../web/src/showcase-catalog";
import { SITE_ORIGIN } from "../../web/src/site";
import {
  caseFromPath,
  isShellPath,
  LANGUAGE_CHOOSER,
  LOCALE_CODES,
  legacyLocaleLocation,
  localizedPath,
  relocalizePath,
  splitLocalePath,
  type Locale,
} from "../../web/src/sitepaths";
import { pagePrefix, STORM_PAGE_LOCALES } from "../../web/src/tc/pages";
import {
  absolutizeUrls,
  buildSitePages,
  pageFileName,
  renderCasePage,
  renderShowcaseList,
  sitePathTarget,
  translateMarkup,
  writeHead,
} from "../../web/tooling/sitepages";

const REPO_ROOT = resolve(__dirname, "../..");
const INDEX_HTML = readFileSync(resolve(REPO_ROOT, "web/index.html"), "utf8");
const SHOWCASE_HTML = readFileSync(resolve(REPO_ROOT, "web/showcase.html"), "utf8");
const page = (name: string) => readFileSync(resolve(REPO_ROOT, "web", name), "utf8");

describe("the language in the path", () => {
  it("has a prefix for every locale but English", () => {
    expect(LOCALE_CODES).toEqual(LOCALES.map((item) => item.code));
    expect(localizedPath("en", "/hrrr")).toBe("/hrrr");
    expect(localizedPath("ja", "/")).toBe("/ja/");
    expect(localizedPath("zh-Hant", "/showcase/x")).toBe("/zh-hant/showcase/x");
    expect(localizedPath("de", "/?type=wind#map=3/1/2")).toBe("/de/?type=wind#map=3/1/2");
  });

  it("splits a path into its language and the page, round-tripping the writer", () => {
    expect(splitLocalePath("/")).toEqual({ locale: "en", path: "/", prefixed: false });
    expect(splitLocalePath("/hrrr")).toEqual({ locale: "en", path: "/hrrr", prefixed: false });
    expect(splitLocalePath("/ja")).toEqual({ locale: "ja", path: "/", prefixed: true });
    expect(splitLocalePath("/ja/")).toEqual({ locale: "ja", path: "/", prefixed: true });
    expect(splitLocalePath("/zh-hant/showcase/x")).toEqual({ locale: "zh-Hant", path: "/showcase/x", prefixed: true });
    // Not a prefix: a storm page, a dataset, a wrong case.
    expect(splitLocalePath("/tc/2025/").locale).toBe("en");
    expect(splitLocalePath("/japan").locale).toBe("en");
    expect(splitLocalePath("/zh-Hant/").locale).toBe("en");
    for (const code of LOCALE_CODES) {
      for (const path of ["/", "/hrrr", "/showcase", "/showcase/ida-2021"]) {
        expect(splitLocalePath(localizedPath(code, path)), `${code} ${path}`).toMatchObject({ locale: code, path });
      }
    }
    expect(relocalizePath("/ja/hrrr", "de")).toBe("/de/hrrr");
    expect(relocalizePath("/ja/", "en")).toBe("/");
  });

  it("never takes a dataset's name", () => {
    for (const code of LOCALE_CODES) {
      const slug = localizedPath(code, "/").split("/")[1]!;
      expect(FORECAST_MODEL_IDS as readonly string[]).not.toContain(slug);
    }
  });

  it("knows the shell's pages from the storm and airport pages", () => {
    const ids = new Set(["hrrr"]);
    for (const path of ["/", "/showcase", "/showcase/ida-2021", "/compare", "/metrics", "/typhoon", "/api", "/hrrr"]) {
      expect(isShellPath(path, ids), path).toBe(true);
    }
    for (const path of ["/tc/", "/airports/rjtt", "/llms.txt", "/gfs", "/showcase/a/b"]) expect(isShellPath(path, ids), path).toBe(false);
    expect(caseFromPath("/showcase/ida-2021")).toBe("ida-2021");
    expect(caseFromPath("/showcase/IDA")).toBeNull();
  });

  it("keeps the storm and airport pages at their five languages", () => {
    expect(STORM_PAGE_LOCALES).toEqual(["en", "zh", "zh-Hant", "ja", "ko"]);
    expect(pagePrefix("ja")).toBe("/ja");
    expect(pagePrefix("de")).toBe("");
  });
});

describe("the move of an old link", () => {
  it("moves ?lang= to the language's prefix, keeping the rest of the query", () => {
    expect(legacyLocaleLocation("/", "?lang=ja")).toBe("/ja/");
    expect(legacyLocaleLocation("/showcase", "?lang=zh-TW")).toBe("/zh-hant/showcase");
    expect(legacyLocaleLocation("/compare", "?lat=1&lang=en&lon=2")).toBe("/compare?lat=1&lon=2");
    expect(legacyLocaleLocation("/ja/hrrr", "?lang=de&type=wind")).toBe("/de/hrrr?type=wind");
    // A tag naming no locale is dropped; the page keeps its language.
    expect(legacyLocaleLocation("/ko/compare", "?lang=klingon")).toBe("/ko/compare");
  });

  it("moves /?case= to the case's page", () => {
    expect(legacyLocaleLocation("/", "?case=ida-2021&type=radar")).toBe("/showcase/ida-2021?type=radar");
    expect(legacyLocaleLocation("/index.html", "?case=ida-2021&lang=zh")).toBe("/zh/showcase/ida-2021");
    // Not a slug: the parameter goes, the home stays.
    expect(legacyLocaleLocation("/", "?case=..%2Fx")).toBe("/");
    // Only the home read it.
    expect(legacyLocaleLocation("/hrrr", "?case=ida-2021")).toBeNull();
    expect(legacyLocaleLocation("/", "?type=wind")).toBeNull();
  });
});

describe("the language chooser", () => {
  afterEach(() => vi.unstubAllGlobals());

  /** Run the chooser on `path` for a reader, returning where it sends them. */
  function choose(path: string, languages: string[], stored: string | null = null, userAgent = "Mozilla/5.0"): string | null {
    const url = new URL(path, SITE_ORIGIN);
    let target: string | null = null;
    const location = {
      pathname: url.pathname,
      search: url.search,
      hash: url.hash,
      replace: (next: string) => {
        target = next;
      },
    };
    const run = new Function("location", "navigator", "localStorage", LANGUAGE_CHOOSER);
    run(location, { userAgent, languages, language: languages[0] }, { getItem: () => stored });
    return target;
  }

  it("sends a reader to their language's copy, the path, query and fragment kept", () => {
    expect(choose("/", ["ja-JP", "en"])).toBe("/ja/");
    expect(choose("/hrrr?type=wind#map=3/1/2", ["zh-TW"])).toBe("/zh-hant/hrrr?type=wind#map=3/1/2");
    expect(choose("/showcase/x", ["it", "pt-BR"])).toBe("/pt/showcase/x");
  });

  it("follows the stored choice before the browser", () => {
    expect(choose("/", ["ja"], "de")).toBe("/de/");
    expect(choose("/", ["ja"], "en")).toBeNull();
  });

  it("agrees with the shell's tag rules", () => {
    for (const tag of ["zh", "zh-CN", "zh-HK", "zh-Hant", "zh-Hans-HK", "ne-NP", "TR", "en-GB", "it", "es-419"]) {
      const locale = normalizeLocale(tag);
      const expected = locale === null || locale === "en" ? null : localizedPath(locale, "/");
      expect(choose("/", [tag]), tag).toBe(expected);
    }
  });

  it("leaves a crawler, an English reader, a prefixed page and an old ?lang= link alone", () => {
    expect(choose("/", ["ja"], null, "Mozilla/5.0 (compatible; Googlebot/2.1)")).toBeNull();
    expect(choose("/", ["en-US"])).toBeNull();
    expect(choose("/ja/hrrr", ["de"])).toBeNull();
    expect(choose("/?lang=en", ["ja"])).toBeNull();
  });
});

describe("a page's copy in a language", () => {
  it("names its assets and links root-absolute", () => {
    expect(absolutizeUrls('<script src="./assets/a.js"></script><a href="./">x</a><a href="https://x/">y</a>')).toBe(
      '<script src="/assets/a.js"></script><a href="/">x</a><a href="https://x/">y</a>',
    );
  });

  it("carries the static copy and the links in its language", () => {
    const html = translateMarkup(INDEX_HTML, "ja");
    expect(html).toContain('<html lang="ja"');
    expect(html).toContain('<span class="sr-only" data-i18n="showcaseLink">過去事例</span>');
    expect(html).toMatch(/<a class="icon-button" href="\/ja\/showcase">/);
    expect(html).toMatch(/id="compare-link" href="\/ja\/compare"/);
    expect(html).toMatch(/data-i18n-aria="settingsAria" aria-label="設定"/);
    // A link outside the shell keeps its address.
    expect(html).toContain('<a href="/llms.txt">');
  });

  it("is canonical to itself with the whole hreflang set, and only English chooses", () => {
    const withChooser = INDEX_HTML.replace("<head>", '<head>\n    <script data-locale-chooser>x()</script>');
    const html = writeHead(withChooser, { path: "/hrrr", locale: "ko" });
    expect(html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/ko/hrrr" />`);
    expect(html).toContain(`<meta property="og:url" content="${SITE_ORIGIN}/ko/hrrr" />`);
    expect(html).toContain('<meta property="og:locale" content="ko_KR" />');
    expect(html.match(/<link rel="alternate" hreflang=/g)).toHaveLength(LOCALES.length + 1);
    expect(html).toContain(`<link rel="alternate" hreflang="x-default" href="${SITE_ORIGIN}/hrrr" />`);
    expect(html).not.toContain("data-locale-chooser");
    expect(writeHead(withChooser, { path: "/hrrr", locale: "en" })).toContain("data-locale-chooser");
  });

  it("is written for every page in every language, at the path Pages serves it", () => {
    expect(pageFileName("en", "/")).toBe("index.html");
    expect(pageFileName("ja", "/")).toBe("ja/index.html");
    expect(pageFileName("zh-Hant", "/showcase/x")).toBe("zh-hant/showcase/x.html");
    const built = Object.fromEntries(
      ["index.html", "showcase.html", "compare.html", "metrics.html", "typhoon.html", "api.html"].map((name) => [name, page(name)]),
    );
    const site = buildSitePages({ built, models: [{ id: "hrrr" }], cases: [GUADALUPE], catalogDay: "2026-10-28" });
    // Home, one dataset, the list, one case, four other pages.
    expect(site.files).toHaveLength(LOCALES.length * 8);
    expect(new Set(site.files.map((file) => file.fileName)).size).toBe(site.files.length);
    expect(site.urls).toContain(`${SITE_ORIGIN}/ne/showcase/guadalupe-2025`);
    expect(site.urls.some((url) => /[?&](lang|case)=/.test(url))).toBe(false);
    expect(site.lastmod[`${SITE_ORIGIN}/showcase/guadalupe-2025`]).toBe("2026-10-28");
    const compare = site.files.find((file) => file.fileName === "de/compare.html")!.source;
    expect(compare).toContain("<title>Vorhersagen vergleichen · Xue</title>");
    const source = (name: string) => site.files.find((file) => file.fileName === name)!.source;
    expect(source("ja/index.html")).toContain("<title>Xue · リアルタイムの気象モデル・レーダー・衛星</title>");
    expect(source("ja/hrrr.html")).toContain("<title>HRRR リアルタイム予報図 · Xue</title>");
    expect(source("ko/api.html")).toContain("<title>API 콘솔 · Xue</title>");
  });
});

const text = (en: string, zh: string) => ({ en, zh, ja: `${en} (ja)` });

const GUADALUPE: ShowcaseCase = {
  id: "guadalupe-2025",
  title: text("Guadalupe River flash flood, July 2025", "2025 年 7 月瓜达卢佩河山洪"),
  summary: text("Thunderstorms stalled over the Texas Hill Country.", "雷暴在得克萨斯山地停滞。"),
  modelId: "hrrr",
  model: "HRRR",
  product: "wrfsfc",
  run: "2025070400",
  runTime: "2025-07-04T00:00:00Z",
  eventTime: "2025-07-04T10:05:00Z",
  timeZone: "America/Chicago",
  forecastHours: 18,
  bbox: [-100.55, 29.0, -97.45, 31.0],
  dataBbox: [-100.55, 29.0, -97.45, 31.0],
  grid: { width: 120, height: 80 },
  variables: ["prate", "cref"],
  defaultVariable: "prate",
  manifestPath: "showcase/guadalupe-2025/manifest.json",
  manifestCrc32: "00000000",
  byteLength: 1_234_567,
  tags: ["flood"],
  markers: [
    {
      id: "hunt",
      kind: "place",
      lon: -99.3217,
      lat: 30.0699,
      time: Date.parse("2025-07-04T09:00:00Z"),
      label: text("Hunt", "亨特"),
      note: text("USGS 08165500, Guadalupe River at Hunt.", "USGS 08165500 瓜达卢佩河亨特站。"),
      sources: [{ title: "USGS 08165500", url: "https://waterdata.usgs.gov/monitoring-location/08165500/" }],
    },
  ],
};

const part = (id: string): ShowcaseCase => ({ ...GUADALUPE, id, title: text(`Haze ${id}`, `烟霾 ${id}`), markers: [] });
const LEAD: ShowcaseCase = {
  ...part("haze-lead"),
  parts: [
    { case: "haze-lead", label: text("Season smoke", "整季烟羽") },
    { case: "haze-day", label: text("One day", "一天") },
  ],
};
const CASES: ShowcaseCase[] = [LEAD, part("haze-day"), GUADALUPE];

describe("a case's page", () => {
  it("is the viewer under the case's head, in its language", () => {
    const html = renderCasePage(INDEX_HTML, GUADALUPE, CASES, "zh");
    expect(html).toContain("<title>2025 年 7 月瓜达卢佩河山洪 · Xue</title>");
    expect(html).toContain('<meta name="description" content="雷暴在得克萨斯山地停滞。" />');
    expect(html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/zh/showcase/guadalupe-2025" />`);
    expect(html).toContain(`<link rel="alternate" hreflang="x-default" href="${SITE_ORIGIN}/showcase/guadalupe-2025" />`);
    expect(html).toContain('<html lang="zh-CN" data-case-opening=""');
    const graphs = [...html.matchAll(/<script type="application\/ld\+json">(\{"@context[\s\S]*?)<\/script>/g)].map(
      (match) => JSON.parse(match[1]!) as Record<string, unknown>,
    );
    expect(graphs.map((graph) => graph["@type"])).toEqual(["BreadcrumbList", "Dataset"]);
    expect(graphs[1]).toMatchObject({ url: `${SITE_ORIGIN}/zh/showcase/guadalupe-2025`, temporalCoverage: "2025-07-04T00:00:00Z/2025-07-04T18:00:00Z" });
  });

  it("names the case's producers and licence, and its own dataset over the map", () => {
    const provenance = { licenses: ["https://example.org/licence"], producers: [{ name: "NOAA / NCEP" }] };
    const html = renderCasePage(INDEX_HTML, GUADALUPE, CASES, "zh", provenance);
    expect(html).toContain('"creator":[{"@type":"Organization","name":"NOAA / NCEP"}]');
    expect(html).toContain('"license":"https://example.org/licence"');
    expect(html).toContain('<span id="model-eyebrow">NOAA / HRRR CONUS (3 KM)</span>');
    // The site's own Dataset is the home's alone.
    expect(html).not.toContain("Xue live forecast bundles");
  });

  it("has the case panel filled in, which the shell fills again", () => {
    const html = renderCasePage(INDEX_HTML, GUADALUPE, CASES, "ja");
    expect(html).toContain('<div class="case-banner" id="case-banner">');
    expect(html).toContain('<h2 id="case-title">Guadalupe River flash flood, July 2025 (ja)</h2>');
    expect(html).toContain('<p id="case-summary">Thunderstorms stalled over the Texas Hill Country. (ja)</p>');
    expect(html).toMatch(/<ul class="case-markers" id="case-markers" data-i18n-aria[^>]*><li><button type="button" data-kind="place">/);
    expect(html).toMatch(/<nav class="case-parts" id="case-parts" hidden[^>]*><\/nav>/);
    // The no-script note says what the map would.
    expect(html).toContain("USGS 08165500, Guadalupe River at Hunt. (ja)");
    expect(html).toContain("2025-07-04 04:00 UTC−05:00 (America/Chicago)");
  });

  it("links the other parts of its event, the open one marked", () => {
    const html = renderCasePage(INDEX_HTML, part("haze-day"), CASES, "zh-Hant");
    expect(html).toContain(
      '<a href="/zh-hant/showcase/haze-lead">整季烟羽</a><a href="/zh-hant/showcase/haze-day" aria-current="page">一天</a>',
    );
  });
});

describe("the case list", () => {
  it("carries the cards of the listed cases and their ItemList", () => {
    const html = renderShowcaseList(absolutizeUrls(SHOWCASE_HTML), CASES.filter((row) => row.id !== "haze-day"), "ja");
    expect(html.match(/<li class="showcase-card">/g)).toHaveLength(2);
    expect(html).toContain('<a href="/ja/showcase/guadalupe-2025"');
    expect(html).toMatch(/<p id="showcase-status"[^>]* hidden>/);
    expect(html).toContain('<script type="application/ld+json" id="showcase-cases">');
    expect(html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/ja/showcase" />`);
  });
});

describe("the dev and preview servers", () => {
  const ids = new Set(["hrrr"]);

  it("serve every language's copy as Pages does", () => {
    expect(sitePathTarget("/", ids, true)).toBeNull();
    expect(sitePathTarget("/ja/", ids, true)).toBe("/ja/index.html");
    expect(sitePathTarget("/ja", ids, true)).toBe("/ja/index.html");
    expect(sitePathTarget("/hrrr", ids, true)).toBe("/hrrr.html");
    expect(sitePathTarget("/de/hrrr", ids, true)).toBe("/de/hrrr.html");
    expect(sitePathTarget("/showcase/x", ids, true)).toBe("/showcase/x.html");
    expect(sitePathTarget("/fr/compare", ids, true)).toBe("/fr/compare.html");
    expect(sitePathTarget("/tc/2025/", ids, true)).toBeNull();
    expect(sitePathTarget("/gfs", ids, true)).toBeNull();
  });

  it("serve the source pages on the dev server", () => {
    expect(sitePathTarget("/ja/hrrr", ids, false)).toBe("/index.html");
    expect(sitePathTarget("/zh/showcase/x", ids, false)).toBe("/index.html");
    expect(sitePathTarget("/zh/showcase", ids, false)).toBe("/showcase.html");
  });
});

const LOCALE_SAMPLE: Locale[] = ["en", "ne"];
describe("every locale", () => {
  it.each(LOCALE_SAMPLE)("translates the viewer's markup without a stray placeholder (%s)", (locale) => {
    expect(translateMarkup(INDEX_HTML, locale)).not.toMatch(/data-i18n="[A-Za-z0-9]+">[^<]*\{year\}/);
  });
});

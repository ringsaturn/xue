import { describe, expect, it } from "vitest";

import { SITE_ORIGIN } from "../../web/src/site";
import {
  buildCaseSite,
  casePagePath,
  caseViewerLink,
  listedRows,
  parseCaseCatalog,
  renderCasePage,
  shortDescription,
  variableName,
  zonedTime,
} from "../../web/tooling/casepages";
import { PAGE_LANGS, pageFile } from "../../web/tooling/stormpages";

const text = (en: string, zh: string) => ({ en, zh, ja: `${en} (ja)` });

const GUADALUPE = {
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
  variables: ["prate", "cref"],
  defaultVariable: "prate",
  tags: ["flood", "north-america"],
  credit: "NOAA HRRR; USGS NWIS",
  markers: [
    {
      id: "hunt",
      kind: "place",
      lon: -99.3217,
      lat: 30.0699,
      label: text("Hunt", "亨特"),
      note: text("USGS 08165500, Guadalupe River at Hunt.", "USGS 08165500 瓜达卢佩河亨特站。"),
      sources: [
        { title: "USGS 08165500", url: "https://waterdata.usgs.gov/monitoring-location/08165500/" },
        { title: "bad", url: "javascript:alert(1)" },
      ],
    },
  ],
};

const part = (id: string) => ({ ...GUADALUPE, id, title: text(`Haze ${id}`, `烟霾 ${id}`), markers: [] });

const CATALOG = JSON.stringify({
  schemaVersion: 1,
  generatedAt: "2026-10-28T01:09:28Z",
  cases: [
    {
      ...part("haze-lead"),
      parts: [
        { case: "haze-lead", label: text("Season smoke", "整季烟羽") },
        { case: "haze-day", label: text("One day", "一天") },
      ],
    },
    part("haze-day"),
    GUADALUPE,
    { ...GUADALUPE, id: "no-english", title: { zh: "只有中文" } },
  ],
});

describe("the catalog", () => {
  it("keeps the rows a page can be written for and lists a set under its lead", () => {
    const catalog = parseCaseCatalog(CATALOG);
    expect(catalog.day).toBe("2026-10-28");
    expect(catalog.cases.map((row) => row.id)).toEqual(["haze-lead", "haze-day", "guadalupe-2025"]);
    expect(listedRows(catalog.cases).map((row) => row.id)).toEqual(["haze-lead", "guadalupe-2025"]);
    // A source that is not an https link is dropped, as the viewer drops it.
    expect(catalog.cases[2]!.markers[0]!.sources).toHaveLength(1);
  });

  it("rejects a catalog of another schema", () => {
    expect(() => parseCaseCatalog(JSON.stringify({ schemaVersion: 2, cases: [] }))).toThrow(/schema version/);
  });
});

describe("case page addresses", () => {
  it("puts English at the root and the others under the storm pages' prefixes", () => {
    expect(casePagePath("en", "ida-2021")).toBe("/cases/ida-2021");
    expect(casePagePath("zh-Hant", "ida-2021")).toBe("/zh-hant/cases/ida-2021");
    expect(casePagePath("ko")).toBe("/ko/cases/");
    expect(pageFile(casePagePath("ja", "ida-2021"))).toBe("ja/cases/ida-2021.html");
    expect(pageFile(casePagePath("ja"))).toBe("ja/cases/index.html");
    expect(caseViewerLink("en", "ida-2021")).toBe("/?case=ida-2021");
    expect(caseViewerLink("zh-Hant", "ida-2021")).toBe("/?case=ida-2021&lang=zh-Hant");
  });
});

describe("a case page", () => {
  it("reads the event on the case's own clock", () => {
    expect(zonedTime("2025-07-04T10:05:00Z", "America/Chicago")).toBe("2025-07-04 05:05 UTC−05:00 (America/Chicago)");
    expect(zonedTime("2025-07-04T10:05:00Z", undefined)).toBe("2025-07-04 10:05 UTC");
  });

  it("names a variable as the viewer does, with its id", () => {
    expect(variableName("tmp2m", "en")).toBe("2 m temperature (tmp2m)");
    expect(variableName("not-a-variable", "en")).toBe("not-a-variable");
  });

  it("cuts a long summary for the description", () => {
    const long = "word ".repeat(80).trim();
    const cut = shortDescription(long);
    expect(cut.length).toBeLessThanOrEqual(201);
    expect(cut.endsWith("word…")).toBe(true);
  });

  it("states the catalog's facts, names every translation and links the viewer", () => {
    const catalog = parseCaseCatalog(CATALOG);
    const row = catalog.cases.find((item) => item.id === "guadalupe-2025")!;
    const { path, html } = renderCasePage(row, catalog.cases, "zh");
    expect(path).toBe("/zh/cases/guadalupe-2025");
    expect(html).toContain('<html lang="zh-CN">');
    expect(html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/zh/cases/guadalupe-2025">`);
    for (const lang of PAGE_LANGS)
      expect(html).toContain(`hreflang="${lang.htmlLang}" href="${SITE_ORIGIN}${lang.prefix}/cases/guadalupe-2025"`);
    expect(html).toContain(`hreflang="x-default" href="${SITE_ORIGIN}/cases/guadalupe-2025"`);
    expect(html).toContain("<h1>2025 年 7 月瓜达卢佩河山洪</h1>");
    expect(html).toContain('href="/?case=guadalupe-2025&lang=zh"');
    expect(html).toContain("2025-07-04 05:05 UTC−05:00 (America/Chicago)");
    expect(html).toContain("HRRR · wrfsfc");
    expect(html).toContain("<h3>亨特</h3>");
    expect(html).toContain('"@type":"Dataset"');
    expect(html).toContain('"box":"29 -100.55 31 -97.45"');
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain('localStorage.getItem("xue-locale")');
  });

  it("falls back to English where the case has no translation, and lists its parts", () => {
    const catalog = parseCaseCatalog(CATALOG);
    const { html } = renderCasePage(catalog.cases[0]!, catalog.cases, "ko");
    expect(html).toContain("<h1>Haze haze-lead</h1>");
    expect(html).toContain("<h3>One day</h3>");
    expect(html).toContain('href="/?case=haze-day&lang=ko"');
  });

  it("sends a reader to their language only from the English page", () => {
    const catalog = parseCaseCatalog(CATALOG);
    const { html } = renderCasePage(catalog.cases[2]!, catalog.cases, "en");
    expect(html).toContain('localStorage.getItem("xue-locale")');
  });
});

describe("the site", () => {
  it("writes an index and a page per listed case in every language, dated by the catalog", () => {
    const site = buildCaseSite(parseCaseCatalog(CATALOG));
    expect(site.urls).toHaveLength(PAGE_LANGS.length * 3);
    expect(site.files.filter((file) => file.fileName === "cases/pages.css")).toHaveLength(1);
    expect(site.files.map((file) => file.fileName)).toContain("zh-hant/cases/index.html");
    expect(site.urls).toContain(`${SITE_ORIGIN}/ja/cases/guadalupe-2025`);
    expect(site.urls).not.toContain(`${SITE_ORIGIN}/cases/haze-day`);
    expect(site.lastmod[`${SITE_ORIGIN}/cases/`]).toBe("2026-10-28");
  });

  it("writes nothing for an empty catalog", () => {
    const site = buildCaseSite(parseCaseCatalog(JSON.stringify({ schemaVersion: 1, cases: [] })));
    expect(site.files).toEqual([]);
    expect(site.urls).toEqual([]);
  });
});

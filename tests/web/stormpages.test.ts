import { describe, expect, it } from "vitest";

import { SITE_ORIGIN } from "../../web/src/site";
import {
  buildStormSite,
  PAGE_LANGS,
  pageFile,
  pagePath,
  renderStormPage,
  stormSlug,
  summaryFacts,
  type StormData,
} from "../../web/tooling/stormpages";

const KT = 0.514444;

function point(time: string, lat: number, lon: number, kt: number, pmin: number) {
  return { time, lat, lon, vmax: kt * KT, pmin, radii: null, class: null, rmw: null, gust: null, cone: null };
}

const RAGASA: StormData = {
  season: 2025,
  entry: {
    id: "WP242025",
    basin: "WP",
    name: "RAGASA",
    sid: null,
    atcf: ["WP242025"],
    offset: 0,
    length: 1,
    best: ["usa"],
    first: "2025-09-18T12:00:00Z",
    last: "2025-09-20T12:00:00Z",
    vmax: 144 * KT,
    months: ["2025-09"],
    sources: ["gfs", "ecmwf"],
  },
  best: {
    storm: "WP242025",
    basin: "WP",
    name: "RAGASA",
    sid: null,
    atcf: ["WP242025"],
    best: {
      usa: {
        source: "ibtracs",
        provisional: false,
        points: [
          point("2025-09-18T12:00:00Z", 15.9, 133.0, 29, 1005),
          point("2025-09-18T15:00:00Z", 16.0, 132.6, 31, 1004),
          point("2025-09-19T12:00:00Z", 16.3, 130.1, 99, 957),
          point("2025-09-20T12:00:00Z", 19.3, 122.9, 144, 910),
        ],
      },
    },
  },
  forecasts: 124,
  errors: {
    usa: {
      ecmwf: { base: ["a", "b", "c"], lead: [48, 48, 48], track: [60, 64, 68], along: [], cross: [], vmax: [], pmin: [] },
      gfs: { base: ["a", "b", "c"], lead: [48, 48, 24], track: [90, 96, 40], along: [], cross: [], vmax: [], pmin: [] },
    },
  },
};

describe("storm page addresses", () => {
  it("puts English at the root and the others under a prefix, as the blog does", () => {
    expect(stormSlug({ id: "WP262026", name: "CHOI-WAN" })).toBe("choi-wan-wp262026");
    expect(stormSlug({ id: "WP232026", name: null })).toBe("wp232026");
    expect(pagePath("en", "storm", 2026, "choi-wan-wp262026")).toBe("/tc/2026/choi-wan-wp262026");
    expect(pagePath("zh-Hant", "storm", 2026, "choi-wan-wp262026")).toBe("/zh-hant/tc/2026/choi-wan-wp262026");
    expect(pagePath("ja", "season", 2026)).toBe("/ja/tc/2026/");
    expect(pagePath("ko", "archive")).toBe("/ko/tc/");
    expect(pageFile("/zh/tc/2026/choi-wan-wp262026")).toBe("zh/tc/2026/choi-wan-wp262026.html");
    expect(pageFile("/zh/tc/2026/")).toBe("zh/tc/2026/index.html");
  });
});

describe("a storm page", () => {
  it("states the storm's facts from its best track", () => {
    const facts = summaryFacts(RAGASA, "zh");
    expect(facts).toMatchObject({ kind: "台风", name: "桦加沙", basin: "西北太平洋", days: 2, peakKt: 144, peakClass: "H5", pmin: 910, forecasts: 124 });
    expect(facts.sources).toEqual(["ECMWF", "GFS"]);
    // ECMWF at 48 h averages 64 km over three cases; GFS has only two.
    expect(facts.best48).toEqual({ source: "ECMWF", km: 64 });
  });

  it("names every translation and is canonical to itself", () => {
    const { path, html } = renderStormPage(RAGASA, "zh");
    expect(path).toBe("/zh/tc/2025/ragasa-wp242025");
    expect(html).toContain('<html lang="zh-CN">');
    expect(html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/zh/tc/2025/ragasa-wp242025">`);
    for (const lang of PAGE_LANGS)
      expect(html).toContain(`hreflang="${lang.htmlLang}" href="${SITE_ORIGIN}${lang.prefix}/tc/2025/ragasa-wp242025"`);
    expect(html).toContain(`hreflang="x-default" href="${SITE_ORIGIN}/tc/2025/ragasa-wp242025"`);
    expect(html).toContain('<meta property="og:locale" content="zh_CN">');
    expect(html).toContain("<h1>台风桦加沙 ");
    expect(html).toContain('href="/typhoon?storm=WP242025&lang=zh"');
    // Synoptic points only: the 15Z row is IBTrACS' interpolation.
    expect(html.match(/<tr><td>2025-/g)).toHaveLength(3);
    expect(html).not.toContain('localStorage.getItem("xue-locale")');
  });

  it("sends a reader to their language only from the English page", () => {
    const { html } = renderStormPage(RAGASA, "en");
    expect(html).toContain("<h1>Typhoon Ragasa ");
    expect(html).toContain('localStorage.getItem("xue-locale")');
  });
});

describe("the site", () => {
  it("writes every page in every language, the stylesheet once", () => {
    const site = buildStormSite([RAGASA]);
    // Per language: the archive, one season and one storm.
    expect(site.urls).toHaveLength(PAGE_LANGS.length * 3);
    expect(site.files.filter((file) => file.fileName === "tc/pages.css")).toHaveLength(1);
    expect(site.files.map((file) => file.fileName)).toContain("ko/tc/2025/index.html");
    expect(site.urls).toContain(`${SITE_ORIGIN}/ja/tc/2025/ragasa-wp242025`);
  });
});

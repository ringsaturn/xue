import { describe, expect, it } from "vitest";

import { SITE_ORIGIN } from "../../web/src/site";
import {
  buildStormSite,
  PAGE_LANGS,
  pageFile,
  pagePath,
  renderSeasonPage,
  renderStormPage,
  stormSlug,
  stormImages,
  summaryFacts,
  trackBox,
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
    expect(html).toContain('href="/zh/typhoon?storm=WP242025"');
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

function jsonLd(html: string): Record<string, unknown>[] {
  return [...html.matchAll(/<script type="application\/ld\+json">(.*?)<\/script>/g)].map((match) => JSON.parse(match[1]!));
}

describe("structured data", () => {
  it("describes a storm as a Dataset the page is about", () => {
    const { html } = renderStormPage(RAGASA, "en");
    const own = `${SITE_ORIGIN}/tc/2025/ragasa-wp242025`;
    const nodes = jsonLd(html);
    const page = nodes.find((node) => node["@type"] === "WebPage")!;
    expect(page.mainEntity).toEqual({ "@id": `${own}#data` });
    expect(page.dateModified).toBe("2025-09-20");
    const data = nodes.find((node) => node["@type"] === "Dataset")!;
    expect(data).toMatchObject({
      "@id": `${own}#data`,
      url: own,
      identifier: "WP242025",
      temporalCoverage: "2025-09-18/2025-09-20",
      spatialCoverage: { geo: { box: "15.9 122.9 19.3 133" } },
      isAccessibleForFree: true,
      dateModified: "2025-09-20",
    });
    expect((data.description as string).length).toBeGreaterThanOrEqual(50);
  });

  it("leaves out a box across the antimeridian", () => {
    const at = (lon: number) => ({ time: "", lat: 10, lon, vmax: null, pmin: null, radii: null, class: null, rmw: null, gust: null, cone: null });
    expect(trackBox([at(175), at(-178)])).toBeNull();
    expect(trackBox([at(-170), at(-160)])).toBe("10 -170 10 -160");
  });

  it("lists a season's storms as the collection it is", () => {
    const { html } = renderSeasonPage(2025, [RAGASA], "ja");
    const page = jsonLd(html).find((node) => node["@type"] === "CollectionPage")!;
    expect(page.mainEntity).toMatchObject({
      "@type": "ItemList",
      numberOfItems: 1,
      itemListElement: [{ position: 1, url: `${SITE_ORIGIN}/ja/tc/2025/ragasa-wp242025` }],
    });
  });

  it("keeps a script end tag in a name inside the JSON", () => {
    const named = { ...RAGASA, entry: { ...RAGASA.entry, name: "X</script><b>" } };
    const { html } = renderStormPage(named, "en");
    for (const node of jsonLd(html)) expect(node).toBeTruthy();
    expect(html).not.toContain("X</script><b>");
  });
});

describe("the track pictures", () => {
  const root = "https://data.example/xue/tc-images/";
  const index = {
    storms: {
      WP242025: {
        season: 2025,
        light: { path: "2025/WP242025/light-9a3234af.webp" },
        dark: { path: "2025/WP242025/dark-c91c812f.webp" },
        card: { path: "2025/WP242025/card-df197ab8.png" },
      },
      AL012025: { season: 2025, light: { path: "../../elsewhere.webp" }, dark: { path: "x" }, card: { path: "y" } },
    },
  };

  it("reads only well-formed entries, as absolute addresses", () => {
    const images = stormImages(index, root);
    expect([...images.keys()]).toEqual(["WP242025"]);
    expect(images.get("WP242025")!.card).toBe(`${root}2025/WP242025/card-df197ab8.png`);
    expect(() => stormImages({}, root)).toThrow();
  });

  it("puts the picture on the page, in the card and in the structured data", () => {
    const images = stormImages(index, root).get("WP242025")!;
    const { html } = renderStormPage({ ...RAGASA, images }, "en");
    expect(html).toContain(`<source srcset="${images.dark}" media="(prefers-color-scheme: dark)">`);
    expect(html).toContain(`<img src="${images.light}" width="1200" height="630"`);
    expect(html).not.toContain('class="tc-track"');
    expect(html).toContain(`<meta property="og:image" content="${images.card}">`);
    const nodes = jsonLd(html);
    expect(nodes.find((node) => node["@type"] === "WebPage")!.primaryImageOfPage).toMatchObject({ contentUrl: images.card });
    expect(nodes.find((node) => node["@type"] === "Dataset")!.image).toMatchObject({ contentUrl: images.card, width: 1200 });
  });

  it("keeps the inline track and the site's card without a picture", () => {
    const { html } = renderStormPage(RAGASA, "en");
    expect(html).toContain('class="tc-track"');
    expect(html).toContain(`<meta property="og:image" content="${SITE_ORIGIN}/og-image.jpg">`);
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

  it("dates every page by the last best-track day it shows", () => {
    const site = buildStormSite([RAGASA]);
    expect(Object.keys(site.lastmod)).toHaveLength(site.urls.length);
    expect(site.lastmod[`${SITE_ORIGIN}/ja/tc/2025/ragasa-wp242025`]).toBe("2025-09-20");
    expect(site.lastmod[`${SITE_ORIGIN}/tc/`]).toBe("2025-09-20");
  });
});

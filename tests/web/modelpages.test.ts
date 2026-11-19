/** The datasets' pages (`/hrrr`): the shared path mapping, the Pages
 * Function that redirects old `?model=` links, the head each page is built
 * with and the names the build refuses. */

import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { onRequest } from "../../functions/_middleware";
import { FORECAST_MODELS, isCaseOnlyModel, liveModelIds, type ForecastModelId } from "../../web/src/manifest";
import { modelPageHead } from "../../web/src/modelpage";
import {
  FORECAST_MODEL_IDS,
  legacyModelLocation,
  MODEL_ALIASES,
  modelForAlias,
  modelFromPathname,
} from "../../web/src/modelpaths";
import { SITE_ORIGIN } from "../../web/src/site";
import {
  checkModelPageNames,
  modelPageIds,
  modelPagePaths,
  renderModelPage,
  reservedStems,
  rootStem,
} from "../../web/tooling/modelpages";

const REPO_ROOT = resolve(__dirname, "../..");
const INDEX_HTML = readFileSync(resolve(REPO_ROOT, "web/index.html"), "utf8");

describe("the dataset paths", () => {
  it("are every dataset the switch offers, none of them reached through cases alone", () => {
    // The Pages Function reads this list without the manifest: it must
    // already be the live set.
    for (const id of FORECAST_MODEL_IDS) expect(isCaseOnlyModel(id), id).toBe(false);
    expect(liveModelIds()).toEqual([...FORECAST_MODEL_IDS]);
    expect(modelPageIds(undefined)).toEqual([...FORECAST_MODEL_IDS]);
    expect(modelPageIds("")).toEqual([...FORECAST_MODEL_IDS]);
  });

  it("follow a build's VITE_SOURCES", () => {
    expect(modelPageIds("hrrr, gfs,woof")).toEqual(["gfs", "hrrr"]);
    expect(modelPagePaths(["gfs", "hrrr"])).toEqual(["/gfs", "/hrrr"]);
  });

  it("map every spelling to a dataset the table has", () => {
    for (const [alias, id] of Object.entries(MODEL_ALIASES)) {
      expect(alias).toBe(alias.toLowerCase());
      expect(id in FORECAST_MODELS, alias).toBe(true);
      expect(modelForAlias(alias.toUpperCase())).toBe(id);
    }
    expect(modelForAlias("constructor")).toBeNull();
    expect(modelForAlias("icon")).toBeNull();
  });

  it("read one segment, a canonical id or a spelling of one in the set", () => {
    const ids = new Set(["hrrr", "goeseast"]);
    expect(modelFromPathname("/hrrr", ids)).toBe("hrrr");
    expect(modelFromPathname("/hrrr.html", ids)).toBe("hrrr");
    expect(modelFromPathname("/GOES-East", ids)).toBe("goeseast");
    expect(modelFromPathname("/gfs", ids)).toBeNull();
    expect(modelFromPathname("/", ids)).toBeNull();
    expect(modelFromPathname("/tc/2026/", ids)).toBeNull();
  });
});

describe("the redirect of an old ?model= link", () => {
  const ids = new Set(FORECAST_MODEL_IDS);

  it("moves a live dataset to its path and keeps the rest of the query", () => {
    expect(legacyModelLocation("/", "?model=ECMWF&type=wind850&lang=zh", ids)).toBe("/ecmwf?type=wind850&lang=zh");
    expect(legacyModelLocation("/index.html", "?model=himawari-9", ids)).toBe("/himawari");
    expect(legacyModelLocation("/", "?model=worldview&gibs=aqua", ids)).toBe("/gibs?gibs=aqua");
  });

  it("sends a case-only or unknown dataset home without the parameter", () => {
    expect(legacyModelLocation("/", "?model=era5&type=temp", ids)).toBe("/?type=temp");
    expect(legacyModelLocation("/", "?model=nope", ids)).toBe("/");
    // A spelling outside a build's served set goes home too.
    expect(legacyModelLocation("/", "?model=hrrr", new Set(["gfs"]))).toBe("/");
  });

  it("leaves a case, a path and a link without the parameter alone", () => {
    expect(legacyModelLocation("/", "?case=ida-2021&model=gfs", ids)).toBeNull();
    expect(legacyModelLocation("/hrrr", "?model=gfs", ids)).toBeNull();
    expect(legacyModelLocation("/showcase", "?model=gfs", ids)).toBeNull();
    expect(legacyModelLocation("/", "?type=wind", ids)).toBeNull();
  });
});

describe("the Pages Function", () => {
  async function request(path: string): Promise<{ status: number; location: string | null; passed: boolean }> {
    let passed = false;
    const response = await onRequest({
      request: new Request(`${SITE_ORIGIN}${path}`),
      next: async () => {
        passed = true;
        return new Response("static", { status: 200 });
      },
    });
    return { status: response.status, location: response.headers.get("location"), passed };
  }

  it("301s an old link to the dataset's path", async () => {
    expect(await request("/?model=hrrr&type=wind")).toEqual({
      status: 301,
      location: `${SITE_ORIGIN}/hrrr?type=wind`,
      passed: false,
    });
    expect(await request("/index.html?model=CMA-RADAR")).toEqual({ status: 301, location: `${SITE_ORIGIN}/cma`, passed: false });
    expect(await request("/?model=woof&lang=ja")).toEqual({ status: 301, location: `${SITE_ORIGIN}/?lang=ja`, passed: false });
  });

  it("passes everything else through to the static site", async () => {
    for (const path of ["/", "/?type=wind", "/?case=ida-2021&model=gfs", "/index.html"]) {
      expect((await request(path)).passed, path).toBe(true);
    }
  });

  it("is routed for the root alone", () => {
    const routes = JSON.parse(readFileSync(resolve(REPO_ROOT, "web/public/_routes.json"), "utf8"));
    expect(routes).toEqual({ version: 1, include: ["/", "/index.html"], exclude: [] });
  });
});

describe("a dataset's page", () => {
  it("names the dataset in the head and keeps the viewer's body", () => {
    const html = renderModelPage(INDEX_HTML, "hrrr");
    const head = modelPageHead("hrrr");
    expect(head.title).toBe("HRRR live forecast maps");
    expect(html).toContain("<title>HRRR live forecast maps · Xue</title>");
    expect(html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/hrrr" />`);
    expect(html).toContain(`<link rel="alternate" hreflang="x-default" href="${SITE_ORIGIN}/hrrr" />`);
    expect(html).toContain(`<link rel="alternate" hreflang="zh-TW" href="${SITE_ORIGIN}/hrrr?lang=zh-Hant" />`);
    expect(html).toContain(`<meta property="og:url" content="${SITE_ORIGIN}/hrrr" />`);
    expect(html).toContain(`<meta name="description" content="${head.description}" />`);
    expect(html).toContain(`<meta property="og:description" content="${head.description}" />`);
    // The static-message pass must not put the site's description back.
    expect(html).not.toMatch(/<meta name="description"[^>]*data-i18n-content/);
    expect(html).not.toContain(`href="${SITE_ORIGIN}/?lang=`);
    // Everything after the head is the viewer's, byte for byte.
    expect(html.slice(html.indexOf("</head>"))).toBe(INDEX_HTML.slice(INDEX_HTML.indexOf("</head>")));
  });

  it("adds a Dataset to the page's graph, from the table's facts", () => {
    const html = renderModelPage(INDEX_HTML, "himawari");
    const json = /<script type="application\/ld\+json">([\s\S]*?)<\/script>/.exec(html)![1]!;
    const graph = (JSON.parse(json) as { "@graph": Record<string, unknown>[] })["@graph"];
    const dataset = graph.at(-1)!;
    expect(dataset).toMatchObject({
      "@type": "Dataset",
      "@id": `${SITE_ORIGIN}/himawari#dataset`,
      url: `${SITE_ORIGIN}/himawari`,
      isAccessibleForFree: true,
      isPartOf: { "@id": `${SITE_ORIGIN}/#data` },
      // The region past the antimeridian, spelled in −180…180.
      spatialCoverage: { "@type": "Place", geo: { "@type": "GeoShape", box: "-60 80.7 60 -159.3" } },
      distribution: [{ "@type": "DataDownload", contentUrl: "https://dataset.ringsaturn.me/xue/latest-himawari.json" }],
    });
    expect(String(dataset.description)).toContain("a frame every 10 minutes");
    // The site's own nodes stay.
    expect(graph.some((node) => node["@type"] === "WebApplication")).toBe(true);
  });

  it("is written for every dataset, each with a head of its own", () => {
    const titles = new Set<string>();
    const descriptions = new Set<string>();
    for (const id of FORECAST_MODEL_IDS as readonly ForecastModelId[]) {
      const head = modelPageHead(id);
      titles.add(head.title);
      descriptions.add(head.description);
      expect(head.path).toBe(`/${id}`);
      expect(() => renderModelPage(INDEX_HTML, id)).not.toThrow();
    }
    expect(titles.size).toBe(FORECAST_MODEL_IDS.length);
    expect(descriptions.size).toBe(FORECAST_MODEL_IDS.length);
    // A global dataset with no region, a mosaic, daily imagery.
    expect(modelPageHead("gfs").dataset.spatialCoverage).toEqual({
      "@type": "Place",
      geo: { "@type": "GeoShape", box: "-90 -180 90 180" },
    });
    expect(modelPageHead("geo").description).toContain("HIMAWARI");
    expect(modelPageHead("gibs").dataset).not.toHaveProperty("distribution");
  });

  it("fails on a head the build cannot find its way around", () => {
    expect(() => renderModelPage(INDEX_HTML.replace(/<title>[^<]*<\/title>/, ""), "gfs")).toThrow(/title/);
  });
});

describe("the names a dataset's page may not take", () => {
  it("are the root names of the output, the public files and the pages", () => {
    expect(rootStem("showcase.html")).toBe("showcase");
    expect(rootStem("assets/main-abc.js")).toBe("assets");
    expect(rootStem("zh/tc/2026/x.html")).toBe("zh");
    expect(rootStem("sitemap.xml")).toBe("sitemap");
    const reserved = reservedStems(["index.html", "assets/a.js", "sitemap.xml", "llms-full.txt"], ["data", "llms.txt"], ["skewt.html"]);
    for (const stem of ["index", "assets", "sitemap", "llms-full", "data", "llms", "skewt", "tc", "zh", "zh-hant", "ja", "ko"]) {
      expect(reserved.has(stem), stem).toBe(true);
    }
  });

  it("fail the build on a collision, and pass the real tree", () => {
    expect(() => checkModelPageNames(["gfs", "showcase"], new Set(["showcase"]))).toThrow(/\/showcase/);
    const publicEntries = readdirSync(resolve(REPO_ROOT, "web/public"));
    const pages = readdirSync(resolve(REPO_ROOT, "web")).filter((name) => name.endsWith(".html"));
    const outputs = ["index.html", "showcase.html", "compare.html", "metrics.html", "typhoon.html", "api.html", "assets/x.js", "sitemap.xml", "llms-full.txt"];
    expect(() => checkModelPageNames(FORECAST_MODEL_IDS, reservedStems(outputs, publicEntries, pages))).not.toThrow();
  });
});

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { renderLive } from "../../web/src/airports/live";
import { airportModels, localMidnights, renderModelChart } from "../../web/src/airports/modelchart";
import { airportIndexPath, airportPagePath, fill } from "../../web/src/airports/pages";
import { dayExtremes, localDay } from "../../web/src/airports/today";
import { SITE_ORIGIN } from "../../web/src/site";
import type { AirportStationHistory, Metar } from "../../web/src/stations/schema";
import {
  airportCards,
  airportName,
  AIRPORT_PAGES_REVISED,
  buildAirportSite,
  liveScript,
  loadAirports,
  nearestAirports,
  renderAirportIndex,
  renderAirportPage,
  validateAirports,
  localCity,
  WORDS,
} from "../../web/tooling/airportpages";
import { pageFile } from "../../web/tooling/stormpages";

const LIST_PATH = resolve(import.meta.dirname, "../../web/tooling/airports.json");
const LIST = loadAirports(LIST_PATH);
const RAW = JSON.parse(readFileSync(LIST_PATH, "utf8")) as { airports: Record<string, unknown>[] };
const byIcao = (icao: string) => LIST.airports.find((airport) => airport.icao === icao)!;

describe("the airport list", () => {
  it("is valid as checked in", () => {
    expect(LIST.airports.length).toBeGreaterThan(200);
    expect(new Set(LIST.airports.map((airport) => airport.icao)).size).toBe(LIST.airports.length);
    expect(LIST.updated).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(byIcao("RJTT").tz).toBe("Asia/Tokyo");
  });

  it("refuses an id listed twice, an unknown zone and a missing field", () => {
    const base = { schemaVersion: 1, updated: "2026-11-19" };
    const first = RAW.airports[0]!;
    expect(() => validateAirports({ ...base, airports: [first, first] })).toThrow(/twice/);
    expect(() => validateAirports({ ...base, airports: [{ ...first, tz: "Mars/Olympus" }] })).toThrow(/tz/);
    expect(() => validateAirports({ ...base, airports: [{ ...first, name: undefined }] })).toThrow(/name/);
    expect(() => validateAirports({ ...base, airports: [{ ...first, lat: 91 }] })).toThrow(/lat/);
    expect(() => validateAirports({ ...base, updated: "today", airports: [first] })).toThrow(/updated/);
  });

  it("names an airport in the page language, else in English", () => {
    const missing = LIST.airports.find((airport) => airport.names.ja === undefined);
    if (missing) expect(airportName(missing, "ja")).toBe(missing.name);
    expect(airportName(byIcao("RJTT"), "en")).toBe(byIcao("RJTT").name);
    expect(airportName(byIcao("RJTT"), "ja")).toBe(byIcao("RJTT").names.ja);
  });

  it("names the city served in the page language, else in English", () => {
    expect(localCity(byIcao("RJAA"), "en")).toBe("Tokyo");
    expect(localCity(byIcao("RJAA"), "ja")).toBe("東京");
    expect(localCity(byIcao("EGLC"), "zh")).toBe("伦敦");
    const missing = LIST.airports.find((airport) => airport.cityNames.ko === undefined);
    if (missing) expect(localCity(missing, "ko")).toBe(missing.city);
  });
});

describe("airport page addresses", () => {
  it("puts English at the root and the others under a prefix, lowercase", () => {
    expect(airportPagePath("en", "RJTT")).toBe("/airports/rjtt");
    expect(airportPagePath("zh-Hant", "RJTT")).toBe("/zh-hant/airports/rjtt");
    expect(airportIndexPath("ko")).toBe("/ko/airports/");
    expect(pageFile("/ja/airports/rjtt")).toBe("ja/airports/rjtt.html");
    expect(pageFile("/airports/")).toBe("airports/index.html");
  });
});

describe("an airport page", () => {
  const page = renderAirportPage(byIcao("RJTT"), LIST.airports, "ja", "assets/airport-abc.js");

  it("names every translation and itself", () => {
    expect(page.path).toBe("/ja/airports/rjtt");
    expect(page.html).toContain(`<link rel="canonical" href="${SITE_ORIGIN}/ja/airports/rjtt">`);
    expect(page.html.match(/hreflang="[^"]+" href=/g)).toHaveLength(6);
    expect(page.html).toContain(`hreflang="x-default" href="${SITE_ORIGIN}/airports/rjtt"`);
    expect(page.html).toContain('<html lang="ja">');
  });

  it("states the airport and describes it as schema.org data", () => {
    const blocks = [...page.html.matchAll(/<script type="application\/ld\+json">(.*?)<\/script>/g)].map((match) => JSON.parse(match[1]!) as Record<string, unknown>);
    const airport = blocks.find((block) => block["@type"] === "Airport")!;
    expect(airport).toMatchObject({ icaoCode: "RJTT", iataCode: "HND", geo: { "@type": "GeoCoordinates", latitude: 35.553 } });
    expect(blocks.find((block) => block["@type"] === "BreadcrumbList")).toBeTruthy();
    expect(page.html).toContain("RJTT / HND");
    expect(page.html).toContain("Asia/Tokyo");
    expect(page.html).toContain("日本");
  });

  it("links the map at the airport with its marks, the comparison and the neighbours", () => {
    expect(page.html).toContain("/ja/?stations=apt#map=10/35.553/139.781");
    expect(page.html).toContain("/ja/compare?lat=35.55&#38;lon=139.78");
    expect(nearestAirports(byIcao("RJTT"), LIST.airports)[0]!.airport.icao).toBe("RJAA");
    expect(page.html).toContain('href="/ja/airports/rjaa"');
  });

  it("loads the live block and hands it its words", () => {
    expect(page.html).toContain('<script type="module" src="/assets/airport-abc.js"></script>');
    const config = JSON.parse(/<script type="application\/json" id="live-config">(.*?)<\/script>/.exec(page.html)![1]!) as { icao: string; tz: string };
    expect(config).toMatchObject({ icao: "RJTT", tz: "Asia/Tokyo", lat: 35.553, lon: 139.781 });
  });

  it("names the models its forecast section reads", () => {
    expect(page.html).toContain('<section id="models"><h2>数値予報モデル</h2>');
    expect(page.html).toContain("GFS、ECMWF、AIFS");
    expect(airportModels(139.781, 35.553)).toEqual(["gfs", "ecmwf", "aifs"]);
    expect(airportModels(-73.779, 40.64)).toEqual(["gfs", "ecmwf", "aifs", "hrrr"]);
  });

  it("is the language chooser only in English", () => {
    expect(renderAirportPage(byIcao("RJTT"), LIST.airports, "en", null).html).toContain('localStorage.getItem("xue-locale")');
    expect(page.html).not.toContain('localStorage.getItem("xue-locale")');
  });
});

describe("an airport's share card", () => {
  const root = "https://data.example/xue/airport-images/";
  const card = `${root}RJTT/card-0123abcd.png`;

  it("reads the card index, leaving out malformed entries", () => {
    const cards = airportCards(
      {
        product: "airport-images",
        airports: {
          RJTT: { source: "1:00000000", card: { path: "RJTT/card-0123abcd.png", byteLength: 1 } },
          RJAA: { card: { path: "../x.png" } },
          RJBB: { card: { path: "RJAA/card-0123abcd.png" } },
        },
      },
      root,
    );
    expect([...cards]).toEqual([["RJTT", card]]);
    expect(() => airportCards({ product: "tc-images", storms: {} }, root)).toThrow();
  });

  it("is the page's og:image and the image of its WebPage", () => {
    const html = renderAirportPage(byIcao("RJTT"), LIST.airports, "ja", null, card).html;
    expect(html).toContain(`<meta property="og:image" content="${card}">`);
    expect(html).toContain('<meta property="og:image:width" content="1200">');
    const blocks = [...html.matchAll(/<script type="application\/ld\+json">(.*?)<\/script>/g)].map((match) => JSON.parse(match[1]!) as Record<string, unknown>);
    expect(blocks.find((block) => block["@type"] === "WebPage")).toMatchObject({ image: card, primaryImageOfPage: { "@type": "ImageObject", url: card, width: 1200, height: 630 } });
  });

  it("falls back to the site's card", () => {
    const html = renderAirportPage(byIcao("RJTT"), LIST.airports, "ja", null).html;
    expect(html).toContain(`<meta property="og:image" content="${SITE_ORIGIN}/og-image.jpg">`);
    expect(html).not.toContain("og:image:width");
    expect(html).not.toContain("primaryImageOfPage");
    const site = buildAirportSite(LIST, null, new Map([["RJTT", card]]));
    const page = site.files.find((file) => file.fileName === pageFile("/airports/rjtt"))!;
    expect(page.source).toContain(`content="${card}"`);
  });
});

describe("the airport index", () => {
  it("groups the airports under their country's name in the page language", () => {
    const page = renderAirportIndex(LIST.airports, "ja");
    expect(page.path).toBe("/ja/airports/");
    expect(page.html).toContain("<h2>日本</h2>");
    expect(page.html).toContain('href="/ja/airports/rjtt"');
  });
});

describe("the airport site", () => {
  it("writes every page in every language, dated by the list or the pages' revision", () => {
    const site = buildAirportSite(LIST, "assets/airport-abc.js");
    expect(site.files.length).toBe(1 + 5 * (LIST.airports.length + 1));
    expect(site.urls).toContain(`${SITE_ORIGIN}/airports/`);
    expect(site.urls).toContain(`${SITE_ORIGIN}/zh/airports/zbaa`);
    expect(new Set(Object.values(site.lastmod))).toEqual(new Set([LIST.updated > AIRPORT_PAGES_REVISED ? LIST.updated : AIRPORT_PAGES_REVISED]));
  });

  it("finds the live block's file in the bundle", () => {
    expect(liveScript({ "assets/airport-x.js": { type: "chunk", isEntry: true, name: "airport", fileName: "assets/airport-x.js" } })).toBe("assets/airport-x.js");
    expect(liveScript({ "a.js": { type: "chunk", isEntry: true, name: "main", fileName: "a.js" } })).toBeNull();
  });
});

describe("the model forecast chart", () => {
  const now = Date.parse("2026-11-28T03:00:00Z");
  const hour = 3600_000;
  const trace = (label: string, offset: number) => ({ label, points: [0, 6, 12, 18].map((h) => ({ t: now + h * hour, v: 10 + offset + h / 6 })) });

  it("marks the airport's midnights", () => {
    expect(localMidnights(now, now + 24 * hour, "Asia/Tokyo")).toEqual([Date.parse("2026-11-28T15:00:00Z")]);
    expect(localMidnights(now, now + 24 * hour, "Asia/Kolkata")).toEqual([Date.parse("2026-11-28T18:30:00Z")]);
  });

  it("draws one dashed trace per model, the reports as dots and the legend", () => {
    const html = renderModelChart({
      nowMs: now,
      tz: "Asia/Tokyo",
      lang: "en",
      rows: [{ title: "Temperature", unit: "°C", traces: [trace("GFS", 0), trace("ECMWF", 1)], observed: [{ t: now - hour, v: 9 }], baseline: null }],
      legend: ["GFS 28/00Z", "ECMWF 28/00Z"],
      observedLabel: "Observed (METAR)",
      nowLabel: "now",
    });
    expect(html.match(/class="trace" d=/g)).toHaveLength(2);
    expect(html).toContain('stroke-dasharray="6 3"');
    expect(html.match(/<circle class="obs"/g)).toHaveLength(2);
    expect(html).toContain("GFS 28/00Z");
    expect(html).toContain("Observed (METAR)");
  });

  it("is empty when no row has a point in the window", () => {
    const far = { label: "GFS", points: [{ t: now + 400 * hour, v: 1 }] };
    expect(renderModelChart({ nowMs: now, tz: "UTC", lang: "en", rows: [{ title: "T", unit: "°C", traces: [far], observed: [], baseline: null }], legend: ["GFS"], observedLabel: "", nowLabel: "" })).toBe("");
  });
});

function metar(time: string, t: number | null): Metar {
  return { time, raw: `METAR XXXX ${time}`, t, td: null, wd: 90, ws: 3, gust: null, vis: 10000, qnh: 1015, slp: null, wx: null, cloud: [], category: "VFR", auto: false, type: "METAR" };
}

describe("the day's extremes", () => {
  it("counts only the reports on the airport's own calendar day", () => {
    // 15:00Z is midnight in Tokyo: the 14:30Z report is yesterday there.
    const metars = [metar("2026-09-16T21:00:00Z", 24.1), metar("2026-09-16T18:00:00Z", 19.8), metar("2026-09-16T15:30:00Z", 21), metar("2026-09-16T14:30:00Z", 30)];
    expect(localDay(Date.parse("2026-09-16T15:30:00Z"), "Asia/Tokyo")).toBe("2026-09-17");
    expect(dayExtremes(metars, "Asia/Tokyo", Date.parse("2026-09-16T22:00:00Z"))).toEqual({
      day: "2026-09-17",
      high: { t: 24.1, time: "2026-09-16T21:00:00Z" },
      low: { t: 19.8, time: "2026-09-16T18:00:00Z" },
      reports: 3,
    });
    expect(dayExtremes([metar("2026-09-16T14:30:00Z", 30)], "Asia/Tokyo", Date.parse("2026-09-16T22:00:00Z"))).toBeNull();
  });
});

describe("the live block", () => {
  it("renders the newest report, the window and the forecast", () => {
    const station: AirportStationHistory = {
      icao: "RJTT",
      name: null,
      lat: 35.553,
      lon: 139.781,
      elev: 5,
      iata: "HND",
      wmo: null,
      metars: [metar("2026-09-16T21:00:00Z", 24.1), metar("2026-09-16T20:30:00Z", 23.5)],
      taf: null,
    };
    const html = renderLive({ icao: "RJTT", tz: "Asia/Tokyo", lat: 35.553, lon: 139.781, lang: "en", words: WORDS.en.live }, station, Date.parse("2026-09-16T21:10:00Z"));
    expect(html).toContain("METAR XXXX 2026-09-16T21:00:00Z");
    expect(html).toContain("24.1 °C");
    expect(html).toContain(fill(WORDS.en.live.minutesAgo, { n: 10 }));
    expect(html).toContain(WORDS.en.live.noForecast);
    expect(html).toContain("<td>06:00</td>");
  });
});

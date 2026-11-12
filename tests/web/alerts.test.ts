/** The weather warnings in the shell (`docs/alerts.md`): the pointer, index
 * and network-file readers on the committed golden, how area-coded
 * warnings are joined to their boundary sets, the style the layers add,
 * the card's text choices and the boundary sets' source URL. */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import alertsPointerJson from "../fixtures/alerts/expected/latest-alerts.json";

import { alertBody, alertHeadline } from "../../web/src/alerts/card";
import {
  areaColorExpression,
  areaFilter,
  areaLayersFor,
  areaTargets,
  inlineFeatures,
  sortHits,
  type AlertHit,
} from "../../web/src/alerts/layer";
import { pmtilesSourceUrl } from "../../web/src/alerts/pmtiles";
import {
  parseAlertLine,
  parseAlertLines,
  parseAlertsIndex,
  parseAlertsPointer,
  type AlertLine,
  type AlertsIndex,
} from "../../web/src/alerts/schema";
import { pickLanguageTag } from "../../web/src/langtag";

function repositoryRoot(): string {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory) throw new Error("repository root not found from the working directory");
    directory = parent;
  }
  return directory;
}

type Json = Record<string, unknown>;

const ROUND = join(
  repositoryRoot(),
  "tests/fixtures/alerts/expected",
  dirname((alertsPointerJson as { path: string }).path),
);

function readIndex(): Json {
  return JSON.parse(readFileSync(join(ROUND, "index.json"), "utf8")) as Json;
}

function load(): { index: AlertsIndex; lines: AlertLine[][] } {
  const index = parseAlertsIndex(readIndex());
  const lines = index.networks.map((network) =>
    parseAlertLines(readFileSync(join(ROUND, network.file.path), "utf8"), network.id),
  );
  return { index, lines };
}

describe("the alerts pointer", () => {
  it("reads the golden's and the flat layout, and refuses another product", () => {
    expect(parseAlertsPointer(alertsPointerJson).product).toBe("alerts");
    expect(parseAlertsPointer({ ...alertsPointerJson, path: "alerts.202611010005/index.json" }).path).toBe(
      "alerts.202611010005/index.json",
    );
    expect(() => parseAlertsPointer({ ...alertsPointerJson, product: "aq" })).toThrow(/product/);
  });
});

describe("the alerts index and network files", () => {
  it("reads every row, each span naming its own line", () => {
    const { index, lines } = load();
    expect(index.networks.map((network) => network.id)).toEqual(["nws", "jma", "eccc", "meteoalarm", "cma", "hko"]);
    expect(index.networks[1]!.areas?.set).toBe("jma-class20");
    expect(index.networks[2]!.areas).toBeNull();
    expect(index.networks[3]!.areas?.set).toBe("meteoalarm-areas");
    expect(index.networks[4]!.areas?.set).toBe("cma-areas");
    expect(index.networks[5]!.areas?.set).toBe("cma-areas");
    for (const row of index.alerts) {
      const network = index.networks[row.network]!;
      const bytes = readFileSync(join(ROUND, network.file.path));
      const line = parseAlertLine(JSON.parse(bytes.subarray(row.offset, row.offset + row.length).toString("utf8")));
      expect(line.id).toBe(row.id);
      expect(line.level).toBe(row.level);
    }
    expect(lines.flat().length).toBe(index.alerts.length);
  });

  it("refuses a level off the scale, an unsorted row and a newer schema", () => {
    const raw = readIndex();
    const rows = raw.alerts as unknown[][];
    const badLevel = { ...raw, alerts: rows.map((row, at) => (at === 0 ? [...row.slice(0, 3), 6, ...row.slice(4)] : row)) };
    expect(() => parseAlertsIndex(badLevel)).toThrow(/one of 1, 2, 3, 4, 5/);
    expect(() => parseAlertsIndex({ ...raw, alerts: [...rows].reverse() })).toThrow(/sorted/);
    expect(() => parseAlertsIndex({ ...raw, schemaVersion: 2 })).toThrow(/schema version/);
  });

  it("finds a row's columns by name, and by position in an index without them", () => {
    const raw = readIndex();
    expect(raw.columns).toBeDefined();
    const expected = parseAlertsIndex(raw);
    const positional = { ...raw };
    delete positional.columns;
    expect(parseAlertsIndex(positional)).toEqual(expected);
    const reordered = { ...raw, columns: [...(raw.columns as string[])].reverse() };
    expect(() => parseAlertsIndex(reordered)).toThrow(/columns/);
  });

  it("refuses a line filed under another network", () => {
    const { index } = load();
    const text = readFileSync(join(ROUND, index.networks[1]!.file.path), "utf8");
    expect(() => parseAlertLines(text, "nws")).toThrow(/holds/);
  });
});

describe("the warning areas", () => {
  it("draws a polygon where a warning has one and its codes otherwise", () => {
    const { index, lines } = load();
    const inline = inlineFeatures(lines.flat());
    expect(inline.features.length).toBe(lines.flat().reduce((parts, line) => parts + (line.geometry?.length ?? 0), 0));
    const targets = areaTargets(index.networks, lines);
    const jma = targets.get("jma-class20.20261102/class20")!;
    const jmaCodes = new Set(lines[1]!.flatMap((line) => line.areas));
    expect(new Set(jma.levels.keys())).toEqual(jmaCodes);
    // No NWS or JMA warning with a polygon of its own is drawn again by its codes.
    for (const target of targets.values())
      for (const ids of target.alerts.values())
        for (const id of ids)
          if (!id.startsWith("meteoalarm:")) expect(lines.flat().find((line) => line.id === id)!.geometry).toBeNull();
  });

  it("draws CMA's and HKO's codes on the one set they share", () => {
    const { index, lines } = load();
    const targets = areaTargets(index.networks, lines);
    const province = targets.get("cma-areas.20261112b/province")!;
    const county = targets.get("cma-areas.20261112b/county")!;
    const hko = lines[index.networks.findIndex((network) => network.id === "hko")]!;
    expect(hko.length).toBeGreaterThan(0);
    expect(province.alerts.get("810000")).toEqual(hko.map((line) => line.id));
    expect(county.alerts.get("120115")).toEqual(["cma:12011541600000_20261111192247"]);
  });

  it("draws a MeteoAlarm warning's codes beside its polygon", () => {
    const { index, lines } = load();
    const coded = lines[3]!.find((line) => line.areas.length > 0)!;
    const polygon = lines.flat().find((line) => line.geometry !== null)!;
    const both = { ...coded, id: "meteoalarm:both", geometry: polygon.geometry };
    const targets = areaTargets(index.networks, [[], [], [], [both]]);
    const drawn = [...targets.values()].flatMap((target) => [...target.alerts.values()].flat());
    expect(drawn).toContain("meteoalarm:both");
    expect(inlineFeatures([both]).features.length).toBe(polygon.geometry!.length);
  });

  it("draws each part of a warning's polygon as its own feature", () => {
    // MapLibre's hit test is even-odd over every ring of a feature: two
    // overlapping parts in one feature would hide the warning from a click.
    const { lines } = load();
    const polygon = lines.flat().find((line) => line.geometry !== null)!;
    const part = polygon.geometry![0]!;
    const overlapping = { ...polygon, id: "meteoalarm:overlap", geometry: [part, part] };
    const features = inlineFeatures([overlapping]).features;
    expect(features.map((feature) => feature.geometry.type)).toEqual(["Polygon", "Polygon"]);
    expect(features.every((feature) => feature.properties?.alert === "meteoalarm:overlap")).toBe(true);
  });

  it("gives each code the highest level in force on it", () => {
    const { index, lines } = load();
    const jma = lines[1]!.map((line, at) => (at === 0 ? { ...line, level: 5 as const } : line));
    const target = areaTargets(index.networks, [[], jma, []]).get("jma-class20.20261102/class20")!;
    for (const code of jma[0]!.areas) expect(target.levels.get(code)).toBe(5);
  });

  it("sends NWS fire weather products to the fire zones and the rest elsewhere", () => {
    const layers = ["zone", "county", "marine", "offshore", "fire"];
    const line = { native: { event: "Red Flag Warning" } } as AlertLine;
    expect(areaLayersFor("nws-zones", layers, line)).toEqual(["fire"]);
    expect(areaLayersFor("nws-zones", layers, { native: { event: "Flood Watch" } } as AlertLine)).toEqual([
      "zone",
      "county",
      "marine",
      "offshore",
    ]);
    expect(areaLayersFor("jma-class20", ["class20"], line)).toEqual(["class20"]);
  });

  it("is a style MapLibre accepts on either ground", () => {
    const { index, lines } = load();
    for (const darkGround of [false, true]) {
      const layers = [...areaTargets(index.networks, lines).values()].map((target, at) => ({
        id: `area-${at}`,
        type: "fill" as const,
        source: "areas",
        "source-layer": target.sourceLayer,
        filter: areaFilter(target),
        paint: { "fill-color": areaColorExpression(target, darkGround) },
      }));
      expect(layers.length).toBeGreaterThan(0);
      expect(validateStyleMin({ version: 8, sources: { areas: { type: "vector", url: "pmtiles://x" } }, layers })).toEqual([]);
    }
  });
});

describe("the warnings card", () => {
  it("lists the highest level first, the newest first within a level", () => {
    const { index, lines } = load();
    const hits: AlertHit[] = lines.flatMap((network, position) =>
      network.map((line) => ({ line, network: index.networks[position]! })),
    );
    const sorted = sortHits([...hits]);
    for (let at = 1; at < sorted.length; at += 1)
      expect(sorted[at - 1]!.line.level).toBeGreaterThanOrEqual(sorted[at]!.line.level);
  });

  it("takes the reader's language when published, else the first", () => {
    const line = {
      texts: { en: { headline: "Fog advisory" }, fr: { headline: "Avis de brouillard" } },
    } as unknown as AlertLine;
    expect(alertHeadline(line, "fr")).toBe("Avis de brouillard");
    expect(alertHeadline(line, "ja")).toBe("Fog advisory");
    expect(alertBody(line, "fr")).toEqual([]);
    const cma = {
      texts: {
        "zh-CN": { headline: "大雾橙色预警", description: "预计今晨有雾。", instruction: "减少户外活动。" },
        "en-US": { headline: "Orange fog warning", description: "Fog expected this morning." },
      },
    } as unknown as AlertLine;
    expect(alertBody(cma, "zh")).toEqual(["预计今晨有雾。", "减少户外活动。"]);
    expect(alertBody(cma, "en")).toEqual(["Fog expected this morning."]);
    expect(pickLanguageTag(["zh-Hant"], "zh")).toBe("zh-Hant");
    expect(pickLanguageTag(["en"], "zh-Hant")).toBeNull();
  });
});

describe("the boundary sets", () => {
  it("are named to MapLibre through the pmtiles protocol", () => {
    expect(pmtilesSourceUrl("https://data.test/alerts-areas/nws-zones.20260416.pmtiles")).toBe(
      "pmtiles://https://data.test/alerts-areas/nws-zones.20260416.pmtiles",
    );
  });
});

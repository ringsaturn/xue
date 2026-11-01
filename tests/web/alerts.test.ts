/** The weather warnings in the shell (`docs/alerts.md`): the pointer, index
 * and network-file readers on the committed golden, how area-coded
 * warnings are joined to their boundary sets, the style the layers add,
 * the card's text choices and the boundary sets' source URL. */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import alertsPointerJson from "../fixtures/alerts/expected/latest-alerts.json";

import { alertHeadline } from "../../web/src/alerts/card";
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
    expect(index.networks.map((network) => network.id)).toEqual(["nws", "jma", "eccc"]);
    expect(index.networks[1]!.areas?.set).toBe("jma-class20");
    expect(index.networks[2]!.areas).toBeNull();
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
    expect(() => parseAlertsIndex(badLevel)).toThrow(/1 … 5/);
    expect(() => parseAlertsIndex({ ...raw, alerts: [...rows].reverse() })).toThrow(/sorted/);
    expect(() => parseAlertsIndex({ ...raw, schemaVersion: 2 })).toThrow(/schema version/);
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
    expect(inline.features.length).toBe(lines.flat().filter((line) => line.geometry !== null).length);
    const targets = areaTargets(index.networks, lines);
    const jma = targets.get("jma-class20.20260226/class20")!;
    const jmaCodes = new Set(lines[1]!.flatMap((line) => line.areas));
    expect(new Set(jma.levels.keys())).toEqual(jmaCodes);
    // No warning with a polygon of its own is drawn again by its codes.
    for (const target of targets.values())
      for (const ids of target.alerts.values())
        for (const id of ids) expect(lines.flat().find((line) => line.id === id)!.geometry).toBeNull();
  });

  it("gives each code the highest level in force on it", () => {
    const { index, lines } = load();
    const jma = lines[1]!.map((line, at) => (at === 0 ? { ...line, level: 5 as const } : line));
    const target = areaTargets(index.networks, [[], jma, []]).get("jma-class20.20260226/class20")!;
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

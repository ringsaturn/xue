/** The air-quality product in the shell (`docs/aq.md`): the pointer, index,
 * stations-table and network-file readers on the committed golden, the
 * marks' PM2.5 colouring and the nearest station a pin reads. */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import aqPointerJson from "../fixtures/aq/expected/latest-aq.json";

import {
  aqUnit,
  assembleAqIndex,
  parseAqIndex,
  parseAqPointer,
  parseAqStation,
  parseAqStationTable,
  type AqIndex,
} from "../../web/src/aq/schema";
import { aqColorExpression, aqFeatures, pm25Color, PM25_STEPS, stationDataOf } from "../../web/src/stations/layers";
import { nearestAq } from "../../web/src/stations/nearest";

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

const ROUND = join(repositoryRoot(), "tests/fixtures/aq/expected", dirname((aqPointerJson as { path: string }).path));

function read(name: string): Json {
  return JSON.parse(readFileSync(join(ROUND, name), "utf8")) as Json;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function loadIndex(): AqIndex {
  const index = parseAqIndex(read("index.json"));
  const tables = index.networks.map((network, position) =>
    parseAqStationTable(read(network.stations.path), index, position),
  );
  return assembleAqIndex(index, tables);
}

describe("the aq pointer", () => {
  it("reads the golden's, nested under its day", () => {
    const pointer = parseAqPointer(aqPointerJson);
    expect(pointer.product).toBe("aq");
    expect(pointer.path).toMatch(/^aq\/\d{4}\/\d{2}\/\d{2}\/aq\.\d{12}\/index\.json$/);
  });

  it("accepts the flat layout and refuses a mismatched day or another product", () => {
    const flat = { ...aqPointerJson, path: "aq.202610311545/index.json" };
    expect(parseAqPointer(flat).path).toBe("aq.202610311545/index.json");
    expect(() => parseAqPointer({ ...aqPointerJson, path: "aq/2026/10/30/aq.202610311545/index.json" })).toThrow();
    expect(() => parseAqPointer({ ...aqPointerJson, product: "synop" })).toThrow(/product/);
    expect(() => parseAqPointer({ ...aqPointerJson, schemaVersion: 2 })).toThrow(/schema version/);
  });
});

describe("the aq index and stations tables", () => {
  it("reads every network's rows with their units and credit", () => {
    const index = loadIndex();
    expect(index.networks.map((network) => network.id)).toEqual(["airnow", "hkepd"]);
    expect(index.stations.length).toBe(index.networks.reduce((sum, network) => sum + network.stations.count, 0));
    expect(index.networks.every((network) => network.preliminary)).toBe(true);
    expect(aqUnit(index, "pm25")).toBe("µg/m³");
    expect(aqUnit(index, "co")).toBe("ppm");
    const station = index.stations.find((entry) => entry.id.startsWith("hkepd:"))!;
    expect(index.networks[station.network]!.id).toBe("hkepd");
    expect(station.agency).toBe("EPD");
  });

  it("falls back to §2's units without an element table", () => {
    const raw = read("index.json");
    delete raw.elementTable;
    const index = parseAqIndex(raw);
    expect(index.elementTable).toEqual({});
    expect(aqUnit(index, "o3")).toBe("ppb");
  });

  it("refuses a table whose rows disagree with the index", () => {
    const index = parseAqIndex(read("index.json"));
    const table = read(index.networks[1]!.stations.path);
    const reordered = clone(table);
    (reordered.stations as unknown[][]).reverse();
    expect(() => parseAqStationTable(reordered, index, 1)).toThrow(/sorted/);
    const outOfRange = clone(table);
    (outOfRange.stations as unknown[][])[0]![8] = 20000;
    expect(() => parseAqStationTable(outOfRange, index, 1)).toThrow(/outside/);
    expect(() => parseAqStationTable(table, index, 0)).toThrow(/network/);
  });

  it("drops a network whose table failed and keeps the rest", () => {
    const index = parseAqIndex(read("index.json"));
    const hk = parseAqStationTable(read(index.networks[1]!.stations.path), index, 1);
    const assembled = assembleAqIndex(index, [null, hk]);
    expect(assembled.networks.map((network) => network.id)).toEqual(["hkepd"]);
    expect(assembled.stations.every((station) => station.network === 0)).toBe(true);
  });

  it("reads a station's line by the span its row gives", () => {
    const index = loadIndex();
    for (const network of index.networks) {
      const bytes = readFileSync(join(ROUND, network.file.path));
      for (const station of index.stations.filter((entry) => index.networks[entry.network] === network)) {
        const slice = bytes.subarray(station.offset, station.offset + station.length).toString("utf8");
        const series = parseAqStation(JSON.parse(slice));
        expect(series.id).toBe(station.id);
        for (const column of Object.values(series.obs)) expect(column.length).toBe(series.time.length);
      }
    }
  });
});

describe("the aq marks", () => {
  it("colour by PM2.5 on fixed steps and leave a station without it hollow", () => {
    expect(pm25Color(null)).toBeNull();
    expect(pm25Color(3)).toBe(PM25_STEPS[0]![1]);
    expect(pm25Color(25)).toBe(PM25_STEPS[2]![1]);
    expect(pm25Color(5000)).toBe(PM25_STEPS[PM25_STEPS.length - 1]![1]);
    expect(aqColorExpression()[0]).toBe("case");
  });

  it("carry the station and its network for the card", () => {
    const index = loadIndex();
    const features = aqFeatures(index).features;
    expect(features.length).toBe(index.stations.length);
    const data = stationDataOf(features[0]!);
    expect(data?.kind).toBe("aq");
    if (data?.kind === "aq") expect(data.network.id).toBe(index.networks[data.station.network]!.id);
    const missing = index.stations.find((station) => station.values.pm25 === null);
    if (missing) {
      const feature = features.find((entry) => entry.id === missing.id)!;
      expect(feature.properties).not.toHaveProperty("pm25");
    }
    expect(aqFeatures(null).features).toEqual([]);
  });

  it("answer a pin with the nearest station that has PM2.5, within 20 km", () => {
    const index = loadIndex();
    const station = index.stations.find((entry) => entry.values.pm25 !== null)!;
    const found = nearestAq(index, station.lon + 0.01, station.lat);
    expect(found?.station.values.pm25).not.toBeNull();
    expect(found!.distanceKm).toBeLessThan(20);
    expect(nearestAq(index, station.lon, station.lat - 60)).toBeNull();
  });
});

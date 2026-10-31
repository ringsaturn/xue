/** The surface station product in the shell (`docs/synop.md`): the index,
 * pointer and network-file readers on the committed golden, the marks, the
 * nearest-station choice by ground height, and the meteogram marks. */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import synopPointerJson from "../fixtures/synop/expected/latest-synop.json";

import {
  SYNOP_AUTOMATIC_ZOOM,
  stationDataOf,
  synopColorExpression,
  synopFeatures,
} from "../../web/src/stations/layers";
import { nearestSynopStations, synopForElevation } from "../../web/src/stations/nearest";
import {
  assembleSynopIndex,
  parseSynopIndex,
  parseSynopPointer,
  parseSynopStation,
  parseSynopStationTable,
  type SynopIndex,
  type SynopStationSeries,
} from "../../web/src/stations/schema";
import {
  SYNOP_MATCH_MS,
  synopObservedValue,
  synopRowObservations,
  synopTakesRow,
} from "../../web/src/stations/synopobs";

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
type Row = unknown[];

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** The live round of the golden, whichever index version it was written
 * in: the pointer names the directory. */
const ROUND = join(
  repositoryRoot(),
  "tests/fixtures/synop/expected",
  dirname((synopPointerJson as { path: string }).path),
);
const goldenIndex = JSON.parse(readFileSync(join(ROUND, "index.json"), "utf8")) as Json;

function readTable(name: string): Json {
  return JSON.parse(readFileSync(join(ROUND, name), "utf8")) as Json;
}

/** A v1 index split into the v2 index and its station tables (§4): the
 * network column leaves each row and decides which table it goes to. */
function toV2(v1: Json): { index: Json; tables: Record<string, Json> } {
  const networks = clone(v1.networks) as Json[];
  const tables: Record<string, Json> = {};
  for (const network of networks) {
    tables[`${network.id as string}.stations.json`] = {
      schemaVersion: 2,
      network: network.id,
      issued: v1.issued,
      elements: clone(v1.elements),
      stations: [],
    };
  }
  for (const row of v1.stations as Row[]) {
    const network = networks[row[1] as number]!;
    const table = tables[`${network.id as string}.stations.json`]!;
    (table.stations as Row[]).push([row[0], ...row.slice(2)]);
  }
  for (const network of networks) {
    const name = `${network.id as string}.stations.json`;
    network.stations = {
      path: name,
      byteLength: 1,
      crc32: "00000000",
      count: (tables[name]!.stations as Row[]).length,
    };
  }
  const index = clone(v1);
  delete index.stations;
  index.schemaVersion = 2;
  index.networks = networks;
  return { index, tables };
}

/** A v2 index and its tables joined back into the v1 index it replaces. */
function toV1(v2: Json, tables: Record<string, Json>): Json {
  const networks = clone(v2.networks) as Json[];
  const rows: Row[] = [];
  networks.forEach((network, position) => {
    const table = tables[(network.stations as Json).path as string]!;
    for (const row of table.stations as Row[]) rows.push([row[0], position, ...row.slice(1)]);
    delete network.stations;
  });
  rows.sort((a, b) => ((a[0] as string) < (b[0] as string) ? -1 : 1));
  return { ...clone(v2), schemaVersion: 1, networks, stations: rows };
}

const golden = (() => {
  if (goldenIndex.schemaVersion === 1) return { v1: goldenIndex, ...toV2(goldenIndex) };
  const tables: Record<string, Json> = {};
  for (const network of goldenIndex.networks as Json[]) {
    const name = (network.stations as Json).path as string;
    tables[name] = readTable(name);
  }
  return { v1: toV1(goldenIndex, tables), index: goldenIndex, tables };
})();

const synopIndexJson = golden.v1;

/** The v2 golden read the way `fetchSynopIndex` reads it. */
function assembleV2(index: Json, tables: Record<string, Json>): SynopIndex {
  const head = parseSynopIndex(index);
  return assembleSynopIndex(
    head,
    head.networks.map((network, position) =>
      parseSynopStationTable(tables[network.stations!.path], head, position),
    ),
  );
}

const index = parseSynopIndex(synopIndexJson);

const networkFiles = new Map<string, Uint8Array>();
function networkFile(path: string): Uint8Array {
  let bytes = networkFiles.get(path);
  if (!bytes) {
    bytes = new Uint8Array(readFileSync(join(ROUND, path)));
    networkFiles.set(path, bytes);
  }
  return bytes;
}

function line(id: string): SynopStationSeries {
  const row = index.stations.find((station) => station.id === id)!;
  const file = networkFile(index.networks[row.network]!.file.path);
  const slice = file.subarray(row.offset, row.offset + row.length);
  return parseSynopStation(JSON.parse(new TextDecoder().decode(slice)));
}

describe("the synop readers", () => {
  it("read the golden index and pointer", () => {
    expect(parseSynopPointer(synopPointerJson).path).toMatch(/^synop\.\d{12}\/index\.json$/);
    expect(index.networks[0]!.id).toBe("amedas");
    expect(index.networks[0]!.file.path).toBe("amedas.jsonl");
    const fuji = index.stations.find((station) => station.id === "amedas:50066")!;
    expect(fuji.elev).toBe(3775);
    expect(fuji.rank).toBe(0);
    expect(fuji.values.t).not.toBeNull();
    // On the hour only, carried into the index for the rest of the hour.
    expect(fuji.values.rh).not.toBeNull();
  });

  it("slice every station out of its network file by its span", () => {
    for (const station of index.stations) {
      expect(line(station.id).id).toBe(station.id);
    }
    const tokyo = line("amedas:44132");
    expect(tokyo.time).toHaveLength(3);
    expect(tokyo.obs.t).toHaveLength(3);
    expect(tokyo.names?.ja).toBe("東京");
  });

  it("refuse a row that names another network, or spans past its file", () => {
    const foreign = clone(synopIndexJson) as { stations: unknown[][] };
    foreign.stations[0]![0] = "gts:47401";
    expect(() => parseSynopIndex(foreign)).toThrow(/network/);
    const long = clone(synopIndexJson) as { stations: unknown[][] };
    const last = long.stations[long.stations.length - 1]!;
    last[last.length - 1] = 10_000_000;
    expect(() => parseSynopIndex(long)).toThrow(/past the end/);
    const hot = clone(synopIndexJson) as { stations: unknown[][] };
    hot.stations[0]![8] = 99;
    expect(() => parseSynopIndex(hot)).toThrow(/outside/);
  });

  it("admit an element this build does not know", () => {
    const wider = clone(synopIndexJson) as { elements: string[]; stations: unknown[][] };
    wider.elements.push("dewpoint2");
    for (const row of wider.stations) row.splice(8 + wider.elements.length - 1, 0, 1.5);
    expect(parseSynopIndex(wider).stations[0]!.values.dewpoint2).toBe(1.5);
  });
});

describe("the synop v2 readers", () => {
  /** A second network ahead of the golden's, so assembly has something to
   * merge and renumber: two stations whose ids sort before the others. */
  function twoNetworks(): { index: Json; tables: Record<string, Json> } {
    const { index: head, tables } = clone({ index: golden.index, tables: golden.tables });
    const elements = head.elements as string[];
    const networks = head.networks as Json[];
    const values = elements.map((key) => (key === "t" ? 4.5 : key === "td" ? -1.2 : null));
    networks.unshift({
      ...clone(networks[0]!),
      id: "aaa",
      name: "A network",
      file: { path: "aaa.jsonl", byteLength: 200, crc32: "0000000a" },
      stations: { path: "aaa.stations.json", byteLength: 300, crc32: "0000000b", count: 2 },
    });
    tables["aaa.stations.json"] = {
      schemaVersion: 2,
      network: "aaa",
      issued: head.issued,
      elements: clone(elements),
      stations: [
        ["aaa:1", 10, 20, 5, 0, "One", head.issued, ...values, 0, 90],
        ["aaa:2", 11, 21, null, 1, null, head.issued, ...values, 100, 90],
      ],
    };
    return { index: head, tables };
  }

  it("assemble the v1 index from the v2 index and its tables", () => {
    const assembled = assembleV2(golden.index, golden.tables);
    expect(assembled.schemaVersion).toBe(2);
    expect(assembled.networks.map((network) => network.id)).toEqual(
      index.networks.map((network) => network.id),
    );
    expect(assembled.networks[0]!.stations!.path).toBe("amedas.stations.json");
    expect(assembled.stations).toEqual(index.stations);
    expect(assembled.elements).toEqual(index.elements);
  });

  it("name each network's table and leave the stations to it", () => {
    const head = parseSynopIndex(golden.index);
    expect(head.stations).toEqual([]);
    for (const network of head.networks) {
      expect(network.stations!.path).toBe(`${network.id}.stations.json`);
      expect(network.stations!.count).toBe(
        (golden.tables[network.stations!.path]!.stations as Row[]).length,
      );
    }
    const unnamed = clone(golden.index) as { networks: Json[] };
    delete unnamed.networks[0]!.stations;
    expect(() => parseSynopIndex(unnamed)).toThrow(/stations/);
    const misnamed = clone(golden.index) as { networks: { stations: Json }[] };
    misnamed.networks[0]!.stations.path = "other.stations.json";
    expect(() => parseSynopIndex(misnamed)).toThrow(/stations\.json/);
    const future = clone(golden.index);
    future.schemaVersion = 3;
    expect(() => parseSynopIndex(future)).toThrow(/schema version 3/);
  });

  it("merge the networks in id order, network as its position", () => {
    const { index: head, tables } = twoNetworks();
    const assembled = assembleV2(head, tables);
    expect(assembled.networks[0]!.id).toBe("aaa");
    expect(assembled.stations.slice(0, 2).map((station) => station.id)).toEqual(["aaa:1", "aaa:2"]);
    expect(assembled.stations[0]!.network).toBe(0);
    expect(assembled.stations[0]!.values.t).toBe(4.5);
    // The committed golden may predate td; the table carries it only then.
    if (assembled.elements.includes("td")) expect(assembled.stations[0]!.values.td).toBe(-1.2);
    expect(assembled.stations[1]!.elev).toBeNull();
    const fuji = assembled.stations.find((station) => station.id === "amedas:50066")!;
    expect(fuji.network).toBe(1);
    const ids = assembled.stations.map((station) => station.id);
    expect(ids).toEqual([...ids].sort());
  });

  it("drop a network whose table failed, renumbering the rest", () => {
    const { index: head, tables } = twoNetworks();
    const parsed = parseSynopIndex(head);
    const rows = parsed.networks.map((network, position) =>
      position === 0 ? null : parseSynopStationTable(tables[network.stations!.path], parsed, position),
    );
    const assembled = assembleSynopIndex(parsed, rows);
    expect(assembled.networks.map((network) => network.id)).not.toContain("aaa");
    expect(assembled.stations.some((station) => station.id.startsWith("aaa:"))).toBe(false);
    expect(assembled.stations.find((station) => station.id === "amedas:50066")!.network).toBe(0);
    expect(assembled.stations).toEqual(index.stations);
  });

  it("refuse a table that is not the one the index names", () => {
    const { index: head, tables } = twoNetworks();
    const parsed = parseSynopIndex(head);
    const table = (): Json => clone(tables["aaa.stations.json"]!);
    const read = (input: Json) => () => parseSynopStationTable(input, parsed, 0);
    expect(read(table())).not.toThrow();

    const v1 = table();
    v1.schemaVersion = 1;
    expect(read(v1)).toThrow(/schema version 1/);
    const other = table();
    other.network = "amedas";
    expect(read(other)).toThrow(/network must be aaa/);
    const reordered = table();
    (reordered.elements as string[]).reverse();
    expect(read(reordered)).toThrow(/elements must equal/);
    const short = table();
    (short.stations as Row[]).pop();
    expect(read(short)).toThrow(/the index says 2/);
    const wide = table();
    (wide.stations as Row[])[0]!.splice(1, 0, 0);
    expect(read(wide)).toThrow(/row of/);
    const unsorted = table();
    (unsorted.stations as Row[]).reverse();
    expect(read(unsorted)).toThrow(/sorted by id and unique/);
    const twice = table();
    (twice.stations as Row[])[1]![0] = "aaa:1";
    expect(read(twice)).toThrow(/sorted by id and unique/);
    const foreign = table();
    (foreign.stations as Row[])[1]![0] = "bbb:2";
    expect(read(foreign)).toThrow(/does not belong/);
    const overlapping = table();
    const second = (overlapping.stations as Row[])[1]!;
    second[second.length - 2] = 50;
    expect(read(overlapping)).toThrow(/precedes/);
    const long = table();
    const last = (long.stations as Row[])[1]!;
    last[last.length - 1] = 101;
    expect(read(long)).toThrow(/past the end/);
  });

  it("hold a dew point to its range", () => {
    const { index: head, tables } = twoNetworks();
    const elements = clone(head.elements as string[]);
    if (!elements.includes("td")) {
      // A v1 golden predates `td`: widen it the way v2 does, second.
      elements.splice(1, 0, "td");
      head.elements = elements;
      for (const table of Object.values(tables)) {
        table.elements = clone(elements);
        for (const row of table.stations as Row[]) row.splice(8, 0, null);
      }
    }
    const tdAt = 7 + elements.indexOf("td");
    const parsed = parseSynopIndex(head);
    const table = tables["aaa.stations.json"]!;
    (table.stations as Row[])[0]![tdAt] = -12.3;
    expect(parseSynopStationTable(table, parsed, 0)[0]!.values.td).toBe(-12.3);
    (table.stations as Row[])[0]![tdAt] = 61;
    expect(() => parseSynopStationTable(table, parsed, 0)).toThrow(/outside/);
  });
});

describe("the synop marks", () => {
  it("carry their rank, temperature and data", () => {
    const features = synopFeatures(index).features;
    expect(features).toHaveLength(index.stations.length);
    const gauge = features.find((feature) => feature.id === "amedas:12217")!;
    expect(gauge.properties!.rank).toBe(2);
    // No thermometer: no `t`, so the colour expression falls to the grey.
    expect(gauge.properties!.t).toBeUndefined();
    const data = stationDataOf(features[0]!);
    expect(data?.kind).toBe("synop");
    expect(synopFeatures(null).features).toEqual([]);
    expect(synopColorExpression()[0]).toBe("case");
    expect(SYNOP_AUTOMATIC_ZOOM).toBeGreaterThan(0);
  });
});

describe("the station a pin reads", () => {
  it("is the nearest one at about the pin's height", () => {
    // A pin on Fuji's flank: the summit station is nearest, but 1.5 km above.
    const candidates = nearestSynopStations(index, 138.73, 35.33, 200);
    expect(candidates[0]!.station.id).toBe("amedas:50066");
    expect(synopForElevation(candidates, null)!.station.id).toBe("amedas:50066");
    expect(synopForElevation(candidates, 3700)!.station.id).toBe("amedas:50066");
    const below = synopForElevation(candidates, 2200);
    expect(below?.station.id).not.toBe("amedas:50066");
    expect(nearestSynopStations(index, 0, 0)).toEqual([]);
  });
});

describe("the meteogram marks", () => {
  const tokyo = line("amedas:44132");
  const runTimeMs = Date.parse("2026-10-04T18:00:00Z");
  const axis = { runTimeMs, firstLead: 0, lastLead: 240 * 3600 };

  it("put the temperature on the terrain row when there is one", () => {
    expect(synopTakesRow("temperature", false)).toBe(true);
    expect(synopTakesRow("terrain", false)).toBe(false);
    expect(synopTakesRow("temperature", true)).toBe(false);
    expect(synopTakesRow("terrain", true)).toBe(true);
    expect(synopTakesRow("cloud", false)).toBe(false);
  });

  it("place each report by its own clock", () => {
    const marks = synopRowObservations("temperature", tokyo, axis);
    expect(marks).toHaveLength(3);
    expect(marks[0]!.x).toBe(tokyo.time[0]! - runTimeMs / 1000);
    expect(marks.every((mark) => mark.kind === "dot")).toBe(true);
    // A run that starts after the window has nowhere to put them.
    expect(synopRowObservations("temperature", tokyo, { ...axis, runTimeMs: Date.parse("2026-10-06T00:00:00Z") })).toEqual([]);
  });

  it("read the report nearest the playhead, or nothing", () => {
    const newest = tokyo.time[tokyo.time.length - 1]! * 1000;
    expect(synopObservedValue("temperature", tokyo, newest)).toBe(tokyo.obs.t![tokyo.time.length - 1]);
    expect(synopObservedValue("temperature", tokyo, newest + SYNOP_MATCH_MS + 1)).toBeNull();
    expect(synopObservedValue("cloud", tokyo, newest)).toBeNull();
  });
});

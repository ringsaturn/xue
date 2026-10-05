/** The surface station product in the shell (`docs/synop.md`): the index,
 * pointer and network-file readers on the committed golden, the marks, the
 * nearest-station choice by ground height, and the meteogram marks. */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import synopIndexJson from "../fixtures/synop/expected/synop.202610050010/index.json";
import synopPointerJson from "../fixtures/synop/expected/latest-synop.json";

import {
  SYNOP_AUTOMATIC_ZOOM,
  stationDataOf,
  synopColorExpression,
  synopFeatures,
} from "../../web/src/stations/layers";
import { nearestSynopStations, synopForElevation } from "../../web/src/stations/nearest";
import {
  parseSynopIndex,
  parseSynopPointer,
  parseSynopStation,
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

const AMEDAS_JSONL = new Uint8Array(
  readFileSync(join(repositoryRoot(), "tests/fixtures/synop/expected/synop.202610050010/amedas.jsonl")),
);

const index = parseSynopIndex(synopIndexJson);

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function line(id: string): SynopStationSeries {
  const row = index.stations.find((station) => station.id === id)!;
  const slice = AMEDAS_JSONL.subarray(row.offset, row.offset + row.length);
  return parseSynopStation(JSON.parse(new TextDecoder().decode(slice)));
}

describe("the synop readers", () => {
  it("read the golden index and pointer", () => {
    expect(parseSynopPointer(synopPointerJson).path).toBe("synop.202610050010/index.json");
    expect(index.networks.map((network) => network.id)).toEqual(["amedas"]);
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

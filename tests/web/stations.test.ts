import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import airportIndexJson from "../fixtures/airport/expected/airport.202609161440/index.json";
import airportPointerJson from "../fixtures/airport/expected/latest-airport.json";
import soundingIndexJson from "../fixtures/sounding/expected/index.json";
import soundingPointerJson from "../fixtures/sounding/expected/latest-sounding.json";

import {
  fetchAirportStation,
  fetchSoundingStation,
  parseContentRange,
  rangeHeader,
  stationFileUrl,
  type LoadedAirportIndex,
  type LoadedSoundingIndex,
} from "../../web/src/stations/fetch";
import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";

import {
  AIRPORT_ALL_ZOOM,
  AIRPORT_STALE_MS,
  SOUNDING_STALE_MS,
  airportDrawnAtZoom,
  airportFeatures,
  categoryColor,
  categoryColorExpression,
  freshnessOpacity,
  isStale,
  soundingFeatures,
  soundingFill,
  stationDataOf,
  stationLayerSpecs,
  stationSourceSpecs,
} from "../../web/src/stations/layers";
import {
  parseAirportIndex,
  parseAirportPointer,
  parseAirportStation,
  parseSoundingIndex,
  parseSoundingPointer,
  parseSoundingStation,
  type AirportStation,
} from "../../web/src/stations/schema";
import { parseStationsFromSearch, searchWithStations } from "../../web/src/urlstate";

/** Under jsdom `import.meta.url` is not a file URL, so the two `.jsonl`
 * goldens — which are not JSON modules and cannot be imported — are found
 * from the working directory, the way `zarr.test.ts` finds its fixtures. */
function repositoryRoot(): string {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory) throw new Error("repository root not found from the working directory");
    directory = parent;
  }
  return directory;
}

function fixture(path: string): Uint8Array {
  return new Uint8Array(readFileSync(join(repositoryRoot(), "tests/fixtures", path)));
}

const SOUNDINGS_JSONL = fixture("sounding/expected/soundings.jsonl");
const HISTORY_JSONL = fixture("airport/expected/airport.202609161440/history.jsonl");

const soundingIndex = parseSoundingIndex(soundingIndexJson);
const airportIndex = parseAirportIndex(airportIndexJson);

/** A deep copy of a golden, for the tests that damage one. */
function copy<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

describe("the station product schemas", () => {
  it("accepts the committed sounding golden", () => {
    const pointer = parseSoundingPointer(soundingPointerJson);
    expect(pointer.path).toBe("sounding.2026091402/index.json");
    expect(soundingIndex.soundings.path).toBe("soundings.jsonl");
    expect(soundingIndex.stations.length).toBeGreaterThan(0);
    const first = soundingIndex.stations[0]!;
    expect(first.id).toBe("0-20000-0-47401");
    expect(first.wmo).toBe("47401");
    expect(first.latest).toBe(first.times[0]);
    expect(first.headline.levels).toBe(15);
    // The spans tile the file exactly, newline by newline.
    const last = soundingIndex.stations[soundingIndex.stations.length - 1]!;
    expect(last.offset + last.length + 1).toBe(soundingIndex.soundings.byteLength);
  });

  it("accepts the committed airport golden, rows expanded", () => {
    const pointer = parseAirportPointer(airportPointerJson);
    expect(pointer.path).toBe("airport.202609161440/index.json");
    const station = airportIndex.stations.find((entry) => entry.icao === "FACT")!;
    expect(station).toMatchObject({
      icao: "FACT",
      lat: -33.965,
      lon: 18.602,
      obsTime: "2026-09-16T14:00:00Z",
      t: 16,
      td: 9,
      wd: 170,
      ws: 12.3,
      gust: null,
      vis: 10000,
      qnh: 1027.8,
      category: "VFR",
      tafPresent: true,
    });
    // A variable wind direction is null with the speed kept, never a north
    // wind, and a station without a category is admitted.
    const uncategorized = airportIndex.stations.find((entry) => entry.category === null);
    expect(uncategorized).toBeDefined();
  });

  it("reads every line of both jsonl files", () => {
    const soundings = new TextDecoder().decode(SOUNDINGS_JSONL).split("\n").filter(Boolean);
    expect(soundings).toHaveLength(soundingIndex.stations.length);
    for (const [position, line] of soundings.entries()) {
      const station = parseSoundingStation(JSON.parse(line));
      expect(station.id).toBe(soundingIndex.stations[position]!.id);
      expect(station.soundings[0]!.p).toHaveLength(station.soundings[0]!.n);
    }
    const history = new TextDecoder().decode(HISTORY_JSONL).split("\n").filter(Boolean);
    expect(history).toHaveLength(airportIndex.stations.length);
    for (const [position, line] of history.entries()) {
      const station = parseAirportStation(JSON.parse(line));
      expect(station.icao).toBe(airportIndex.stations[position]!.icao);
      expect(station.metars.length).toBeGreaterThan(0);
    }
  });

  it("refuses a schema version it does not implement", () => {
    const sounding = copy(soundingIndexJson) as { schemaVersion: number };
    sounding.schemaVersion = 2;
    expect(() => parseSoundingIndex(sounding)).toThrow(/schema version/);
    const airport = copy(airportIndexJson) as { schemaVersion: number };
    airport.schemaVersion = 2;
    expect(() => parseAirportIndex(airport)).toThrow(/schema version/);
    const pointer = copy(soundingPointerJson) as { schemaVersion: number };
    pointer.schemaVersion = 2;
    expect(() => parseSoundingPointer(pointer)).toThrow(/schema version/);
  });

  it("refuses a station row that is not sixteen values", () => {
    const payload = copy(airportIndexJson) as { stations: unknown[][] };
    payload.stations[0] = payload.stations[0]!.slice(0, 15);
    expect(() => parseAirportIndex(payload)).toThrow(/row of 16 values/);
  });

  it("refuses a span that reaches past the file it indexes", () => {
    const sounding = copy(soundingIndexJson) as {
      soundings: { byteLength: number };
      stations: { offset: number; length: number }[];
    };
    sounding.stations[0]!.length = sounding.soundings.byteLength + 1;
    expect(() => parseSoundingIndex(sounding)).toThrow(/past the end/);
    const airport = copy(airportIndexJson) as {
      history: { byteLength: number };
      stations: unknown[][];
    };
    const row = airport.stations[airport.stations.length - 1]!;
    row[14] = airport.history.byteLength;
    expect(() => parseAirportIndex(airport)).toThrow(/past the end/);
  });

  it("refuses a value outside the range the contract admits", () => {
    const payload = copy(airportIndexJson) as { stations: unknown[][] };
    payload.stations[0]![7] = 400;
    expect(() => parseAirportIndex(payload)).toThrow(/wd is outside/);
    const sounding = copy(soundingIndexJson) as { stations: { lat: number }[] };
    sounding.stations[0]!.lat = 120;
    expect(() => parseSoundingIndex(sounding)).toThrow(/lat is outside/);
  });

  it("admits a visibility beyond ten kilometres, which the contract does not cap", () => {
    // KWHP reports 99 statute miles: 159 300 m, and the row is genuine.
    const payload = copy(airportIndexJson) as { stations: unknown[][] };
    payload.stations[0]![10] = 159300;
    expect(parseAirportIndex(payload).stations[0]!.vis).toBe(159300);
  });
});

describe("what the marks are drawn from", () => {
  it("ramps the sounding fill from its 500 hPa temperature", () => {
    // The ends clamp, so a −60 °C ascent and a +5 °C one are drawn at the
    // ramp's own extremes rather than off it.
    expect(soundingFill(-60)).toBe(soundingFill(-45));
    expect(soundingFill(-45)).toBe("#3b4cc0");
    expect(soundingFill(5)).toBe(soundingFill(-5));
    expect(soundingFill(-5)).toBe("#c9482f");
    // Halfway between two stops is halfway between their colors.
    expect(soundingFill(-40)).toBe("#556ecb");
    // A sounding that never reached 500 hPa is hollow.
    expect(soundingFill(null)).toBe("rgba(0, 0, 0, 0)");
  });

  it("colors an airport by its flight category, one set per ground", () => {
    expect(categoryColor("VFR", false)).not.toBe(categoryColor("VFR", true));
    expect(categoryColor(null, false)).toBe(categoryColor(null, false));
    const expression = categoryColorExpression(false);
    expect(expression[0]).toBe("match");
    expect(expression).toContain(categoryColor("LIFR", false));
    // The fallback is the last member: a station with no category.
    expect(expression[expression.length - 1]).toBe(categoryColor(null, false));
  });

  it("thins the airports below the zoom the whole set reads at", () => {
    const withTaf = airportIndex.stations.find((station) => station.tafPresent)!;
    const without = airportIndex.stations.find((station) => !station.tafPresent)!;
    expect(airportDrawnAtZoom(withTaf, 0)).toBe(true);
    expect(airportDrawnAtZoom(withTaf, 10)).toBe(true);
    expect(airportDrawnAtZoom(without, AIRPORT_ALL_ZOOM - 0.1)).toBe(false);
    expect(airportDrawnAtZoom(without, AIRPORT_ALL_ZOOM)).toBe(true);
  });

  it("dims a station whose observation is far from the playhead", () => {
    const now = Date.parse("2026-09-16T14:30:00Z");
    expect(isStale(now, now, AIRPORT_STALE_MS)).toBe(false);
    expect(isStale(now - AIRPORT_STALE_MS, now, AIRPORT_STALE_MS)).toBe(false);
    expect(isStale(now - AIRPORT_STALE_MS - 1, now, AIRPORT_STALE_MS)).toBe(true);
    // Symmetric: a playhead in a run's forecast hours is ahead of every
    // observation.
    expect(isStale(now + AIRPORT_STALE_MS + 1, now, AIRPORT_STALE_MS)).toBe(true);
    expect(isStale(now - 12 * 3600 * 1000, now, SOUNDING_STALE_MS)).toBe(false);
    // Before a dataset is open nothing is measured against anything.
    expect(freshnessOpacity(null, AIRPORT_STALE_MS)).toBeTypeOf("number");
    const expression = freshnessOpacity(now, AIRPORT_STALE_MS);
    expect(Array.isArray(expression) ? expression[0] : expression).toBe("case");
  });

  it("builds one feature per station, carrying what the card reads", () => {
    const soundings = soundingFeatures(soundingIndex);
    expect(soundings.features).toHaveLength(soundingIndex.stations.length);
    const first = soundings.features[0]!;
    expect(first.geometry).toEqual({
      type: "Point",
      coordinates: [soundingIndex.stations[0]!.lon, soundingIndex.stations[0]!.lat],
    });
    expect(first.properties!.time).toBe(Date.parse(soundingIndex.stations[0]!.latest));
    const data = stationDataOf(first)!;
    expect(data.kind).toBe("sounding");
    expect(data.station.lat).toBe(soundingIndex.stations[0]!.lat);

    const airports = airportFeatures(airportIndex);
    expect(airports.features).toHaveLength(airportIndex.stations.length);
    const airport = stationDataOf(airports.features[0]!)!;
    expect(airport.kind).toBe("airport");
    expect((airport.station as AirportStation).icao).toBe(airportIndex.stations[0]!.icao);
    // A product that is not drawn is an empty collection, not a throw.
    expect(soundingFeatures(null).features).toEqual([]);
    expect(airportFeatures(null).features).toEqual([]);
  });
});

describe("the style the layers add", () => {
  it("is a style MapLibre accepts, on either ground and with a playhead", () => {
    for (const darkGround of [false, true]) {
      for (const time of [null, Date.parse("2026-09-16T14:30:00Z")]) {
        const sources = Object.fromEntries(
          stationSourceSpecs({ soundings: soundingIndex, airports: airportIndex }).map(
            ({ id, source }) => [id, source],
          ),
        );
        const errors = validateStyleMin({
          version: 8,
          sources,
          layers: stationLayerSpecs({ darkGround, time, ink: "#333333" }),
        });
        expect(errors).toEqual([]);
      }
    }
  });

  it("draws the soundings over the airports, all three under one anchor", () => {
    const layers = stationLayerSpecs({ darkGround: false, time: null, ink: "#333333" });
    expect(layers.map((layer) => layer.id)).toEqual([
      "station-airport-minor",
      "station-airport-major",
      "station-sounding",
    ]);
    // Only the thinned-out half is held back by zoom; a station with a
    // current TAF is drawn at every zoom.
    expect(layers[0]!.minzoom).toBe(AIRPORT_ALL_ZOOM);
    expect(layers[1]!.minzoom).toBeUndefined();
  });
});

describe("reading one station by range", () => {
  const soundingLoaded: LoadedSoundingIndex = {
    pointer: parseSoundingPointer(soundingPointerJson),
    index: soundingIndex,
    indexUrl: "https://data.test/sounding.2026091402/index.json?v=07fa0918",
  };
  const airportLoaded: LoadedAirportIndex = {
    pointer: parseAirportPointer(airportPointerJson),
    index: airportIndex,
    indexUrl: "https://data.test/airport.202609161440/index.json?v=ca823538",
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function answer(status: number, body: Uint8Array, headers: Record<string, string> = {}) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
      arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength),
    };
  }

  it("asks for the span the index names, against the file's own ?v=", async () => {
    const station = soundingIndex.stations[2]!;
    const calls: { url: string; range: string | undefined }[] = [];
    vi.stubGlobal("fetch", (url: string | URL, init?: RequestInit) => {
      calls.push({
        url: String(url),
        range: (init?.headers as Record<string, string> | undefined)?.Range,
      });
      const slice = SOUNDINGS_JSONL.subarray(station.offset, station.offset + station.length);
      return Promise.resolve(
        answer(206, slice, {
          "content-range": `bytes ${station.offset}-${station.offset + station.length - 1}/${soundingIndex.soundings.byteLength}`,
        }),
      );
    });
    const parsed = await fetchSoundingStation(soundingLoaded, station);
    expect(parsed.id).toBe(station.id);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("https://data.test/sounding.2026091402/soundings.jsonl?v=06902ee6");
    expect(calls[0]!.range).toBe(`bytes=${station.offset}-${station.offset + station.length - 1}`);
    expect(rangeHeader(0, 1000)).toBe("bytes=0-999");
    expect(stationFileUrl(soundingLoaded.indexUrl, soundingIndex.soundings)).toBe(calls[0]!.url);
  });

  it("slices the body itself when the server ignores the range", async () => {
    const station = airportIndex.stations[1]!;
    vi.stubGlobal("fetch", () => Promise.resolve(answer(200, HISTORY_JSONL)));
    const parsed = await fetchAirportStation(airportLoaded, station);
    expect(parsed.icao).toBe(station.icao);
    expect(parsed.metars[0]!.time).toBe(station.obsTime);
  });

  it("refuses a 206 that answers with another span", async () => {
    const station = soundingIndex.stations[1]!;
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        answer(206, SOUNDINGS_JSONL.subarray(0, station.length), {
          "content-range": `bytes 0-${station.length - 1}/${soundingIndex.soundings.byteLength}`,
        }),
      ),
    );
    await expect(fetchSoundingStation(soundingLoaded, station)).rejects.toThrow(/does not match the span/);
  });

  it("accepts a 206 whose Content-Range the browser cannot read, by its length", async () => {
    // Cross-origin, the header is hidden unless the bucket's CORS policy
    // exposes it; the span is still the one asked for when it is as long.
    const station = soundingIndex.stations[1]!;
    const span = SOUNDINGS_JSONL.subarray(station.offset, station.offset + station.length);
    vi.stubGlobal("fetch", () => Promise.resolve(answer(206, span)));
    expect((await fetchSoundingStation(soundingLoaded, station)).id).toBe(station.id);
    vi.stubGlobal("fetch", () => Promise.resolve(answer(206, span.subarray(0, span.length - 1))));
    await expect(fetchSoundingStation(soundingLoaded, station)).rejects.toThrow(/length mismatch/);
  });

  it("reads a Content-Range, and refuses one it cannot", () => {
    expect(parseContentRange("bytes 100-199/1000")).toEqual({ offset: 100, length: 100 });
    expect(parseContentRange("bytes 0-0/*")).toEqual({ offset: 0, length: 1 });
    expect(parseContentRange("items 1-2/3")).toBeNull();
    expect(parseContentRange(null)).toBeNull();
  });
});

describe("the ?stations= parameter", () => {
  it("is off when the link says nothing", () => {
    expect(parseStationsFromSearch("")).toEqual({ soundings: false, airports: false });
    expect(parseStationsFromSearch("?stations=off")).toEqual({ soundings: false, airports: false });
    expect(parseStationsFromSearch("?stations=nonsense")).toEqual({ soundings: false, airports: false });
  });

  it("reads each product, together and apart", () => {
    expect(parseStationsFromSearch("?stations=snd")).toEqual({ soundings: true, airports: false });
    expect(parseStationsFromSearch("?stations=apt")).toEqual({ soundings: false, airports: true });
    expect(parseStationsFromSearch("?stations=snd,apt")).toEqual({ soundings: true, airports: true });
    expect(parseStationsFromSearch("?stations=SOUNDINGS, Airport")).toEqual({
      soundings: true,
      airports: true,
    });
    expect(parseStationsFromSearch("?stations=on")).toEqual({ soundings: true, airports: true });
    expect(parseStationsFromSearch("?stations=all")).toEqual({ soundings: true, airports: true });
  });

  it("writes only what differs from the default", () => {
    expect(searchWithStations("?type=temp", { soundings: false, airports: false })).toBe("?type=temp");
    expect(searchWithStations("?type=temp", { soundings: true, airports: false })).toBe(
      "?type=temp&stations=snd",
    );
    expect(searchWithStations("?type=temp", { soundings: true, airports: true })).toBe(
      "?type=temp&stations=snd%2Capt",
    );
    // A link that already named the products is rewritten, not doubled.
    expect(searchWithStations("?stations=apt", { soundings: false, airports: false })).toBe("?");
  });

  it("round-trips what it writes", () => {
    for (const state of [
      { soundings: false, airports: false },
      { soundings: true, airports: false },
      { soundings: false, airports: true },
      { soundings: true, airports: true },
    ]) {
      expect(parseStationsFromSearch(searchWithStations("", state))).toEqual(state);
    }
  });
});

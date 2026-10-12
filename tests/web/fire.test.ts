/** The active fire product in the shell (`docs/fire.md`): the pointer,
 * index and line readers, the span a playhead reads and shows, the one
 * range request per sensor, and the `?stations=fire` switch. The product is
 * built here rather than read from the golden, so each rule the reader
 * enforces can be broken one at a time. */

import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchFireLines, sensorSpan, splitLines, type LoadedFireIndex } from "../../web/src/fire/fetch";
import {
  FIRE_SPAN_HOURS,
  fetchSpan,
  fireFeatures,
  shownSpan,
  spanCovers,
} from "../../web/src/fire/layer";
import { parseFireIndex, parseFireLine, parseFirePointer } from "../../web/src/fire/schema";
import { parseStationsFromSearch, searchWithStations } from "../../web/src/urlstate";

const HOUR = 3600;
const H0 = Date.parse("2026-10-12T02:00:00Z") / 1000;

function line(sensor: string, hour: number, n: number) {
  return {
    sensor,
    hour,
    n,
    t: Array.from({ length: n }, (_, i) => (i % 60) * 60),
    lat: Array.from({ length: n }, (_, i) => 480000 + i),
    lon: Array.from({ length: n }, (_, i) => 1190000 + i),
    frp: Array.from({ length: n }, (_, i) => 10 * (i + 1)),
    bt: Array.from({ length: n }, () => 3400),
    conf: "n".repeat(n),
    dn: "D".repeat(n),
  };
}

/** Two sensors' files and the index over them, built the way the
 * publisher lays them out: one line per hour, consecutive spans. */
function product() {
  const sensors = [
    { id: "viirs-noaa20", lines: [line("viirs-noaa20", H0, 2), line("viirs-noaa20", H0 + HOUR, 3), line("viirs-noaa20", H0 + 3 * HOUR, 1)] },
    { id: "modis-aqua", lines: [line("modis-aqua", H0 + HOUR, 1)] },
  ];
  const encoder = new TextEncoder();
  const files: Record<string, Uint8Array> = {};
  const hours: unknown[][] = [];
  const entries = sensors.map((sensor, index) => {
    let offset = 0;
    const chunks: string[] = [];
    for (const item of sensor.lines) {
      const text = JSON.stringify(item);
      const length = encoder.encode(text).length;
      hours.push([index, new Date(item.hour * 1000).toISOString().replace(".000Z", "Z"), item.n, Math.max(...item.frp) / 10, offset, length]);
      offset += length + 1;
      chunks.push(`${text}\n`);
    }
    const bytes = encoder.encode(chunks.join(""));
    files[`${sensor.id}.jsonl`] = bytes;
    return {
      id: sensor.id,
      name: sensor.id === "modis-aqua" ? "MODIS Aqua" : "VIIRS NOAA-20",
      resolution: sensor.id === "modis-aqua" ? 1000 : 375,
      attribution: "NASA FIRMS (LANCE NRT)",
      license: "NASA-open-data",
      url: "https://firms.modaps.eosdis.nasa.gov/",
      latest: "2026-10-12T05:00:00Z",
      count: sensor.lines.reduce((sum, item) => sum + item.n, 0),
      file: { path: `${sensor.id}.jsonl`, byteLength: bytes.length, crc32: "0123abcd" },
    };
  });
  const index = {
    schemaVersion: 1,
    issued: "2026-10-12T05:00:00Z",
    generated: "2026-10-12T05:23:40Z",
    window: { start: "2026-10-05T05:00:00Z", end: "2026-10-12T05:00:00Z" },
    sensors: entries,
    hours,
    sources: [{ id: "firms-noaa-20-viirs-c2", ok: true, reports: 6, rejected: 0 }],
  };
  return { index, files };
}

describe("the fire pointer and index", () => {
  it("reads the pointer the publisher writes", () => {
    const pointer = parseFirePointer({
      schemaVersion: 1,
      product: "fire",
      issued: "2026-10-12T05:00:00Z",
      path: "fire.202610120500/index.json",
      byteLength: 100,
      crc32: "1c0ffee5",
    });
    expect(pointer.path).toBe("fire.202610120500/index.json");
    expect(() =>
      parseFirePointer({ ...pointer, path: "fire.202610120510/index.json" }),
    ).toThrow(/path/);
    expect(() => parseFirePointer({ ...pointer, product: "synop" })).toThrow(/product/);
  });

  it("expands the hour rows and checks their spans", () => {
    const { index } = product();
    const parsed = parseFireIndex(index);
    expect(parsed.sensors.map((sensor) => sensor.id)).toEqual(["viirs-noaa20", "modis-aqua"]);
    expect(parsed.hours).toHaveLength(4);
    expect(parsed.hours[1]!.hour).toBe((H0 + HOUR) * 1000);
    expect(parsed.hours[3]!.offset).toBe(0);
  });

  it("refuses an index whose spans do not follow each other", () => {
    const { index } = product();
    const gap = structuredClone(index);
    (gap.hours[1] as number[])[4] = (gap.hours[1] as number[])[4]! + 1;
    expect(() => parseFireIndex(gap)).toThrow(/follow/);
    const unsorted = structuredClone(index);
    unsorted.hours.reverse();
    expect(() => parseFireIndex(unsorted)).toThrow();
    const past = structuredClone(index);
    past.sensors[1]!.file.byteLength = 3;
    expect(() => parseFireIndex(past)).toThrow(/past the end/);
    expect(() => parseFireIndex({ ...index, schemaVersion: 2 })).toThrow(/schema version/);
  });

  it("admits a sensor it has never seen", () => {
    const { index } = product();
    const renamed = structuredClone(index);
    renamed.sensors[1]!.id = "goes-east-fdc";
    renamed.sensors[1]!.file.path = "goes-east-fdc.jsonl";
    expect(parseFireIndex(renamed).sensors[1]!.id).toBe("goes-east-fdc");
  });
});

describe("a fire line", () => {
  it("decodes to degrees, MW, kelvin and epoch ms", () => {
    const parsed = parseFireLine(line("viirs-noaa20", H0, 2));
    expect(parsed.lat[0]).toBeCloseTo(48.0);
    expect(parsed.lon[1]).toBeCloseTo(119.0001);
    expect(parsed.frp).toEqual([1, 2]);
    expect(parsed.bt[0]).toBe(340);
    expect(parsed.time).toEqual([H0 * 1000, (H0 + 60) * 1000]);
  });

  it("holds every column to n and to its range", () => {
    expect(() => parseFireLine({ ...line("viirs-noaa20", H0, 2), dn: "D" })).toThrow(/dn/);
    expect(() => parseFireLine({ ...line("viirs-noaa20", H0, 1), lat: [950000] })).toThrow(/lat/);
    expect(() => parseFireLine({ ...line("viirs-noaa20", H0, 1), t: [30] })).toThrow(/minute/);
    expect(() => parseFireLine({ ...line("viirs-noaa20", H0, 1), conf: "x" })).toThrow(/conf/);
    expect(() => parseFireLine({ ...line("viirs-noaa20", H0 + 1, 1) })).toThrow(/hour/);
  });
});

describe("the span a playhead reads", () => {
  const window = { start: "2026-10-05T05:00:00Z", end: "2026-10-12T05:00:00Z" };
  const end = Date.parse(window.end);

  it("shows the day ending at the playhead's hour", () => {
    const playhead = Date.parse("2026-10-11T12:34:00Z");
    const shown = shownSpan(playhead, window);
    expect(shown.to).toBe(Date.parse("2026-10-11T13:00:00Z"));
    expect(shown.to - shown.from).toBe(FIRE_SPAN_HOURS * 3600_000);
  });

  it("pins a playhead past the window to its end, so a forecast never reads again", () => {
    const a = shownSpan(end + 30 * 3600_000, window);
    const b = shownSpan(end + 90 * 3600_000, window);
    expect(a).toEqual(b);
    expect(a.to).toBe(end + 3600_000);
  });

  it("reads wider than it shows, so the next hours need no request", () => {
    const shown = shownSpan(Date.parse("2026-10-11T12:00:00Z"), window);
    const read = fetchSpan(shown);
    expect(spanCovers(read, shown)).toBe(true);
    expect(spanCovers(read, shownSpan(Date.parse("2026-10-11T20:00:00Z"), window))).toBe(true);
    expect(spanCovers(read, shownSpan(Date.parse("2026-10-12T04:00:00Z"), window))).toBe(false);
  });

  it("builds a feature per detection inside the shown span", () => {
    const parsed = parseFireLine(line("viirs-noaa20", H0, 3));
    const all = fireFeatures([{ sensor: 0, line: parsed }], H0 * 1000, (H0 + HOUR) * 1000);
    expect(all.features).toHaveLength(3);
    expect(all.features[0]!.geometry.coordinates).toEqual([119, 48]);
    expect(all.features[2]!.properties).toMatchObject({ sensor: 0, frp: 3, dn: "D", conf: "n" });
    expect(fireFeatures([{ sensor: 0, line: parsed }], (H0 + 60) * 1000, (H0 + HOUR) * 1000).features).toHaveLength(2);
  });
});

describe("reading a sensor's hours", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function answer(status: number, body: Uint8Array) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => null },
      arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength),
    };
  }

  function loaded(): { loaded: LoadedFireIndex; files: Record<string, Uint8Array> } {
    const { index, files } = product();
    return {
      loaded: {
        pointer: parseFirePointer({
          schemaVersion: 1,
          product: "fire",
          issued: index.issued,
          path: "fire.202610120500/index.json",
          byteLength: 1,
          crc32: "1c0ffee5",
        }),
        index: parseFireIndex(index),
        indexUrl: "https://data.test/fire.202610120500/index.json?v=1c0ffee5",
      },
      files,
    };
  }

  it("picks one sensor's overlapping rows as one span", () => {
    const { loaded: product } = loaded();
    const span = sensorSpan(product.index, 0, (H0 + 30 * 60) * 1000, (H0 + 2 * HOUR) * 1000)!;
    expect(span.rows.map((row) => row.hour / 1000)).toEqual([H0, H0 + HOUR]);
    expect(span.offset).toBe(0);
    expect(span.length).toBe(span.rows[1]!.offset + span.rows[1]!.length);
    expect(sensorSpan(product.index, 1, H0 * 1000, (H0 + HOUR) * 1000)).toBeNull();
  });

  it("asks for that span once, and splits it at the index's offsets", async () => {
    const { loaded: product, files } = loaded();
    const file = files["viirs-noaa20.jsonl"]!;
    const calls: { url: string; range: string | undefined }[] = [];
    vi.stubGlobal("fetch", (url: string | URL, init?: RequestInit) => {
      const range = (init?.headers as Record<string, string> | undefined)?.Range;
      calls.push({ url: String(url), range });
      const [first, last] = range!.replace("bytes=", "").split("-").map(Number) as [number, number];
      return Promise.resolve(answer(206, file.subarray(first, last + 1)));
    });
    const lines = await fetchFireLines(product, 0, H0 * 1000, (H0 + 4 * HOUR) * 1000);
    expect(lines.map((item) => item.time.length)).toEqual([2, 3, 1]);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("https://data.test/fire.202610120500/viirs-noaa20.jsonl?v=0123abcd");
    expect(calls[0]!.range).toBe(`bytes=0-${file.length - 2}`);
  });

  it("slices a whole-file answer at the same offsets", async () => {
    const { loaded: product, files } = loaded();
    vi.stubGlobal("fetch", () => Promise.resolve(answer(200, files["viirs-noaa20.jsonl"]!)));
    const lines = await fetchFireLines(product, 0, (H0 + HOUR) * 1000, (H0 + 2 * HOUR) * 1000);
    expect(lines).toHaveLength(1);
    expect(lines[0]!.hour).toBe(H0 + HOUR);
  });

  it("refuses a line that is not the hour its row names", () => {
    const { loaded: product, files } = loaded();
    const span = sensorSpan(product.index, 0, H0 * 1000, (H0 + HOUR) * 1000)!;
    const shifted = span.rows.map((row) => ({ ...row, hour: row.hour + 3600_000 }));
    expect(() => splitLines(files["viirs-noaa20.jsonl"]!, shifted, "viirs-noaa20")).toThrow(/hour/);
  });
});

describe("the ?stations=fire switch", () => {
  it("reads and writes fire beside the stations", () => {
    expect(parseStationsFromSearch("?stations=fire").fire).toBe(true);
    expect(parseStationsFromSearch("?stations=FIRMS,syn")).toEqual({
      soundings: false,
      airports: false,
      synop: true,
      fire: true,
    });
    expect(
      searchWithStations("?type=temp", { soundings: false, airports: false, synop: true, fire: true }),
    ).toBe("?type=temp&stations=syn%2Cfire");
  });
});

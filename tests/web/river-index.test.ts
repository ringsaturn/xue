import { describe, expect, it, vi } from "vitest";

import { fetchRiverIndex, parseGaugeLine, parseRiverIndex, RiverIndexError, riverFileUrl } from "../../web/src/river/index";
import { riverBlock } from "../../web/src/showcase-catalog";

/** The Guadalupe index's shape (docs/river.md §2), trimmed. */
function networkIndex(): Record<string, unknown> {
  return {
    version: 1,
    kind: "network",
    geometry: { path: "reaches.parquet", byteLength: 1114663, crc32: "de943f65", featureCount: 10940, rowGroupRows: 4096 },
    tracks: [
      { id: "analysis", kind: "analysis", runTime: "2025-07-03T00:00:00Z", store: { path: "analysis.zarr", byteLength: 286352, crc32: "499f2cb1" } },
      {
        id: "short_range-2025070409",
        kind: "forecast",
        runTime: "2025-07-04T09:00:00Z",
        store: { path: "short_range-2025070409.zarr", byteLength: 173890, crc32: "3cfe6713" },
      },
    ],
    gauges: [
      {
        id: "08165500",
        name: "Guadalupe River at Hunt, TX",
        lon: -99.321712,
        lat: 30.069931,
        source: "USGS NWIS",
        unit: "m3/s",
        feature: 5366,
        series: { path: "gauges.jsonl", offset: 0, length: 30080 },
        peaks: [{ time: "2025-07-04T10:05:00Z", value: 8919.8, note: "USGS, estimated" }],
      },
    ],
    extent: [],
    credits: ["NOAA National Water Model v3.0"],
    disclaimer: "National Water Model output is a simulation.",
  };
}

/** The Valencia index's shape: gauges with sample cells, an outflow line,
 * a stage peak, and an extent. */
function gridIndex(): Record<string, unknown> {
  return {
    version: 1,
    kind: "grid",
    gauges: [
      {
        id: "forata",
        name: "Rio Magro at Forata reservoir",
        lon: -0.745,
        lat: 39.345,
        source: "CHJ SAIH",
        unit: "m3/s",
        sample: [-0.725, 39.35833],
        series: { path: "gauges.jsonl", offset: 2776, length: 16438 },
        outflow: { path: "gauges.jsonl", offset: 19215, length: 16767 },
        peaks: [
          { time: "2024-10-29T17:55:00Z", value: 2016.71, note: "peak inflow (CHJ SAIH)" },
          { value: 5.465, unit: "m", note: "last stage" },
        ],
      },
    ],
    extent: [
      {
        path: "extent/aoi03-horta-sud.geojson",
        byteLength: 1498250,
        crc32: "771ae218",
        at: "2024-10-31T10:22:00Z",
        credit: "Copernicus Emergency Management Service (© 2024 European Union), [EMSR773]",
      },
    ],
    credits: ["Contains modified Copernicus Emergency Management Service information 2024"],
  };
}

function refused(mutate: (index: Record<string, unknown>) => void, base = networkIndex): () => unknown {
  const index = structuredClone(base());
  mutate(index);
  return () => parseRiverIndex(index);
}

const gauge0 = (index: Record<string, unknown>) => (index.gauges as Record<string, unknown>[])[0]!;
const track0 = (index: Record<string, unknown>) => (index.tracks as Record<string, unknown>[])[0]!;

describe("parseRiverIndex", () => {
  it("reads a network index", () => {
    const index = parseRiverIndex(networkIndex());
    expect(index.kind).toBe("network");
    expect(index.geometry).toMatchObject({ featureCount: 10940, rowGroupRows: 4096 });
    expect(index.tracks.map((track) => track.id)).toEqual(["analysis", "short_range-2025070409"]);
    expect(index.tracks[1]!.runTimeMs).toBe(Date.parse("2025-07-04T09:00:00Z"));
    expect(index.gauges[0]!.feature).toBe(5366);
    expect(index.gauges[0]!.peaks[0]!.timeMs).toBe(Date.parse("2025-07-04T10:05:00Z"));
    expect(index.extent).toEqual([]);
    expect(index.disclaimer).toMatch(/simulation/);
  });

  it("reads a grid index", () => {
    const index = parseRiverIndex(gridIndex());
    expect(index.kind).toBe("grid");
    expect(index.geometry).toBeUndefined();
    expect(index.tracks).toEqual([]);
    expect(index.gauges[0]!.sample).toEqual([-0.725, 39.35833]);
    expect(index.gauges[0]!.outflow?.offset).toBe(19215);
    expect(index.gauges[0]!.peaks[1]).toEqual({ value: 5.465, unit: "m", note: "last stage" });
    expect(index.extent[0]!.atMs).toBe(Date.parse("2024-10-31T10:22:00Z"));
  });

  it("defaults the optional lists", () => {
    const index = networkIndex();
    delete index.gauges;
    delete index.extent;
    delete index.disclaimer;
    const parsed = parseRiverIndex(index);
    expect(parsed.gauges).toEqual([]);
    expect(parsed.extent).toEqual([]);
    expect(parsed.disclaimer).toBeUndefined();
  });

  const rules: [string, () => unknown][] = [
    ["a document that is not an object", () => parseRiverIndex([])],
    ["another version", refused((index) => (index.version = 2))],
    ["an unknown kind", refused((index) => (index.kind = "mesh"))],
    ["a key outside the index's", refused((index) => (index.extra = true))],
    ["a network index without geometry", refused((index) => delete index.geometry)],
    ["a network index without tracks", refused((index) => delete index.tracks)],
    ["a grid index with tracks", refused((index) => (index.tracks = []), gridIndex)],
    ["a grid index with geometry", refused((index) => (index.geometry = networkIndex().geometry), gridIndex)],
    ["geometry that is not a .parquet file", refused((index) => ((index.geometry as Record<string, unknown>).path = "reaches.csv"))],
    ["geometry with a key missing", refused((index) => delete (index.geometry as Record<string, unknown>).rowGroupRows)],
    ["a zero feature count", refused((index) => ((index.geometry as Record<string, unknown>).featureCount = 0))],
    ["an uppercase CRC", refused((index) => ((index.geometry as Record<string, unknown>).crc32 = "DE943F65"))],
    ["an empty track list", refused((index) => (index.tracks = []))],
    ["a duplicate track id", refused((index) => ((index.tracks as Record<string, unknown>[])[1]!.id = "analysis"))],
    ["a track id that is not a slug", refused((index) => (track0(index).id = "Analysis"))],
    ["a track kind outside analysis and forecast", refused((index) => (track0(index).kind = "reanalysis"))],
    ["a run time without Z", refused((index) => (track0(index).runTime = "2025-07-03T00:00:00+00:00"))],
    ["a store that is not a .zarr", refused((index) => ((track0(index).store as Record<string, unknown>).path = "analysis.n5"))],
    ["a store path climbing out", refused((index) => ((track0(index).store as Record<string, unknown>).path = "../analysis.zarr"))],
    ["an absolute store path", refused((index) => ((track0(index).store as Record<string, unknown>).path = "/analysis.zarr"))],
    ["a store path with a scheme", refused((index) => ((track0(index).store as Record<string, unknown>).path = "https://x/a.zarr"))],
    ["a store path with a . component", refused((index) => ((track0(index).store as Record<string, unknown>).path = "./analysis.zarr"))],
    ["a gauge key outside the gauge's", refused((index) => (gauge0(index).river = "Guadalupe"))],
    ["a duplicate gauge id", refused((index) => (index.gauges = [gauge0(index), gauge0(index)]))],
    ["a gauge without a name", refused((index) => (gauge0(index).name = " "))],
    ["a gauge longitude off the globe", refused((index) => (gauge0(index).lon = 181))],
    ["a gauge unit that is not a symbol", refused((index) => (gauge0(index).unit = "cubic metres"))],
    ["a network gauge without its feature", refused((index) => delete gauge0(index).feature)],
    ["a feature past the last row", refused((index) => (gauge0(index).feature = 10940))],
    ["a grid gauge with a feature", refused((index) => (gauge0(index).feature = 3), gridIndex)],
    ["a sample that is not a pair", refused((index) => (gauge0(index).sample = [-0.7]), gridIndex)],
    ["a series in another file", refused((index) => ((gauge0(index).series as Record<string, unknown>).path = "other.jsonl"))],
    ["a series with a key missing", refused((index) => delete (gauge0(index).series as Record<string, unknown>).length)],
    ["an outflow with a negative offset", refused((index) => ((gauge0(index).outflow as Record<string, unknown>).offset = -1), gridIndex)],
    ["a peak without a value", refused((index) => (gauge0(index).peaks = [{ note: "x" }]))],
    ["a peak with a stray key", refused((index) => (gauge0(index).peaks = [{ value: 1, source: "x" }]))],
    ["a peak time that is not UTC", refused((index) => (gauge0(index).peaks = [{ value: 1, time: "2025-07-04 10:05" }]))],
    ["an extent that is not GeoJSON", refused((index) => ((index.extent as Record<string, unknown>[])[0]!.path = "extent/a.shp"), gridIndex)],
    ["an extent without its credit", refused((index) => delete (index.extent as Record<string, unknown>[])[0]!.credit, gridIndex)],
    ["an extent time without Z", refused((index) => ((index.extent as Record<string, unknown>[])[0]!.at = "2024-10-31"), gridIndex)],
    ["credits that are not strings", refused((index) => (index.credits = ["ok", 3]))],
    ["an empty disclaimer", refused((index) => (index.disclaimer = ""))],
    [
      "a grid index with neither gauges nor extents",
      refused((index) => {
        index.gauges = [];
        index.extent = [];
      }, gridIndex),
    ],
  ];
  for (const [rule, read] of rules) {
    it(`refuses ${rule}`, () => {
      expect(read).toThrow(RiverIndexError);
    });
  }
});

describe("parseGaugeLine", () => {
  const encode = (value: unknown) => new TextEncoder().encode(JSON.stringify(value));

  it("reads the times and values, nulls as gaps, the stage passed over", () => {
    const line = parseGaugeLine(
      encode({ id: "08165500", time: ["2025-07-02T00:00:00Z", "2025-07-02T00:05:00Z"], value: [0.142, null], stage: [7.56, 7.57], stageUnit: "ft" }),
      "08165500",
    );
    expect(line.times).toEqual([Date.parse("2025-07-02T00:00:00Z"), Date.parse("2025-07-02T00:05:00Z")]);
    expect(line.values).toEqual([0.142, null]);
  });

  it("refuses another gauge's line, unequal lists and times out of order", () => {
    const times = ["2025-07-02T00:05:00Z", "2025-07-02T00:00:00Z"];
    expect(() => parseGaugeLine(encode({ id: "x", time: times.slice(0, 1), value: [1] }), "y")).toThrow(RiverIndexError);
    expect(() => parseGaugeLine(encode({ id: "x", time: times.slice(0, 1), value: [1, 2] }), "x")).toThrow(RiverIndexError);
    expect(() => parseGaugeLine(encode({ id: "x", time: times, value: [1, 2] }), "x")).toThrow(/increasing/);
    expect(() => parseGaugeLine(new TextEncoder().encode("{\"id\":"), "x")).toThrow(RiverIndexError);
  });
});

describe("the catalog's river block", () => {
  it("names the index from the data root under its crc32", () => {
    expect(riverBlock({ index: "river/index.json", byteLength: 1784, crc32: "40af8390" }, "guadalupe-2025")).toEqual({
      indexPath: "showcase/guadalupe-2025/river/index.json",
      crc32: "40af8390",
      byteLength: 1784,
    });
    expect(riverBlock(undefined, "x")).toBeUndefined();
  });

  it("carries the bundle that colours the reaches", () => {
    expect(riverBlock({ index: "river/index.json", byteLength: 1784, crc32: "40af8390", color: "dis" }, "x")?.color).toBe("dis");
    expect(riverBlock({ index: "river/index.json", byteLength: 1784, crc32: "40af8390" }, "x")).not.toHaveProperty("color");
  });

  it("drops a block it cannot read with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(riverBlock({ index: "../river/index.json", byteLength: 1, crc32: "40af8390" }, "x")).toBeUndefined();
    expect(riverBlock({ index: "river/index.json", byteLength: 1, crc32: "nope" }, "x")).toBeUndefined();
    expect(riverBlock({ index: "river/index.json", crc32: "40af8390" }, "x")).toBeUndefined();
    expect(riverBlock({ index: "river/index.json", byteLength: 1, crc32: "40af8390", color: "../dis" }, "x")).toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(4);
    warn.mockRestore();
  });
});

describe("fetchRiverIndex", () => {
  const block = { indexPath: "showcase/guadalupe-2025/river/index.json", crc32: "40af8390", byteLength: 1784 };

  it("asks for the index under its crc32 and resolves paths beside it", async () => {
    const asked: string[] = [];
    const loaded = await fetchRiverIndex(
      "data/",
      block,
      async (url) => {
        asked.push(url);
        return new Response(JSON.stringify(networkIndex()));
      },
      "http://localhost/",
    );
    expect(asked).toEqual(["http://localhost/data/showcase/guadalupe-2025/river/index.json?v=40af8390"]);
    expect(loaded?.index.kind).toBe("network");
    expect(riverFileUrl(loaded!.indexUrl, "analysis.zarr/zarr.json", "499f2cb1")).toBe(
      "http://localhost/data/showcase/guadalupe-2025/river/analysis.zarr/zarr.json?v=499f2cb1",
    );
  });

  it("turns a refused index or a failed request into a warning and null", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const bad = { ...networkIndex(), version: 9 };
    expect(await fetchRiverIndex("data/", block, async () => new Response(JSON.stringify(bad)), "http://localhost/")).toBeNull();
    expect(await fetchRiverIndex("data/", block, async () => new Response("", { status: 404 }), "http://localhost/")).toBeNull();
    expect(warn).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });
});

import { describe, expect, it } from "vitest";

import {
  archiveBounds,
  forecastsAt,
  isArchiveStormId,
  issueTimes,
  seasonOfStorm,
  sourcesOf,
  stormAt,
  stormCode,
  storyTimes,
  trackErrors,
  truthsOf,
  validateArchiveCatalog,
  validateSeasonIndex,
  type ArchiveRecord,
  type BestRecord,
  type StormErrors,
} from "../../web/src/tc/archive";

const REF = { byteLength: 10, crc32: "0123abcd" };

function point(time: string, lead: number | undefined, lat: number, lon: number) {
  return { time, ...(lead === undefined ? {} : { lead }), lat, lon, vmax: 20, pmin: 990, radii: null, class: null, rmw: null, gust: null, cone: null };
}

function record(source: string, kind: ArchiveRecord["kind"], base: string, lat = 15): ArchiveRecord {
  const forecast =
    kind === "ensemble"
      ? { issued: base, base, leads: [0], members: [1], lat: [lat * 100], lon: [14000], vmax: [200], pmin: [9900], mean: null }
      : { issued: base, base, points: [point(base, 0, lat, 140)] };
  return { storm: "WP062023", basin: "WP", name: null, source, kind, origin: "live", forecast };
}

describe("tc archive documents", () => {
  it("reads a catalog, scores optional, and refuses a newer schema", () => {
    const catalog = validateArchiveCatalog({
      archiveSchemaVersion: 1,
      product: "tc",
      months: [{ month: "2023-08", path: "2023/08/index.json", storms: 9, records: 936, ...REF }],
      seasons: [{ season: 2023, path: "2023/index.json", storms: 83, ...REF }],
    });
    expect(catalog.months[0]!.month).toBe("2023-08");
    expect(catalog.scores).toEqual([]);
    expect(() => validateArchiveCatalog({ archiveSchemaVersion: 2, months: [], seasons: [] })).toThrow(/newer/);
    expect(() =>
      validateArchiveCatalog({ archiveSchemaVersion: 1, months: [{ month: "2023-08", path: "../x", ...REF }], seasons: [] }),
    ).toThrow(/relative/);
  });

  it("reads a season index written before the summary fields", () => {
    const index = validateSeasonIndex({
      archiveSchemaVersion: 1,
      season: 2023,
      best: { path: "best.jsonl", ...REF },
      storms: [
        { id: "WP062023", basin: "WP", name: "KHANUN", sid: null, atcf: ["WP062023"], offset: 0, length: 10, best: ["usa"], months: ["2023-07"] },
        { id: "WP072023", basin: "WP", name: null, sid: null, atcf: ["WP072023"], offset: null, length: null, best: [], months: [], first: "2023-08-01T00:00:00Z", vmax: 30, sources: ["gfs"] },
      ],
    });
    expect(index.storms[0]).toMatchObject({ first: null, last: null, vmax: null, sources: [] });
    expect(index.storms[1]).toMatchObject({ offset: null, first: "2023-08-01T00:00:00Z", vmax: 30, sources: ["gfs"] });
    expect(() =>
      validateSeasonIndex({ archiveSchemaVersion: 1, season: 2023, best: { path: "best.jsonl", ...REF }, storms: [{ id: "x-wp-1" }] }),
    ).toThrow(/ATCF/);
  });

  it("names storms", () => {
    expect(isArchiveStormId("WP062023")).toBe(true);
    expect(isArchiveStormId("wp062023")).toBe(false);
    expect(isArchiveStormId(null)).toBe(false);
    expect(seasonOfStorm("SH022027")).toBe(2027);
    expect(stormCode("WP062023")).toBe("WP06");
  });
});

describe("a storm at one issue time", () => {
  const records = [
    record("jtwc", "agency", "2023-08-01T00:00:00Z"),
    record("gfs", "model", "2023-07-31T18:00:00Z", 14),
    record("gfs", "model", "2023-08-01T00:00:00Z", 15),
    record("ecmwf", "model", "2023-07-31T12:00:00Z"),
    record("ecmwfens", "ensemble", "2023-07-31T18:00:00Z"),
    record("gefs", "ensemble", "2023-08-01T06:00:00Z"),
  ];

  it("lists the issue times once each, in order", () => {
    expect(issueTimes(records)).toEqual([
      "2023-07-31T12:00:00Z",
      "2023-07-31T18:00:00Z",
      "2023-08-01T00:00:00Z",
      "2023-08-01T06:00:00Z",
    ]);
  });

  it("takes each source's newest forecast within six hours before the issue", () => {
    const best: BestRecord = {
      storm: "WP062023",
      basin: "WP",
      name: "KHANUN",
      sid: "2023207N12139",
      atcf: ["WP062023"],
      best: { usa: { source: "ibtracs", provisional: false, points: [point("2023-08-01T00:00:00Z", undefined, 15, 140)] } },
    };
    const storm = stormAt("WP062023", "KHANUN", records, best, "2023-08-01T00:00:00Z");
    expect(Object.keys(storm.agencies)).toEqual(["jtwc"]);
    expect(Object.keys(storm.models).sort()).toEqual(["ecmwfens", "gfs"]);
    expect((storm.models.gfs as { points: { lat: number }[] }).points[0]!.lat).toBe(15);
    expect(storm.best.usa!.points).toHaveLength(1);
    expect(storm.sid).toBe("2023207N12139");
    expect(stormAt("WP062023", null, records, null, "2023-07-31T12:00:00Z").models).toHaveProperty("ecmwf");
  });

  it("follows the storm past its last forecast, on the best track's synoptic times", () => {
    const best: BestRecord = {
      storm: "WP062023",
      basin: "WP",
      name: null,
      sid: null,
      atcf: [],
      best: {
        usa: {
          source: "ibtracs",
          provisional: false,
          points: [
            point("2023-08-01T00:00:00Z", undefined, 15, 140),
            point("2023-08-01T03:00:00Z", undefined, 15, 140),
            point("2023-08-02T12:00:00Z", undefined, 18, 138),
          ],
        },
      },
    };
    expect(storyTimes(records, best)).toEqual([
      "2023-07-31T12:00:00Z",
      "2023-07-31T18:00:00Z",
      "2023-08-01T00:00:00Z",
      "2023-08-01T06:00:00Z",
      "2023-08-02T12:00:00Z",
    ]);
    expect(forecastsAt(records, "2023-08-01T06:00:00Z")).toBe(true);
    expect(forecastsAt(records, "2023-08-02T12:00:00Z")).toBe(false);
  });

  it("orders the sources agencies first, by the registry", () => {
    expect(sourcesOf(records, ["nhc", "jtwc", "gfs", "gefs", "ecmwf", "ecmwfens"])).toEqual({
      agencies: ["jtwc"],
      models: ["gfs", "gefs", "ecmwf", "ecmwfens"],
    });
  });
});

describe("track errors", () => {
  const errors: StormErrors = {
    storm: "WP062023",
    truths: {
      jma: { gfs: { base: ["2023-08-01T00:00:00Z"], lead: [24], track: [80], along: [null], cross: [null], vmax: [null], pmin: [null] } },
      usa: {
        gfs: {
          base: ["2023-08-01T00:00:00Z", "2023-08-01T00:00:00Z", "2023-08-01T06:00:00Z"],
          lead: [24, 48, 24],
          track: [50, 100, 70],
          along: [null, null, null],
          cross: [null, null, null],
          vmax: [null, null, null],
          pmin: [null, null, null],
        },
        jtwc: { base: ["2023-08-01T06:00:00Z"], lead: [12], track: [30], along: [null], cross: [null], vmax: [null], pmin: [null] },
      },
    },
  };

  it("means the cases at the leads shown, over the storm or for one issue", () => {
    const mean = trackErrors(errors, "usa", [24, 48]);
    expect(mean.get("gfs")!.get(24)).toEqual({ km: 60, n: 2 });
    expect(mean.get("gfs")!.get(48)).toEqual({ km: 100, n: 1 });
    expect(mean.has("jtwc")).toBe(false);
    const one = trackErrors(errors, "usa", [24, 48], "2023-08-01T06:00:00Z");
    expect(one.get("gfs")!.get(24)).toEqual({ km: 70, n: 1 });
    expect(one.get("gfs")!.has(48)).toBe(false);
    expect(trackErrors(null, "usa", [24]).size).toBe(0);
  });

  it("puts the US truth first", () => {
    expect(truthsOf(errors)).toEqual(["usa", "jma"]);
    expect(truthsOf(null)).toEqual([]);
  });
});

describe("framing", () => {
  it("unwraps a storm across the antimeridian into one box", () => {
    const best: BestRecord = {
      storm: "CP032026",
      basin: "CP",
      name: null,
      sid: null,
      atcf: [],
      best: {
        usa: {
          source: "ibtracs",
          provisional: false,
          points: [point("2026-10-20T00:00:00Z", undefined, 15, 178), point("2026-10-21T00:00:00Z", undefined, 18, -176)],
        },
      },
    };
    const [[west, south], [east, north]] = archiveBounds(best, [])!;
    expect([west, south, east, north]).toEqual([178, 15, 184, 18]);
    expect(archiveBounds(null, [])).toBeNull();
    const windy: BestRecord = {
      ...best,
      best: { usa: { source: "ibtracs", provisional: false, points: [{ ...point("2026-10-20T00:00:00Z", undefined, 0, 150), radii: { "34": [111.195, null, 0, 50] } }] } },
    };
    const [[w, s], [e, n]] = archiveBounds(windy, [])!;
    expect([w, s, e, n].map((value) => Math.round(value * 1000) / 1000)).toEqual([149, -1, 151, 1]);
    expect(archiveBounds(null, [record("gfs", "model", "2023-08-01T00:00:00Z")])).toEqual([
      [140, 15],
      [140, 15],
    ]);
  });
});

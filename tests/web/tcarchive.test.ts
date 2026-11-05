import { describe, expect, it } from "vitest";

import {
  archiveBounds,
  forecastsAt,
  isArchiveStormId,
  issueTimes,
  seasonHasForecasts,
  seasonQuality,
  seasonOfStorm,
  sourcesOf,
  stormAt,
  stormCode,
  storyTimes,
  trackErrors,
  truthsOf,
  validateArchiveCatalog,
  validateBest,
  validateMonthIndex,
  validateRecord,
  validateScores,
  validateSeasonIndex,
  validateSeasonOverview,
  validateStormErrors,
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
      validateArchiveCatalog({
        archiveSchemaVersion: 1,
        product: "tc",
        months: [{ month: "2023-08", path: "../x", storms: 1, records: 1, ...REF }],
        seasons: [],
      }),
    ).toThrow(/catalog\.months\[0\]\.path is malformed/);
    expect(() =>
      validateArchiveCatalog({
        archiveSchemaVersion: 1,
        product: "tc",
        months: [],
        seasons: [],
        scores: [{ season: 2023, path: "2024/scores.json", from: { season: "0123abcd", months: {} }, ...REF }],
      }),
    ).toThrow(/does not name its season/);
  });

  it("reads a season index written before the summary fields", () => {
    const index = validateSeasonIndex({
      archiveSchemaVersion: 1,
      product: "tc",
      season: 2023,
      best: { path: "best.jsonl", ...REF },
      storms: [
        { id: "WP062023", basin: "WP", name: "KHANUN", sid: null, atcf: ["WP062023"], offset: 0, length: 10, best: ["usa"], months: ["2023-07"] },
        { id: "WP072023", basin: "WP", name: null, sid: null, atcf: ["WP072023"], offset: null, length: null, best: [], months: [], first: "2023-08-01T00:00:00Z", vmax: 30, sources: ["gfs"] },
      ],
    });
    expect(index.storms[0]).toMatchObject({ first: null, last: null, vmax: null, sources: [] });
    expect(index.storms[1]).toMatchObject({ offset: null, first: "2023-08-01T00:00:00Z", vmax: 30, sources: ["gfs"] });
    const season = (storm: Record<string, unknown>) => ({
      archiveSchemaVersion: 1,
      product: "tc",
      season: 2023,
      best: { path: "best.jsonl", ...REF },
      storms: [{ id: "WP062023", basin: "WP", name: null, sid: null, atcf: [], offset: 0, length: 10, best: [], months: [], ...storm }],
    });
    expect(() => validateSeasonIndex(season({ id: "x-wp-1" }))).toThrow(/storms\[0\]\.id is malformed/);
    expect(() => validateSeasonIndex(season({ length: null }))).toThrow(/whole byte span or none/);
    expect(() => validateSeasonIndex(season({ id: "WP062024" }))).toThrow(/not a storm of season 2023/);
    expect(validateSeasonIndex(season({ id: "SH-1951123S10140-2023", offset: null, length: null })).storms[0]!.offset).toBeNull();
  });

  it("reads a month index and refuses a storm listed twice", () => {
    const storm = { id: "WP062023", basin: "WP", name: null, offset: 0, length: 10, records: 2, sources: ["gfs"] };
    const month = (storms: unknown[]) => ({ archiveSchemaVersion: 1, product: "tc", month: "2023-08", forecasts: { path: "forecasts.jsonl", ...REF }, storms });
    expect(validateMonthIndex(month([storm])).storms).toEqual([{ id: "WP062023", offset: 0, length: 10 }]);
    expect(() => validateMonthIndex(month([storm, storm]))).toThrow(/twice/);
    expect(() => validateMonthIndex({ ...month([]), archiveSchemaVersion: 2 })).toThrow(/newer/);
  });

  it("reads a season overview and refuses a class past H5", () => {
    const overview = (track: unknown) => ({ archiveSchemaVersion: 1, product: "tc", season: 2023, storms: [{ id: "WP062023", track }] });
    expect(validateSeasonOverview(overview([[1400, 150, 2]])).storms[0]!.track).toEqual([[1400, 150, 2]]);
    expect(() => validateSeasonOverview(overview([[1400, 150, 7]]))).toThrow(/must be at most 6/);
  });

  it("reads forecast and best-track lines", () => {
    expect(validateRecord(record("gefs", "ensemble", "2023-08-01T00:00:00Z")).kind).toBe("ensemble");
    expect(() => validateRecord({ ...record("gfs", "model", "2023-08-01T00:00:00Z"), kind: "ensemble" })).toThrow(/wrong forecast shape/);
    expect(() => validateRecord({ ...record("gfs", "model", "2023-08-01T00:00:00Z"), storm: "WP-1951123N10140-1951" })).toThrow(
      /storm is malformed/,
    );
    const best = {
      storm: "SH-1951123S10140-1952",
      basin: "SH",
      name: null,
      sid: "1951123S10140",
      atcf: [],
      best: { ibtracs: { source: "ibtracs", provisional: false, points: [point("1951-05-03T00:00:00Z", undefined, -10, 140)] } },
    };
    expect(validateBest(best).best.ibtracs!.points).toHaveLength(1);
    expect(() => validateBest({ ...best, atcf: ["SH-1951123S10140-1952"] })).toThrow(/atcf\[0\] is malformed/);
  });

  it("reads scores and an errors line", () => {
    const scores = {
      scoreSchemaVersion: 1,
      product: "tc",
      season: 2023,
      from: { season: "0123abcd", months: { "2023-08": "0123abcd" } },
      leads: [0, 24],
      errors: { path: "errors.jsonl", ...REF },
      storms: [{ id: "WP062023", offset: 0, length: 10 }],
      summary: [],
      homogeneous: [
        { basin: "WP", truth: "usa", reference: "jtwc", source: "gfs", lead: 24, n: 1, track: 80, referenceTrack: 0, skill: null },
      ],
    };
    expect(validateScores(scores).storms).toEqual([{ id: "WP062023", offset: 0, length: 10 }]);
    expect(() => validateScores({ ...scores, scoreSchemaVersion: 2 })).toThrow(/newer/);
    expect(() => validateScores({ ...scores, errors: { path: "other.jsonl", ...REF } })).toThrow(/errors\.path/);
    const columns = { base: ["2023-08-01T00:00:00Z"], lead: [24], track: [80], along: [null], cross: [null], vmax: [1], pmin: [null] };
    const line = { storm: "WP062023", basin: "WP", truths: { usa: { gfs: columns } } };
    expect(validateStormErrors(line).truths.usa!.gfs!.track).toEqual([80]);
    expect(() => validateStormErrors({ ...line, truths: { usa: { gfs: { ...columns, lead: [24, 48] } } } })).toThrow(/differ in length/);
  });

  it("says what an early season's record lacks", () => {
    const storms = [{ vmax: null }, { vmax: 30 }, { vmax: null }];
    expect(seasonQuality(1980, storms)).toBeNull();
    expect(seasonQuality(1979, storms)).toEqual({ count: 3, missing: 2, duplicates: false });
    expect(seasonQuality(1950, storms)?.duplicates).toBe(false);
    expect(seasonQuality(1949, storms)?.duplicates).toBe(true);
  });

  it("reads a reconstructed storm", () => {
    expect(isArchiveStormId("WP-COBRA-1944")).toBe(true);
    expect(isArchiveStormId("WP-cobra-1944")).toBe(false);
    expect(stormCode("WP-COBRA-1944")).toBe("WP");
    expect(seasonOfStorm("WP-COBRA-1944")).toBe(1944);
    const point = (time: string) => ({ time, lat: 15, lon: 128 });
    const best = { storm: "WP-COBRA-1944", basin: "WP", name: "COBRA", sid: null, atcf: [], best: { curated: { source: "curated", provisional: false, points: [point("1944-12-18T04:30:00Z"), point("1944-12-18T12:30:00Z")] } } };
    expect(validateBest(best).provenance).toEqual([]);
    // Its own times, none synoptic: without them nothing could be drawn.
    expect(storyTimes([], validateBest(best))).toEqual(["1944-12-18T04:30:00Z", "1944-12-18T12:30:00Z"]);
    // Beside an agency's track its points stay reachable though some are synoptic.
    const beside = { ...best, best: { curated: { source: "curated", provisional: false, points: [point("1945-06-04T18:00:00Z"), point("1945-06-04T19:40:00Z")] }, usa: { source: "usa", provisional: false, points: [point("1945-06-04T18:00:00Z"), point("1945-06-04T21:00:00Z"), point("1945-06-05T00:00:00Z")] } } };
    expect(storyTimes([], validateBest(beside))).toEqual(["1945-06-04T18:00:00Z", "1945-06-04T19:40:00Z", "1945-06-05T00:00:00Z"]);
    const source = { publisher: "NOAA", title: "Maps", url: "https://example.com", use: "wind" };
    expect(validateBest({ ...best, provenance: [source] }).provenance).toEqual([source]);
    expect(() => validateBest({ ...best, provenance: [{ ...source, url: "http://example.com" }] })).toThrow();
  });

  it("names storms", () => {
    expect(isArchiveStormId("WP062023")).toBe(true);
    expect(isArchiveStormId("wp062023")).toBe(false);
    expect(isArchiveStormId(null)).toBe(false);
    expect(isArchiveStormId("WP-1951123N10140-1951")).toBe(true);
    expect(isArchiveStormId("WP-1951123X10140-1951")).toBe(false);
    expect(seasonOfStorm("SH-1951123S10140-1952")).toBe(1952);
    expect(stormCode("WP-1951123N10140-1951")).toBe("WP");
    expect(seasonHasForecasts({ storms: [] })).toBe(false);
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

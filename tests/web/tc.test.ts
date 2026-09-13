import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/tc-registry.json";
import expectedIndex from "../fixtures/tc/expected/index.json";
import expectedPointer from "../fixtures/tc/expected/latest-tc.json";
import expectedStorm from "../fixtures/tc/expected/EP142026.json";

import { TC_AGENCIES, TC_BASINS, TC_MODELS } from "../../web/src/tc/agencies";
import {
  ensembleMembers,
  isTcEnsemble,
  validateTcIndex,
  validateTcPointer,
  validateTcStorm,
} from "../../web/src/tc/schema";
import {
  destination,
  ensembleLines,
  forecastLine,
  modelTracks,
  positionAt,
  radiiRing,
  resolveTcStormId,
  stormBounds,
  unwrapLongitudes,
} from "../../web/src/tc/tracks";
import { parseTcFromSearch, searchWithTc } from "../../web/src/urlstate";

interface Registry {
  agencies: {
    id: string;
    name: string;
    color: string;
    basins: string[];
    scale: string;
  }[];
  models: { id: string; name: string; ensemble: boolean }[];
  basins: Record<string, string>;
}

const registry = registryJson as unknown as Registry;

describe("the tropical cyclone registry", () => {
  it("names the agencies the encoder registers, in their colors", () => {
    expect(Object.keys(TC_AGENCIES)).toEqual(
      registry.agencies.map((agency) => agency.id),
    );
    for (const agency of registry.agencies) {
      const info = TC_AGENCIES[agency.id]!;
      expect(info.color).toBe(agency.color);
      expect(info.name).toBe(agency.name);
      expect([...info.basins]).toEqual(agency.basins);
      expect(info.scale).toBe(agency.scale);
    }
  });

  it("names the models the encoder registers", () => {
    expect(Object.keys(TC_MODELS)).toEqual(
      registry.models.map((model) => model.id),
    );
    for (const model of registry.models) {
      expect(TC_MODELS[model.id]!.ensemble).toBe(model.ensemble);
      expect(TC_MODELS[model.id]!.name).toBe(model.name);
    }
    expect(TC_BASINS).toEqual(registry.basins);
  });
});

describe("the product schema", () => {
  it("accepts the committed golden", () => {
    const pointer = validateTcPointer(expectedPointer);
    expect(pointer.path).toBe("tc.2026091206/index.json");
    const index = validateTcIndex(expectedIndex);
    expect(index.storms.map((storm) => storm.id)).toEqual([
      "EP142026",
      "x-ep-2026091012-1",
      "x-wp-2026091200-1",
    ]);
    const storm = validateTcStorm(expectedStorm);
    expect(storm.name).toBe("NORBERT");
    expect(Object.keys(storm.agencies)).toEqual(["jtwc", "nhc"]);
    expect(Object.keys(storm.models)).toEqual([
      "gfs",
      "gefs",
      "ecmwf",
      "ecmwfens",
    ]);
    expect(isTcEnsemble(storm.models.gefs!)).toBe(true);
    expect(isTcEnsemble(storm.models.gfs!)).toBe(false);
  });

  it("rejects a newer schema and malformed shapes, and admits unknown keys", () => {
    expect(() =>
      validateTcPointer({ ...expectedPointer, schemaVersion: 2 }),
    ).toThrow(/schema version/);
    expect(() =>
      validateTcIndex({
        ...expectedIndex,
        storms: [{ ...expectedIndex.storms[0], id: "ep142026" }],
      }),
    ).toThrow(/id/);
    const forecast = expectedStorm.agencies.nhc;
    const broken = {
      ...forecast,
      points: [
        forecast.points[0],
        { ...forecast.points[1], lead: forecast.points[0]!.lead },
      ],
    };
    expect(() =>
      validateTcStorm({ ...expectedStorm, agencies: { nhc: broken } }),
    ).toThrow(/leads/);
    expect(() =>
      validateTcStorm({
        ...expectedStorm,
        agencies: { "Some-Body": forecast },
      }),
    ).toThrow(/malformed key/);
    const admitted = validateTcStorm({
      ...expectedStorm,
      agencies: { somebody: forecast },
    });
    expect(Object.keys(admitted.agencies)).toEqual(["somebody"]);
  });

  it("unpacks an ensemble's fixed-point arrays member by member", () => {
    const storm = validateTcStorm(expectedStorm);
    const gefs = storm.models.gefs!;
    if (!isTcEnsemble(gefs)) throw new Error("gefs is an ensemble");
    const members = ensembleMembers(gefs);
    expect(members.map((member) => member.member)).toEqual([0, 1, 2]);
    const control = members[0]!;
    expect(control.leads[0]).toBe(0);
    expect(control.lat[0]).toBeCloseTo(gefs.lat[0]! / 100, 6);
    expect(control.lat.length).toBe(control.leads.length);
    expect(ensembleLines(gefs).every((line) => line.line.length > 1)).toBe(
      true,
    );
    expect(gefs.mean?.points.length).toBeGreaterThan(0);
    expect(
      modelTracks(storm).map((track) => [track.model, track.fromEnsemble]),
    ).toEqual([
      ["gfs", false],
      ["gefs", true],
      ["ecmwf", false],
    ]);
  });

  it("follows the crosswalk to a renamed storm", () => {
    const index = validateTcIndex({
      ...expectedIndex,
      crosswalk: { "x-ep-2026090100-1": "EP142026" },
    });
    expect(resolveTcStormId(index, "x-ep-2026090100-1")).toBe("EP142026");
    expect(resolveTcStormId(index, "EP142026")).toBe("EP142026");
    expect(resolveTcStormId(index, "EP992026")).toBeNull();
  });
});

describe("walking a track", () => {
  const storm = validateTcStorm(expectedStorm);
  const nhc = storm.agencies.nhc!;

  it("interpolates position, intensity and radii between forecast points", () => {
    const base = Date.parse(nhc.base);
    const a = nhc.points[1]!;
    const b = nhc.points[2]!;
    const midway = (Date.parse(a.time) + Date.parse(b.time)) / 2;
    const position = positionAt(nhc.points, midway)!;
    expect(position.index).toBe(1);
    expect(position.fraction).toBeCloseTo(0.5, 6);
    expect(position.lat).toBeCloseTo((a.lat + b.lat) / 2, 6);
    expect(position.lon).toBeCloseTo((a.lon + b.lon) / 2, 6);
    expect(position.vmax).toBeCloseTo((a.vmax! + b.vmax!) / 2, 6);
    expect(position.radii!["34"]![0]).toBeCloseTo(
      (a.radii!["34"]![0]! + b.radii!["34"]![0]!) / 2,
      6,
    );
    expect(positionAt(nhc.points, base - 1)).toBeNull();
    expect(
      positionAt(
        nhc.points,
        Date.parse(nhc.points[nhc.points.length - 1]!.time) + 1,
      ),
    ).toBeNull();
    const exact = positionAt(nhc.points, Date.parse(a.time))!;
    expect(exact.fraction).toBe(0);
    expect(exact.lat).toBe(a.lat);
  });

  it("keeps a line continuous across the antimeridian", () => {
    expect(
      unwrapLongitudes([
        [178, 10],
        [-179, 11],
        [-176, 12],
      ]),
    ).toEqual([
      [178, 10],
      [181, 11],
      [184, 12],
    ]);
    expect(
      unwrapLongitudes([
        [-178, 10],
        [179, 11],
      ]),
    ).toEqual([
      [-178, 10],
      [-181, 11],
    ]);
    const crossing = positionAt(
      [
        {
          time: "2026-09-12T00:00:00Z",
          lat: 10,
          lon: 179,
          vmax: null,
          pmin: null,
          radii: null,
          class: null,
          rmw: null,
          gust: null,
          cone: null,
          lead: 0,
        },
        {
          time: "2026-09-12T12:00:00Z",
          lat: 10,
          lon: -179,
          vmax: null,
          pmin: null,
          radii: null,
          class: null,
          rmw: null,
          gust: null,
          cone: null,
          lead: 43200,
        },
      ],
      Date.parse("2026-09-12T06:00:00Z"),
    )!;
    expect(crossing.lon).toBeCloseTo(180, 6);
  });

  it("draws the wind radii as four quadrant arcs about the centre", () => {
    const ring = radiiRing(17.2, -126.6, [166.7, 74.1, 55.6, 92.6])!;
    expect(ring.length).toBe(4 * 10 + 1);
    expect(ring[0]).toEqual(ring[ring.length - 1]);
    // The first sample points due north by the north-east radius.
    expect(ring[0]![0]).toBeCloseTo(-126.6, 3);
    expect(ring[0]![1]).toBeGreaterThan(17.2 + 1.4);
    expect(radiiRing(0, 0, [0, 0, 0, 0])).toBeNull();
    // A missing quadrant closes through the centre.
    const partial = radiiRing(0, 0, [100, null, null, null])!;
    expect(partial.filter(([lon, lat]) => lon === 0 && lat === 0).length).toBe(
      3,
    );
    const north = destination(0, 0, 0, 111.195);
    expect(north[1]).toBeCloseTo(1, 3);
    expect(north[0]).toBeCloseTo(0, 6);
  });

  it("frames the storm's lines", () => {
    const bounds = stormBounds(storm)!;
    const [west, south, east, north] = bounds;
    expect(west).toBeLessThan(east);
    expect(south).toBeLessThan(north);
    for (const [lon, lat] of forecastLine(nhc.points)) {
      expect(lon).toBeGreaterThanOrEqual(west);
      expect(lon).toBeLessThanOrEqual(east);
      expect(lat).toBeGreaterThanOrEqual(south);
      expect(lat).toBeLessThanOrEqual(north);
    }
  });
});

describe("?tc= in the URL", () => {
  it("reads a storm, off, and the narrowing parameters", () => {
    expect(parseTcFromSearch("")).toEqual({
      storm: null,
      off: false,
      agencies: null,
      models: null,
      members: false,
    });
    expect(parseTcFromSearch("?tc=ep142026").storm).toBe("EP142026");
    expect(parseTcFromSearch("?tc=X-EP-2026091012-1").storm).toBe(
      "x-ep-2026091012-1",
    );
    expect(parseTcFromSearch("?tc=off").off).toBe(true);
    expect(parseTcFromSearch("?tc=nonsense")).toEqual({
      storm: null,
      off: false,
      agencies: null,
      models: null,
      members: false,
    });
    expect(
      parseTcFromSearch("?tcagency=NHC,jtwc,,bad-key&tcmodel=gfs&tcmembers=on"),
    ).toEqual({
      storm: null,
      off: false,
      agencies: ["nhc", "jtwc"],
      models: ["gfs"],
      members: true,
    });
  });

  it("writes only what differs from the default", () => {
    const base = {
      storm: null,
      off: false,
      agencies: null,
      models: null,
      members: false,
    };
    expect(searchWithTc("?model=gfs&type=precip", base)).toBe(
      "?model=gfs&type=precip",
    );
    expect(searchWithTc("?model=gfs&tc=EP142026&tcmembers=on", base)).toBe(
      "?model=gfs",
    );
    expect(
      searchWithTc("?model=gfs", {
        ...base,
        storm: "EP142026",
        agencies: ["nhc"],
        members: true,
      }),
    ).toBe("?model=gfs&tc=EP142026&tcagency=nhc&tcmembers=on");
    // Off says nothing else: the narrowing is meaningless with nothing drawn.
    expect(
      searchWithTc("?model=gfs", { ...base, off: true, models: ["gfs"] }),
    ).toBe("?model=gfs&tc=off");
  });
});

describe("the point card", () => {
  it("prints the centre's numbers for a forecast point", async () => {
    const { buildTcCard } = await import("../../web/src/tc/card");
    const { pointDataOf } = await import("../../web/src/tc/layers");
    const storm = validateTcStorm(expectedStorm);
    const point = storm.agencies.nhc!.points[1]!;
    const feature = {
      properties: {
        data: JSON.stringify({
          storm: storm.id,
          name: storm.name,
          source: "forecast",
          agency: "nhc",
          code: "NHC",
          time: point.time,
          lead: point.lead,
          number: undefined,
          lat: point.lat,
          lon: point.lon,
          vmax: point.vmax,
          pmin: point.pmin,
          radii: point.radii,
          class: point.class,
          rmw: point.rmw,
          gust: point.gust,
        }),
      },
    };
    const data = pointDataOf(feature)!;
    expect(data.agency).toBe("nhc");
    expect(pointDataOf({ properties: {} })).toBeNull();
    const card = buildTcCard(data, { formatTime: (time) => `at ${time}` });
    expect(card.querySelector(".tc-card-name")?.textContent).toBe("NORBERT");
    expect(card.querySelector(".tc-card-code")?.textContent).toBe("NHC");
    expect(card.querySelector(".tc-card-time")?.textContent).toContain(
      `at ${point.time}`,
    );
    expect(card.querySelector(".tc-card-lead")?.textContent).toBe("+3h");
    expect(card.querySelector(".tc-card-class")?.textContent).toBe("TS");
    // 28.3 m/s is the 55 kt the advisory said.
    expect(card.querySelector(".tc-card-headline")?.textContent).toContain(
      "55 kt · 28 m/s",
    );
    expect(card.querySelector(".tc-card-headline")?.textContent).toContain(
      "996 hPa",
    );
    const rows = [...card.querySelectorAll(".tc-card-radii tbody tr")].map(
      (row) => row.textContent,
    );
    expect(rows).toEqual(["34KT167745693", "50KT560037"]);
    expect(card.querySelector(".tc-card-extra")?.textContent).toBe(
      "GUST 65 kt · 33 m/s",
    );
    expect(card.querySelector(".tc-card-position")?.textContent).toBe(
      "17.2°N 126.6°W",
    );
  });
});

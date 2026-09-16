import { describe, expect, it } from "vitest";

import {
  AIRPORT_RADIUS_KM,
  SOUNDING_RADIUS_KM,
  haversineKm,
  nearestAirport,
  nearestSounding,
} from "../../web/src/stations/nearest";
import type { AirportIndex, SoundingIndex } from "../../web/src/stations/schema";

/** A sounding index carrying only what the lookup reads. The parser builds
 * the full shape; this is what the arithmetic is given. */
function soundingIndex(
  stations: readonly { id: string; lat: number; lon: number }[],
): SoundingIndex {
  return {
    schemaVersion: 1,
    issued: "2026-09-16T00:00:00Z",
    generated: "2026-09-16T00:20:00Z",
    soundings: { path: "soundings.jsonl", byteLength: 1, crc32: "00000000" },
    stations: stations.map((station, index) => ({
      id: station.id,
      wmo: null,
      name: null,
      lat: station.lat,
      lon: station.lon,
      elev: null,
      offset: index,
      length: 1,
      latest: "2026-09-16T00:00:00Z",
      times: ["2026-09-16T00:00:00Z"],
      headline: { t500: null, td500: null, freezingLevel: null, pw: null, levels: 1 },
    })),
    sources: [],
  };
}

function airportIndex(
  stations: readonly { icao: string; lat: number; lon: number }[],
): AirportIndex {
  return {
    schemaVersion: 1,
    issued: "2026-09-16T14:40:00Z",
    generated: "2026-09-16T14:46:00Z",
    history: { path: "history.jsonl", byteLength: 1, crc32: "00000000" },
    stations: stations.map((station, index) => ({
      icao: station.icao,
      lat: station.lat,
      lon: station.lon,
      elev: null,
      obsTime: "2026-09-16T14:30:00Z",
      t: null,
      td: null,
      wd: null,
      ws: null,
      gust: null,
      vis: null,
      qnh: null,
      category: null,
      tafPresent: false,
      offset: index,
      length: 1,
    })),
    sources: [],
  };
}

describe("haversineKm", () => {
  it("is zero at a point and symmetric", () => {
    expect(haversineKm(139.7, 35.7, 139.7, 35.7)).toBe(0);
    expect(haversineKm(139.7, 35.7, 135.5, 34.7)).toBeCloseTo(
      haversineKm(135.5, 34.7, 139.7, 35.7),
      9,
    );
  });

  it("measures a degree of latitude at about 111 km", () => {
    expect(haversineKm(0, 0, 0, 1)).toBeCloseTo(111.19, 1);
  });

  it("measures Tokyo to Osaka at about 400 km", () => {
    // Haneda (RJTT) to Itami (RJOO): the distance every air traveller knows.
    expect(haversineKm(139.781, 35.553, 135.438, 34.785)).toBeCloseTo(404, -1);
  });

  it("takes the short way across the antimeridian", () => {
    // Two points a fifth of a degree apart, either side of 180°.
    expect(haversineKm(179.9, 0, -179.9, 0)).toBeCloseTo(22.24, 1);
    expect(haversineKm(-179.9, 0, 179.9, 0)).toBeCloseTo(22.24, 1);
  });

  it("shrinks a degree of longitude toward the pole", () => {
    expect(haversineKm(0, 60, 1, 60)).toBeCloseTo(haversineKm(0, 0, 1, 0) / 2, 0);
  });
});

describe("nearestSounding", () => {
  // Three stations across the Kanto plain, and one far to the west.
  const index = soundingIndex([
    { id: "0-20000-0-47646", lat: 36.05, lon: 140.13 }, // Tateno
    { id: "0-20000-0-47678", lat: 33.12, lon: 139.78 }, // Hachijojima
    { id: "0-20000-0-47807", lat: 33.58, lon: 130.38 }, // Fukuoka
  ]);

  it("takes the closest station inside the radius", () => {
    const found = nearestSounding(index, 139.78, 35.68);
    expect(found?.station.id).toBe("0-20000-0-47646");
    expect(found?.distanceKm).toBeCloseTo(52, 0);
  });

  it("answers nothing when every station is beyond the radius", () => {
    // Mid-Pacific: the nearest is thousands of kilometres away.
    expect(nearestSounding(index, -170, 20)).toBeNull();
  });

  it("holds the radius exactly, and takes a wider one when asked", () => {
    const far = nearestSounding(index, 139.78, 35.68, 10);
    expect(far).toBeNull();
    const wide = nearestSounding(index, 139.78, 35.68, 400);
    expect(wide?.station.id).toBe("0-20000-0-47646");
  });

  it("defaults to the synoptic radius", () => {
    expect(SOUNDING_RADIUS_KM).toBe(150);
    // 200 km out: inside a 400 km radius, outside the default.
    const point = { lon: 140.13, lat: 37.85 };
    expect(nearestSounding(index, point.lon, point.lat)).toBeNull();
    expect(nearestSounding(index, point.lon, point.lat, 400)?.station.id).toBe("0-20000-0-47646");
  });

  it("answers nothing on an index with no stations", () => {
    expect(nearestSounding(soundingIndex([]), 0, 0)).toBeNull();
  });
});

describe("nearestAirport", () => {
  const index = airportIndex([
    { icao: "RJTT", lat: 35.553, lon: 139.781 }, // Haneda
    { icao: "RJAA", lat: 35.765, lon: 140.386 }, // Narita
    { icao: "RJOO", lat: 34.785, lon: 135.438 }, // Itami
  ]);

  it("takes the closest of two airports over the same city", () => {
    expect(nearestAirport(index, 139.75, 35.68)?.station.icao).toBe("RJTT");
    expect(nearestAirport(index, 140.3, 35.8)?.station.icao).toBe("RJAA");
  });

  it("is tighter than the sounding radius, because a METAR is a runway", () => {
    expect(AIRPORT_RADIUS_KM).toBe(40);
    // Mount Fuji: 80 km from Haneda, inside the sounding radius and outside
    // the airport one.
    const fuji = { lon: 138.73, lat: 35.36 };
    expect(nearestAirport(index, fuji.lon, fuji.lat)).toBeNull();
    expect(nearestAirport(index, fuji.lon, fuji.lat, 150)?.station.icao).toBe("RJTT");
  });

  it("answers nothing over open water", () => {
    expect(nearestAirport(index, 160, 20)).toBeNull();
  });
});

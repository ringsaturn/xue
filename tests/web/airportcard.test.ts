import { describe, expect, it } from "vitest";

import { buildAirportCard } from "../../web/src/stations/card";
import type { AirportStation } from "../../web/src/stations/schema";

const station = (icao: string): AirportStation => ({
  icao,
  lat: 35.553,
  lon: 139.781,
  elev: 5,
  obsTime: "2026-11-19T12:00:00Z",
  t: 14.2,
  td: 8.1,
  wd: 340,
  ws: 4.1,
  gust: null,
  vis: 10000,
  qnh: 1021,
  category: "VFR",
  tafPresent: true,
  offset: 0,
  length: 0,
});

const options = { formatTime: (time: string) => time };

describe("an airport's card", () => {
  it("links the airport's own page when the airport has one", () => {
    const link = buildAirportCard(station("RJTT"), options).querySelector<HTMLAnchorElement>("a.station-card-link");
    expect(link?.getAttribute("href")).toBe("/airports/rjtt");
  });

  it("has no link for an airport without a page", () => {
    expect(buildAirportCard(station("RJSN"), options).querySelector("a.station-card-link")).toBeNull();
  });
});

import { describe, expect, it } from "vitest";

import { BASIN_REGIONS, BASIN_VIEWS, seasonFeatures, seasonTracks, trackInBasin } from "../../web/src/tc/seasonlayers";

const OVERVIEW = {
  season: 2026,
  storms: [
    // Across the antimeridian, westward: 178°E → 179°W → 176°W.
    { id: "SH012026", track: [[1780, -150, 1], [-1790, -160, 3], [-1760, -170, 2]] as [number, number, number][] },
    // The Atlantic, east of 20°W: put on the Pacific-centred side.
    { id: "AL012026", track: [[-600, 150, 0], [-650, 200, -1]] as [number, number, number][] },
    { id: "WP012026", track: [] as [number, number, number][] },
  ],
};

describe("a season's tracks", () => {
  it("keeps a track whole across the antimeridian and on the Pacific side", () => {
    const tracks = seasonTracks(OVERVIEW);
    expect(tracks.map((track) => track.id)).toEqual(["SH012026", "AL012026"]);
    expect(tracks[0]!.points.map((point) => point[0])).toEqual([178, 181, 184]);
    expect(tracks[1]!.points.map((point) => point[0])).toEqual([300, 295]);
    expect(tracks[0]!.basin).toBe("SH");
    expect(tracks[0]!.peak).toBe(3);
    expect(tracks[1]!.peak).toBe(0);
  });

  it("draws one segment per step, coloured by the class it starts in", () => {
    const features = seasonFeatures(seasonTracks(OVERVIEW)).features;
    expect(features).toHaveLength(3);
    expect(features[0]!.properties).toEqual({ id: "SH012026", basin: "SH", cls: 1, peak: 3 });
    expect(features[0]!.geometry.coordinates).toEqual([
      [178, -15],
      [181, -16],
    ]);
    expect(features[2]!.properties!.cls).toBe(0);
  });

  it("puts a storm under every basin it reached", () => {
    // NOLO (EP152026): from 108.5°W across the antimeridian to 144.9°E.
    const nolo = seasonTracks({
      season: 2026,
      storms: [{ id: "EP152026", track: [[-1085, 105, 0], [-1500, 150, 2], [1790, 200, 1], [1449, 324, 0]] }],
    })[0]!;
    expect(["WP", "CP", "EP", "AL", "IO", "SH"].filter((basin) => trackInBasin(nolo, basin))).toEqual(["WP", "CP", "EP"]);
    expect(seasonTracks(null)).toEqual([]);
  });

  it("keeps the Gulf of Mexico Atlantic and the Mexican Pacific coast eastern Pacific", () => {
    const at = (id: string, lon: number, lat: number) =>
      seasonTracks({ season: 2025, storms: [{ id, track: [[lon * 10, lat * 10, 0]] }] })[0]!;
    expect(trackInBasin(at("AL012025", -92, 25), "EP")).toBe(false);
    expect(trackInBasin(at("EP012025", -95, 14), "AL")).toBe(false);
    expect(trackInBasin(at("AL022025", -100, 15), "EP")).toBe(true);
  });

  it("frames every basin it groups", () => {
    expect(Object.keys(BASIN_VIEWS).sort()).toEqual(Object.keys(BASIN_REGIONS).sort());
  });
});

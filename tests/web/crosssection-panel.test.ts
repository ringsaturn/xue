import { describe, expect, it } from "vitest";

import { lineAim } from "../../web/src/crosssection/aim";
import { pressureY } from "../../web/src/crosssection/chart";
import { fillForField } from "../../web/src/crosssection/control";
import { sectionFeatures } from "../../web/src/crosssection/maplayer";
import { axisDistance, axisHeight, axisHeights, drawUnits, heightBase, legendTicks, legendUnit } from "../../web/src/crosssection/displayunits";
import { axisSteps, labelsFit, readingLines, sectionPosition, varyingCoordinate } from "../../web/src/crosssection/panel";
import { sectionLegend } from "../../web/src/crosssection/ramps";
import type { SectionReading } from "../../web/src/crosssection/types";
import { setCoordinateFormat } from "../../web/src/mapprefs";
import { setUnitOverride } from "../../web/src/units";

const READING: SectionReading = {
  point: 3,
  pressure: 849.6,
  belowGround: false,
  surfacePressure: 1001.2,
  height: 1456.7,
  temperature: 12.34,
  humidity: 45.6,
  windSpeed: 12.3,
  windDirection: 359.8,
  theta: 300.12,
  thetaE: 322.5,
};

describe("the section's readout", () => {
  it("reads a point in the reader's units, the pressure in hPa", () => {
    expect(readingLines(READING, "here")).toEqual([
      "850 hPa · 1457 m",
      "here",
      "12.3 °C · Humidity 46 %",
      "Wind 0° 12.3 m/s",
      "θ 300.1 K · θe 322.5 K",
    ]);
    setUnitOverride("temperature", "F");
    setUnitOverride("height", "ft");
    expect(readingLines(READING, "here").slice(0, 3)).toEqual(["850 hPa · 4779 ft", "here", "54.2 °F · Humidity 46 %"]);
  });

  it("says where the ground is under it, and leaves a value that is none out", () => {
    expect(readingLines({ ...READING, belowGround: true }, "here")).toEqual(["850 hPa", "here", "Below ground · surface 1001 hPa"]);
    expect(readingLines({ ...READING, humidity: Number.NaN, thetaE: Number.NaN }, "here")[4]).toBe("θ 300.1 K · θe --");
  });

  it("writes places in degrees within one turn, or in the chosen grid", () => {
    expect(sectionPosition([190, 35.5], 2)).toBe("35.50°N 170.00°W");
    setCoordinateFormat("mgrs");
    expect(sectionPosition([139.767, 35.681], 2)).toMatch(/^54S/);
    setCoordinateFormat("latlon");
  });
});

describe("the section's axes", () => {
  const heights = (base: number) =>
    Array.from({ length: Math.floor(16_500 / base) }, (_, index) => ({
      metres: (index + 1) * base,
      pressure: 1013 * Math.exp((-(index + 1) * base) / 7400),
    }));
  const gaps = (kept: { pressure: number }[], top: number, height: number) =>
    kept.slice(1).map(({ pressure }, index) => pressureY(kept[index]!.pressure, top, height) - pressureY(pressure, top, height));

  it("labels heights no closer than they can be read", () => {
    for (const height of [160, 215, 300]) {
      const kept = axisHeights(heights(1000), 100, height);
      expect(kept.length).toBeGreaterThan(1);
      expect(Math.min(...gaps(kept, 100, height))).toBeGreaterThanOrEqual(20);
    }
    expect(axisHeights(heights(1000), 100, 300).map(({ metres }) => axisHeight(metres))).toEqual(
      [2, 4, 6, 8, 10, 12, 14, 16].map((km) => `${km} km`),
    );
    expect(axisHeights(heights(1000), 500, 300).map(({ metres }) => metres)).toEqual([1000, 2000, 3000, 4000, 5000]);
  });

  it("labels round thousands of feet, and none drawn for metres", () => {
    setUnitOverride("height", "ft");
    expect(axisHeights(heights(heightBase()), 100, 215).map(({ metres }) => axisHeight(metres))).toEqual([
      "10k ft",
      "20k ft",
      "30k ft",
      "40k ft",
      "50k ft",
    ]);
    expect(axisHeights(heights(1000), 100, 215)).toEqual([]);
  });

  it("starts the distance axis at a zero of the unit the rest is in", () => {
    expect([axisDistance(0), axisDistance(1_000_000)]).toEqual(["0 km", "1,000 km"]);
    setUnitOverride("distance", "mi");
    expect(axisDistance(0)).toBe("0 mi");
  });

  it("keeps the distance labels apart, and the ends whatever the room", () => {
    // Two ends inside the plot, so two widths and a gap are all they need.
    expect(labelsFit([90, 90], 194)).toBe(true);
    expect(labelsFit([90, 90], 193)).toBe(false);
    expect(labelsFit([90, 90, 90], 300)).toBe(true);
    expect(labelsFit([90, 90, 90], 290)).toBe(false);
    // The ends reach inward where the marks between reach both ways.
    expect(axisSteps(846, () => 95)).toBe(5);
    expect(axisSteps(846, () => 125)).toBe(4);
    expect(axisSteps(268, () => 92)).toBe(1);
    expect(axisSteps(268, () => 50)).toBe(2);
    expect(axisSteps(268, () => 140)).toBe(0);
  });

  it("names the coordinate that changes more", () => {
    expect(varyingCoordinate([{ lon: 140, lat: 10 }, { lon: 141, lat: 60 }])).toBe("lat");
    expect(varyingCoordinate([{ lon: 170, lat: 30 }, { lon: 190, lat: 40 }])).toBe("lon");
  });
});

describe("the section in the reader's units", () => {
  const labels = (fill: "tmp" | "wind" | "cross" | "thetae") => legendTicks(sectionLegend(fill)).map(({ label }) => label);

  it("ticks the legends at round values", () => {
    expect(labels("tmp")).toEqual(["-80", "-60", "-40", "-20", "0", "20"]);
    expect(labels("wind")).toEqual(["0", "10", "20", "30", "40", "50", "60", "70", "80"]);
    expect(labels("cross")).toEqual(["-90", "-60", "-30", "0", "30", "60", "90"]);
    setUnitOverride("temperature", "F");
    setUnitOverride("wind", "kt");
    expect(labels("tmp")).toEqual(["-80", "-40", "0", "40", "80"]);
    expect(legendTicks(sectionLegend("tmp"))[2]!.value).toBeCloseTo(-17.78, 2);
    expect(labels("wind")).toEqual(["0", "20", "40", "60", "80", "100", "120", "140"]);
    expect(labels("cross")).toEqual(["-120", "-60", "0", "60", "120"]);
    expect([legendUnit("temperature"), legendUnit("wind"), legendUnit("kelvin")]).toEqual(["°F", "kt", "K"]);
    setUnitOverride("wind", "kmh");
    expect(labels("cross")).toEqual(["-240", "-120", "0", "120", "240"]);
    expect(labels("thetae")).toEqual(["260", "280", "300", "320", "340", "360", "380", "400"]);
  });

  it("has the worker draw round isotachs and the reader's freezing point", () => {
    expect(drawUnits()).toEqual({ wind: { scale: 1, interval: 10, first: 20, heavy: 50 }, freezing: "0 °C", heightStep: 1000 });
    setUnitOverride("wind", "kt");
    setUnitOverride("temperature", "F");
    const { wind, freezing } = drawUnits();
    expect([wind.interval, wind.first, freezing]).toEqual([20, 40, "32 °F"]);
    // The hundred-knot line is the first a jet's.
    expect(wind.heavy).toBeGreaterThan(80);
    expect(wind.heavy).toBeLessThanOrEqual(100);
    setUnitOverride("wind", "kmh");
    expect([drawUnits().wind.interval, drawUnits().wind.first]).toEqual([40, 80]);
    setUnitOverride("wind", "mph");
    expect([drawUnits().wind.interval, drawUnits().wind.first]).toEqual([20, 40]);
  });
});

describe("where the map goes for a covered line", () => {
  const map = { left: 0, top: 0, right: 1500, bottom: 950 };
  const panel = { left: 270, top: 500, right: 1230, bottom: 840 };

  it("stays where the line is clear, or off to the side of the panel", () => {
    expect(lineAim([{ x: 700, y: 100 }, { x: 800, y: 470 }], map, panel)).toBeNull();
    expect(lineAim([{ x: 60, y: 100 }, { x: 200, y: 900 }], map, panel)).toBeNull();
  });

  it("lifts a line that fits without zooming, and leaves its place across", () => {
    const move = lineAim([{ x: 600, y: 450 }, { x: 900, y: 700 }], map, panel)!;
    expect(move.scale).toBe(1);
    expect(move.from).toEqual({ x: 750, y: 575 });
    // The middle of 72 to 476, from the middle of the map.
    expect(move.offset).toEqual([0, 274 - 475]);
  });

  it("zooms out just enough for one that does not, and moves it off the edge", () => {
    const tall = lineAim([{ x: 750, y: 0 }, { x: 750, y: 808 }], map, panel)!;
    expect(tall.scale).toBeCloseTo(0.5, 9);
    expect(lineAim([{ x: 750, y: 0 }, { x: 750, y: 808 }], map, panel, 0.85)!.scale).toBeCloseTo(0.425, 9);
    const edge = lineAim([{ x: 1100, y: 600 }, { x: 1500, y: 600 }], map, panel)!;
    expect(edge.offset[0] + 750 + 200).toBe(1500 - 76);
  });

  it("does nothing with no map left to move it into", () => {
    expect(lineAim([{ x: 700, y: 400 }], map, { ...panel, top: 150 })).toBeNull();
  });
});

describe("what a line starts with", () => {
  it("is filled with the field it was drawn over where the section has it", () => {
    expect(fillForField("tmp850")).toBe("tmp");
    expect(fillForField("tmp2m")).toBe("tmp");
    expect(fillForField("rh700")).toBe("rh");
    expect(fillForField("wind10m")).toBe("wind");
    expect(fillForField("prate")).toBe("thetae");
    expect(fillForField(null)).toBe("thetae");
  });
});

describe("the line on the map", () => {
  it("letters its ends, A first, and marks the point read", () => {
    const { features } = sectionFeatures({ a: [170, 40], b: [-170, 40] }, true, [180, 41]);
    expect(features.map(({ properties }) => [properties.role, properties.letter ?? null])).toEqual([
      ["line", null],
      ["end", "A"],
      ["end", "B"],
      ["hover", null],
    ]);
    const line = features[0]!.geometry as GeoJSON.LineString;
    // Continuous across the antimeridian, and bowed poleward between.
    expect(line.coordinates[line.coordinates.length - 1]![0]).toBeCloseTo(190);
    expect(Math.max(...line.coordinates.map(([, lat]) => lat!))).toBeGreaterThan(40.2);
  });

  it("is drawn empty with no section under it, and not at all with no line", () => {
    expect(sectionFeatures({ a: [0, 0], b: [10, 0] }, false, null).features[0]!.properties.live).toBe(false);
    expect(sectionFeatures(null, true, [1, 1]).features).toEqual([]);
  });
});

import { describe, expect, it } from "vitest";

import {
  cellOf,
  cellQuery,
  encodeQueryValue,
  groupByGrid,
  manifestGrids,
  parseGrid,
  runDirectory,
} from "../../web/src/apiconsole/locate";

const GFS = {
  width: 1440,
  height: 721,
  layout: "row-major",
  rowOrder: "north-to-south",
  columnOrder: "west-to-east",
  firstLongitude: -180,
  firstLatitude: 90,
  longitudeStep: 0.25,
  latitudeStep: -0.25,
  wrapLongitude: true,
};

const HRRR = {
  width: 2000,
  height: 1000,
  firstLongitude: -134,
  firstLatitude: 53,
  longitudeStep: 0.03,
  latitudeStep: -0.03,
  wrapLongitude: false,
};

describe("cellOf", () => {
  const grid = parseGrid(GFS)!;

  it("matches the API's documented example", () => {
    expect(cellOf(grid, 35.7, 139.7)).toEqual({ row: 217, column: 1279, longitude: 139.75, latitude: 35.75 });
  });

  it("wraps a longitude past 180 onto its -360 twin", () => {
    expect(cellOf(grid, 35.7, 139.7 + 360 - 360)).toEqual(cellOf(grid, 35.7, 139.7));
    expect(cellOf(grid, 0, 359.9)).toEqual(cellOf(grid, 0, -0.1));
  });

  it("rounds the east half of the last column onto column 0 of a global grid", () => {
    expect(cellOf(grid, 0, 179.9)).toMatchObject({ column: 0, longitude: -180 });
  });

  it("rounds with floor(x + 0.5), as the server does, not Math.round", () => {
    // Math.round(0.49999999999999994) is 0; floor(0.49999999999999994 + 0.5) is 1.
    const unit = parseGrid({ width: 360, height: 90, firstLongitude: 0, firstLatitude: 0, longitudeStep: 1, latitudeStep: 1 })!;
    expect(cellOf(unit, 0.49999999999999994, 0)?.row).toBe(1);
  });

  it("answers no negative zero on the first row", () => {
    expect(Object.is(cellOf(grid, 90, -180)?.row, 0)).toBe(true);
  });

  it("puts a point outside a regional grid off it", () => {
    const regional = parseGrid(HRRR)!;
    expect(cellOf(regional, 35.68, 139.77)).toBeNull();
    expect(cellOf(regional, 40, -100)).toMatchObject({ row: 433, column: 1133 });
  });
});

describe("parseGrid", () => {
  it("fills the reference defaults and infers the wrap", () => {
    expect(parseGrid({ width: 1440, height: 721 })).toEqual({
      width: 1440,
      height: 721,
      firstLongitude: -180,
      firstLatitude: 90,
      longitudeStep: 0.25,
      latitudeStep: -0.25,
      wrapLongitude: true,
    });
  });

  it("refuses an order the rule does not cover", () => {
    expect(parseGrid({ ...GFS, rowOrder: "south-to-north" })).toBeNull();
  });
});

describe("manifest grids", () => {
  const manifest = {
    bundles: [
      { variable: "tmp2m", grid: GFS },
      { variable: "wind10m", grid: GFS },
      { variable: "tmp2m", grid: HRRR },
      { variable: "htsgw", grid: { ...GFS, width: 720, height: 361, longitudeStep: 0.5, latitudeStep: -0.5 } },
    ],
  };

  it("keeps the first entry of a repeated id", () => {
    expect(manifestGrids(manifest).get("tmp2m")?.width).toBe(1440);
  });

  it("groups bundles by grid in first-seen order", () => {
    const groups = groupByGrid(manifestGrids(manifest), ["htsgw", "tmp2m", "wind10m", "nope"]);
    expect(groups.map((group) => group.bundles)).toEqual([["htsgw"], ["tmp2m", "wind10m"]]);
  });
});

describe("runDirectory", () => {
  it.each([
    ["gfs.2026103006", "gfs.2026103006"],
    ["mrms.2026092509.1455", "mrms.2026092509/1455"],
    ["mrms.2026092509/1455", "mrms.2026092509/1455"],
    ["", null],
    ["gfs/../x", null],
    ["gfs 1", null],
  ])("%s → %s", (name, directory) => {
    expect(runDirectory(name)).toBe(directory);
  });
});

describe("the canonical cell URL", () => {
  it("keeps , / : literal and escapes the rest in upper-case hex", () => {
    expect(encodeQueryValue("mrms.2026092509/1455")).toBe("mrms.2026092509/1455");
    expect(encodeQueryValue("2026-10-30T00:00:00+09:00")).toBe("2026-10-30T00:00:00%2B09:00");
    expect(encodeQueryValue("a b")).toBe("a%20b");
  });

  it("orders the parameters and sorts the variables once each", () => {
    expect(
      cellQuery({ source: "gfs", run: "gfs.2026103006", row: 217, column: 1279, variables: ["wind10m", "tmp2m", "wind10m"] }),
    ).toBe("source=gfs&run=gfs.2026103006&row=217&column=1279&variables=tmp2m,wind10m");
  });
});

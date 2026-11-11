import { describe, expect, it } from "vitest";

import { parseGars, toGars } from "../../web/src/gars";

// GARS per NGA: longitude bands 001–720 eastward from 180°W in 30′ steps,
// latitude bands AA–QZ northward from 90°S in 30′ steps (letters without I
// and O), quadrants 1 NW 2 NE 3 SW 4 SE, keypads 1–9 NW to SE.
describe("toGars", () => {
  it("names NGA's example cell 006AG39", () => {
    // 006 is 177°30′W–177°W, AG band 6 is 87°S–86°30′S. 12′ east and 1.2′
    // north of the cell's SW corner is the SW quadrant (3), its east column
    // and bottom row (keypad 9).
    expect(toGars(-87 + 1.2 / 60, -177.5 + 12 / 60)).toBe("006AG39");
  });

  it("names Tokyo station", () => {
    // 319.767125° east of 180°W → band 640; 125.681236° north of 90°S →
    // band 251 = 10 × 24 + 11 → L, M. 16.03′ into the cell eastward and
    // 10.87′ northward: the SE quadrant (4); 1.03′ and 10.87′ into it: the
    // west column, top row (keypad 1).
    expect(toGars(35.681236, 139.767125)).toBe("640LM41");
    expect(toGars(35.681236, 139.767125, 15)).toBe("640LM4");
    expect(toGars(35.681236, 139.767125, 30)).toBe("640LM");
  });

  it("puts a point on a line in the cell east and north of it", () => {
    // 0°, 0° is the SW corner of 361HN (band 180 = 7 × 24 + 12 → H, N).
    expect(toGars(0, 0)).toBe("361HN37");
    expect(toGars(-90, -180)).toBe("001AA37");
  });

  it("gives 180°E and 90°N to the last cell", () => {
    expect(toGars(90, 180)).toBe("720QZ23");
    expect(toGars(0, 180)).toBe("720HN49");
    expect(toGars(89.99, 179.99)).toBe("720QZ23");
  });

  it("wraps longitudes", () => {
    expect(toGars(35.681236, 139.767125 - 360)).toBe("640LM41");
  });

  it("refuses what is not a coordinate", () => {
    expect(toGars(91, 0)).toBeNull();
    expect(toGars(Number.NaN, 0)).toBeNull();
  });
});

describe("parseGars", () => {
  it("finds NGA's example cell", () => {
    const cell = parseGars("006AG39");
    expect(cell?.south).toBeCloseTo(-87, 12);
    expect(cell?.west).toBeCloseTo(-177.5 + 10 / 60, 12);
    expect(cell?.size).toBeCloseTo(1 / 12, 12);
  });

  it("finds the coarser cells", () => {
    expect(parseGars("720qz")).toEqual({ south: 89.5, west: 179.5, size: 0.5 });
    expect(parseGars("361HN 2")).toEqual({ south: 0.25, west: 0.25, size: 0.25 });
  });

  it("round-trips every quadrant and keypad", () => {
    for (let q = 1; q <= 4; q += 1) {
      for (let k = 1; k <= 9; k += 1) {
        const cell = parseGars(`640LM${q}${k}`);
        expect(cell).not.toBeNull();
        if (cell === null) continue;
        expect(toGars(cell.south + cell.size / 2, cell.west + cell.size / 2)).toBe(`640LM${q}${k}`);
      }
    }
  });

  it("refuses malformed references", () => {
    for (const text of ["000AA", "721AA", "001RA", "001AI", "001AO", "001AA5", "001AA10", "001A", ""]) expect(parseGars(text)).toBeNull();
  });
});

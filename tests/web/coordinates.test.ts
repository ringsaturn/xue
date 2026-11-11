import { describe, expect, it } from "vitest";

import { digitsFor, formatCoordinate, garsPrecisionFor } from "../../web/src/coordinates";

// Big Ben: MGRS / UTM per PROJ (EPSG:32630 699565.6780 5709430.7186), the
// National Grid per PROJ's Helmert pipeline (530268.09 179643.90), GARS
// hand-derived (longitude band 360; latitude band 283 = 11 × 24 + 19 →
// M, V; 22.52′ east and 0.04′ north into the cell: SE quadrant 4, then
// 7.52′ east, 0.04′ north in it: middle column, bottom row → keypad 8).
const BIG_BEN = [51.500729, -0.124625] as const;
const PARIS = [48.8566, 2.3522] as const;

describe("digitsFor", () => {
  it("maps a resolution to digits per axis", () => {
    expect(digitsFor(1)).toBe(5);
    expect(digitsFor(10)).toBe(4);
    expect(digitsFor(100)).toBe(3);
    expect(digitsFor(1000)).toBe(2);
    expect(digitsFor(10_000)).toBe(1);
    expect(digitsFor(100_000)).toBe(1);
    expect(digitsFor(5)).toBe(4);
  });
});

describe("garsPrecisionFor", () => {
  it("keeps the keypad to 10 km, the quadrant to 30 km", () => {
    expect(garsPrecisionFor(1)).toBe(5);
    expect(garsPrecisionFor(1000)).toBe(5);
    expect(garsPrecisionFor(10_000)).toBe(5);
    expect(garsPrecisionFor(20_000)).toBe(15);
    expect(garsPrecisionFor(50_000)).toBe(30);
  });
});

describe("formatCoordinate", () => {
  it("leaves degrees to the caller", () => {
    expect(formatCoordinate("latlon", ...BIG_BEN, 1)).toBeNull();
  });

  it("formats MGRS with digits from the resolution", () => {
    expect(formatCoordinate("mgrs", ...BIG_BEN, 1)).toBe("30U XC 99565 09430");
    expect(formatCoordinate("mgrs", ...BIG_BEN, 10)).toBe("30U XC 9956 0943");
    expect(formatCoordinate("mgrs", ...BIG_BEN, 100)).toBe("30U XC 995 094");
    expect(formatCoordinate("mgrs", ...BIG_BEN, 1000)).toBe("30U XC 99 09");
  });

  it("formats UTM rounded to the resolution", () => {
    expect(formatCoordinate("utm", ...BIG_BEN, 1)).toBe("30N 699566E 5709431N");
    expect(formatCoordinate("utm", ...BIG_BEN, 1000)).toBe("30N 700000E 5709000N");
  });

  it("formats GARS", () => {
    expect(formatCoordinate("gars", ...BIG_BEN, 1)).toBe("360MV48");
    expect(formatCoordinate("gars", ...BIG_BEN, 1000)).toBe("360MV48");
    expect(formatCoordinate("gars", ...BIG_BEN, 20_000)).toBe("360MV4");
    expect(formatCoordinate("gars", ...BIG_BEN, 100_000)).toBe("360MV");
  });

  it("formats the National Grid no finer than 10 m", () => {
    expect(formatCoordinate("bng", ...BIG_BEN, 1)).toBe("TQ 3026 7964");
    expect(formatCoordinate("bng", ...BIG_BEN, 100)).toBe("TQ 302 796");
    expect(formatCoordinate("bng", ...BIG_BEN, 1000)).toBe("TQ 30 79");
  });

  it("is null where the format cannot name the point", () => {
    expect(formatCoordinate("bng", ...PARIS, 1)).toBeNull();
    expect(formatCoordinate("mgrs", Number.NaN, 0, 1)).toBeNull();
  });
});

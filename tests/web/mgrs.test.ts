import { describe, expect, it } from "vitest";
import { fromUps, fromUtm, gzdCells, parseMgrs, toMgrs, toUps, toUtm, toUtmReference, utmZone } from "../../web/src/mgrs";

// Expected eastings and northings are PROJ's (`+proj=utm` / `+proj=ups`,
// WGS84); the letters follow NGA's MGRS tables.
describe("toUtm", () => {
  it("matches PROJ to the millimetre", () => {
    const tokyo = toUtm(35.681236, 139.767125);
    expect(tokyo.zone).toBe(54);
    expect(tokyo.easting).toBeCloseTo(388435.6871, 3);
    expect(tokyo.northing).toBeCloseTo(3949293.9781, 3);
    const sydney = toUtm(-33.8568, 151.2153);
    expect(sydney.zone).toBe(56);
    expect(sydney.north).toBe(false);
    expect(sydney.easting).toBeCloseTo(334900.5697, 3);
    expect(sydney.northing).toBeCloseTo(6252288.7529, 3);
  });

  it("inverts", () => {
    const back = fromUtm(54, true, 388435.6871, 3949293.9781);
    expect(back.lat).toBeCloseTo(35.681236, 8);
    expect(back.lon).toBeCloseTo(139.767125, 8);
  });
});

describe("toUps", () => {
  it("matches PROJ", () => {
    const near = toUps(89.5, 45);
    expect(near.easting).toBeCloseTo(2039253.0855, 3);
    expect(near.northing).toBeCloseTo(1960746.9145, 3);
    const south = toUps(-80.5, -60);
    expect(south.easting).toBeCloseTo(1084566.4898, 3);
    expect(south.northing).toBeCloseTo(2528525.7835, 3);
  });

  it("inverts", () => {
    const back = fromUps(false, 1084566.4898, 2528525.7835);
    expect(back.lat).toBeCloseTo(-80.5, 8);
    expect(back.lon).toBeCloseTo(-60, 8);
  });
});

describe("utmZone", () => {
  it("widens 32V over south-west Norway and splits Svalbard into 31X–37X", () => {
    expect(utmZone(60.39, 5.32)).toBe(32);
    expect(utmZone(60, 2)).toBe(31);
    expect(utmZone(55.9, 5.32)).toBe(31);
    expect(utmZone(78.2232, 15.6267)).toBe(33);
    expect(utmZone(79, 25)).toBe(35);
    expect(utmZone(79, 8.9)).toBe(31);
    expect(utmZone(79, 40)).toBe(37);
  });

  it("puts a zone's west edge in that zone", () => {
    expect(utmZone(35, 144)).toBe(55);
    expect(utmZone(35, 143.999999)).toBe(54);
  });
});

describe("toMgrs", () => {
  it("names the Washington Monument as the NGA primer does", () => {
    expect(toMgrs(38.8895, -77.0352)).toBe("18S UJ 23486 06483");
  });

  it("names Tokyo Station and truncates to the precision", () => {
    expect(toMgrs(35.681236, 139.767125)).toBe("54S UE 88435 49293");
    expect(toMgrs(35.681236, 139.767125, 3)).toBe("54S UE 884 492");
    expect(toMgrs(35.681236, 139.767125, 1)).toBe("54S UE 8 4");
  });

  it("follows the Norway and Svalbard exceptions", () => {
    expect(toMgrs(60.39, 5.32)).toBe("32V KN 97230 00510");
    expect(toMgrs(60, 2)).toBe("31V DG 44223 51832");
    expect(toMgrs(78.2232, 15.6267)).toBe("33X WG 14278 83355");
    expect(toMgrs(79, 25)).toBe("35X MH 57405 70703");
  });

  it("works in the southern hemisphere", () => {
    expect(toMgrs(-33.8568, 151.2153)).toBe("56H LH 34900 52288");
    expect(toMgrs(-33.9249, 18.4241)).toBe("34H BH 61881 43182");
    expect(toMgrs(-0.5, 10)).toBe("32M PE 11276 44726");
  });

  it("splits at a zone boundary and the equator", () => {
    expect(toMgrs(35, 144)).toBe("55S BU 26201 77156");
    expect(toMgrs(35, 143.999999)).toBe("54S YD 73798 77156");
    expect(toMgrs(0, 0)).toBe("31N AA 66021 00000");
  });

  it("uses UPS in the polar caps", () => {
    expect(toMgrs(90, 0)).toBe("Z AH 00000 00000");
    expect(toMgrs(89.5, 45)).toBe("Z AG 39253 60746");
    expect(toMgrs(86, -120)).toBe("Y UK 15252 22134");
    expect(toMgrs(84.5, 100)).toBe("Z JJ 01797 06113");
    expect(toMgrs(-90, 0)).toBe("B AN 00000 00000");
    expect(toMgrs(-89.99, -139.27)).toBe("A ZM 99275 99158");
    expect(toMgrs(-80.5, -60)).toBe("A LT 84566 28525");
  });

  it("hands over between UTM and UPS at 84°N and 80°S", () => {
    expect(toMgrs(83.99, 10)).toBe("33X VP 41625 29512");
    expect(toMgrs(84, 10)).toBe("Z BA 15776 43401");
    expect(toMgrs(-80, 10)).toBe("32C NS 19384 18247");
    expect(toMgrs(-80.0001, 10)).toBe("B BY 93259 96031");
  });

  it("refuses what is not a coordinate", () => {
    expect(toMgrs(Number.NaN, 0)).toBeNull();
    expect(toMgrs(91, 0)).toBeNull();
  });
});

function metresBetween(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const rad = Math.PI / 180;
  const h =
    Math.sin(((lat2 - lat1) * rad) / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.sqrt(h));
}

describe("toUtmReference", () => {
  // PROJ: EPSG:32654 Tokyo 388435.6871 3949293.9781, EPSG:32630 Big Ben
  // 699565.6780 5709430.7186, EPSG:32756 Sydney 334900.5697 6252288.7529,
  // EPSG:32661 (UPS N) 89.5°N 45°E 2039253.0855 1960746.9145, EPSG:32761
  // (UPS S) 89°S 0° 2000000 2111026.5201.
  it("names the zone and hemisphere, not the band", () => {
    expect(toUtmReference(35.681236, 139.767125)).toBe("54N 388436E 3949294N");
    expect(toUtmReference(51.500729, -0.124625)).toBe("30N 699566E 5709431N");
    expect(toUtmReference(-33.8568, 151.2153)).toBe("56S 334901E 6252289N");
  });

  it("rounds to the resolution", () => {
    expect(toUtmReference(35.681236, 139.767125, 10)).toBe("54N 388440E 3949290N");
    expect(toUtmReference(35.681236, 139.767125, 1000)).toBe("54N 388000E 3949000N");
    expect(toUtmReference(51.500729, -0.124625, 1000)).toBe("30N 700000E 5709000N");
  });

  it("gives the UPS form in the caps", () => {
    expect(toUtmReference(89.5, 45)).toBe("UPS N 2039253E 1960747N");
    expect(toUtmReference(90, 0)).toBe("UPS N 2000000E 2000000N");
    expect(toUtmReference(-89, 0)).toBe("UPS S 2000000E 2111027N");
  });

  it("refuses what is not a coordinate", () => {
    expect(toUtmReference(Number.NaN, 0)).toBeNull();
    expect(toUtmReference(95, 0)).toBeNull();
  });
});

describe("parseMgrs", () => {
  it("round-trips to the square's south-west corner", () => {
    for (const [lat, lon] of <[number, number][]>[
      [35.681236, 139.767125],
      [60.39, 5.32],
      [78.2232, 15.6267],
      [-33.8568, 151.2153],
      [-0.5, 10],
      [89.5, 45],
      [-80.5, -60],
      [84.5, 100],
    ]) {
      const corner = parseMgrs(toMgrs(lat, lon)!)!;
      // The 1 m square's corner is within its diagonal of the point.
      expect(metresBetween(corner.lat, corner.lon, lat, lon)).toBeLessThan(1.5);
    }
  });

  it("accepts the reference without spaces", () => {
    const corner = parseMgrs("18SUJ2348606483")!;
    expect(corner.lat).toBeCloseTo(38.8895, 4);
    expect(corner.lon).toBeCloseTo(-77.0352, 4);
  });

  it("refuses an unknown square", () => {
    expect(parseMgrs("54S IE 1 1")).toBeNull();
    expect(parseMgrs("C AA")).toBeNull();
    expect(parseMgrs("54S UE 123")).toBeNull();
  });
});

describe("gzdCells", () => {
  it("lists every zone designation once, with the exceptions and the caps", () => {
    const names = gzdCells().map((cell) => cell.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names).not.toContain("32X");
    expect(names).toContain("37X");
    expect(names).toEqual(expect.arrayContaining(["A", "B", "Y", "Z"]));
    const v32 = gzdCells().find((cell) => cell.name === "32V")!;
    expect([v32.west, v32.east]).toEqual([3, 12]);
    expect(gzdCells().find((cell) => cell.name === "54X")!.north).toBe(84);
  });
});

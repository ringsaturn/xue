import { describe, expect, it } from "vitest";

import {
  identifyBundle,
  identityForBundleId,
  identityForParameter,
  identityForParameterPair,
  registeredBundleId,
  sameIdentity,
} from "../../web/src/identity";
import type { BundleParameter, BundleVariable, LinearQuantization } from "../../web/src/manifest";

/** A GRIB2 parameter block, the way schemaVersion 3 metadata writes one.
 * `surface` is the type of first fixed surface and `value` its scaled value;
 * an isobaric level is spelled in pascals, the way GDAL spells it. */
function parameter(
  discipline: number,
  category: number,
  number_: number,
  surface: number,
  value: number | null,
  scaleFactor = 0,
): BundleParameter {
  return {
    discipline,
    parameterCategory: category,
    parameterNumber: number_,
    typeOfFirstFixedSurface: surface,
    scaleFactorOfFirstFixedSurface: value === null ? null : scaleFactor,
    scaledValueOfFirstFixedSurface: value,
  };
}

const QUANTIZATION: LinearQuantization = {
  type: "linear",
  offset: -60,
  scale: 0.5,
  minimumCode: 0,
  maximumCode: 220,
  nodataCode: 255,
};

/** A metadata variable. `numericId` is positional — 1..n within one file —
 * exactly as the encoder writes it, which is the whole reason a numericId
 * cannot identify anything on its own. */
function variable(numericId: number, id: string, parameterBlock?: BundleParameter): BundleVariable {
  return { numericId, id, label: id, unit: "", parameter: parameterBlock, quantization: QUANTIZATION };
}

describe("identityForParameter", () => {
  it("reads every row of the table", () => {
    expect(identityForParameter(parameter(0, 3, 5, 100, 50_000))).toEqual({ family: "hgt", level: 500, vector: false });
    expect(identityForParameter(parameter(0, 3, 1, 101, null))).toEqual({ family: "hgt", level: null, vector: false });
    expect(identityForParameter(parameter(0, 0, 0, 100, 85_000))).toEqual({ family: "tmp", level: 850, vector: false });
    expect(identityForParameter(parameter(0, 0, 0, 103, 2))).toEqual({ family: "tmp", level: null, vector: false });
    expect(identityForParameter(parameter(0, 1, 1, 100, 70_000))).toEqual({ family: "rh", level: 700, vector: false });
    expect(identityForParameter(parameter(0, 1, 0, 100, 85_000))).toEqual({ family: "spfh", level: 850, vector: false });
    expect(identityForParameter(parameter(0, 1, 7, 1, 0))).toEqual({ family: "prate", level: null, vector: false });
    expect(identityForParameter(parameter(0, 4, 192, 1, 0))).toEqual({ family: "dswrf", level: null, vector: false });
    expect(identityForParameter(parameter(0, 16, 5, 10, null))).toEqual({ family: "cref", level: null, vector: false });
  });

  it("reads a local parameter number, which is where dswrf lives", () => {
    // 192 is in the local-use range: it is only meaningful with the rest of
    // the triple, which is exactly how the table is keyed.
    expect(identityForParameter(parameter(0, 4, 192, 1, 0))!.family).toBe("dswrf");
    // The same local number on another category is not it.
    expect(identityForParameter(parameter(0, 1, 192, 1, 0))).toBeNull();
  });

  it("reads a surface written as missing, the way GRIB2 writes one with no value", () => {
    // Composite reflectivity is on the entire atmosphere: both halves null.
    const entireAtmosphere = parameter(0, 16, 5, 10, null);
    expect(entireAtmosphere.scaleFactorOfFirstFixedSurface).toBeNull();
    expect(identityForParameter(entireAtmosphere)).toEqual({ family: "cref", level: null, vector: false });
    // A null surface on an isobaric type has no level, so it is not a field
    // this build can place.
    expect(identityForParameter(parameter(0, 0, 0, 100, null))).toBeNull();
  });

  it("takes the level from the scaled value, not from a table of levels", () => {
    // A negative scale factor is still an exact pascal count.
    expect(identityForParameter(parameter(0, 0, 0, 100, 8_500, -1))!.level).toBe(850);
    // A surface the encoders register nothing on still reads as what it is.
    expect(identityForParameter(parameter(0, 0, 0, 100, 55_000))).toEqual({
      family: "tmp",
      level: 550,
      vector: false,
    });
    // ...and has no registered chart id, which is what makes it unknown.
    expect(registeredBundleId({ family: "tmp", level: 550, vector: false })).toBeNull();
  });

  it("returns null for a parameter this build has no chart knowledge for", () => {
    // Total ozone: a real field, not one we draw.
    expect(identityForParameter(parameter(0, 14, 0, 1, 0))).toBeNull();
    // The right triple on the wrong surface is not the same field.
    expect(identityForParameter(parameter(0, 0, 0, 103, 100))).toBeNull();
    expect(identityForParameter(parameter(0, 1, 7, 100, 85_000))).toBeNull();
  });
});

describe("identityForParameterPair", () => {
  it("recognizes the u/v pairs on their own surfaces", () => {
    expect(identityForParameterPair(parameter(0, 2, 2, 103, 10), parameter(0, 2, 3, 103, 10))).toEqual({
      family: "wind",
      level: null,
      vector: true,
    });
    expect(identityForParameterPair(parameter(0, 2, 2, 100, 85_000), parameter(0, 2, 3, 100, 85_000))).toEqual({
      family: "wind",
      level: 850,
      vector: true,
    });
    expect(identityForParameterPair(parameter(0, 1, 250, 100, 85_000), parameter(0, 1, 251, 100, 85_000))).toEqual({
      family: "qflux",
      level: 850,
      vector: true,
    });
  });

  it("refuses halves of different fields", () => {
    // Two components of one vector have to be on one surface.
    expect(identityForParameterPair(parameter(0, 2, 2, 100, 85_000), parameter(0, 2, 3, 100, 50_000))).toBeNull();
    // v then u is not the pair; the caller tries both orders.
    expect(identityForParameterPair(parameter(0, 2, 3, 103, 10), parameter(0, 2, 2, 103, 10))).toBeNull();
    expect(identityForParameterPair(parameter(0, 2, 2, 103, 10), parameter(0, 0, 0, 103, 10))).toBeNull();
  });
});

describe("identifyBundle", () => {
  it("identifies a scalar bundle from its one variable", () => {
    const found = identifyBundle([variable(1, "tmp2m", parameter(0, 0, 0, 103, 2))])!;
    expect(found.identity).toEqual({ family: "tmp", level: null, vector: false });
    expect(found.variables.map((item) => item.id)).toEqual(["tmp2m"]);
  });

  it("identifies a vector bundle, and puts u before v whichever way the file lists them", () => {
    const u = variable(1, "ugrd850", parameter(0, 2, 2, 100, 85_000));
    const v = variable(2, "vgrd850", parameter(0, 2, 3, 100, 85_000));
    expect(identifyBundle([u, v])!.variables.map((item) => item.id)).toEqual(["ugrd850", "vgrd850"]);
    const reversed = identifyBundle([v, u])!;
    expect(reversed.identity).toEqual({ family: "wind", level: 850, vector: true });
    expect(reversed.variables.map((item) => item.id)).toEqual(["ugrd850", "vgrd850"]);
  });

  it("reads a two-variable bundle that is not a pair as its first variable alone", () => {
    const found = identifyBundle([
      variable(1, "tmp850", parameter(0, 0, 0, 100, 85_000)),
      variable(2, "rh850", parameter(0, 1, 1, 100, 85_000)),
    ])!;
    expect(found.identity.vector).toBe(false);
    expect(found.variables).toHaveLength(1);
    expect(found.variables[0]!.id).toBe("tmp850");
  });

  it("falls back to the legacy id list for a schemaVersion 1 or 2 file", () => {
    // Published runs and showcase cases carry these bytes and are never
    // rebuilt: no parameter block, and only these five ids.
    expect(identifyBundle([variable(1, "tmp2m")])!.identity).toEqual({ family: "tmp", level: null, vector: false });
    expect(identifyBundle([variable(1, "prate")])!.identity.family).toBe("prate");
    expect(identifyBundle([variable(1, "dswrf")])!.identity.family).toBe("dswrf");
    expect(identifyBundle([variable(1, "cref")])!.identity.family).toBe("cref");
    const wind = identifyBundle([variable(1, "ugrd10m"), variable(2, "vgrd10m")])!;
    expect(wind.identity).toEqual({ family: "wind", level: null, vector: true });
    expect(wind.variables.map((item) => item.id)).toEqual(["ugrd10m", "vgrd10m"]);
    // The list is closed: an id that never shipped before v3 is not in it,
    // however conventionally it is named.
    expect(identifyBundle([variable(1, "tmp850")])).toBeNull();
    expect(identifyBundle([variable(1, "prmsl")])).toBeNull();
  });

  it("returns null for a bundle nothing in the file places", () => {
    expect(identifyBundle([])).toBeNull();
    expect(identifyBundle([variable(1, "gust10m")])).toBeNull();
    expect(identifyBundle([variable(1, "tozne", parameter(0, 14, 0, 1, 0))])).toBeNull();
  });
});

describe("identityForBundleId", () => {
  it("reads the naming convention, which is all the manifest carries", () => {
    expect(identityForBundleId("tmp2m")).toEqual({ family: "tmp", level: null, vector: false });
    expect(identityForBundleId("wind10m")).toEqual({ family: "wind", level: null, vector: true });
    expect(identityForBundleId("prmsl")).toEqual({ family: "hgt", level: null, vector: false });
    expect(identityForBundleId("hgt500")).toEqual({ family: "hgt", level: 500, vector: false });
    expect(identityForBundleId("qflux850")).toEqual({ family: "qflux", level: 850, vector: true });
    expect(identityForBundleId("gust10m")).toBeNull();
    expect(identityForBundleId("tozne")).toBeNull();
  });

  it("agrees with the parameter block on every id the encoders publish", () => {
    // The guess and the file must not disagree silently; main.ts warns when
    // they do, and for the published ids they never should.
    const cases: [string, BundleVariable[]][] = [
      ["tmp2m", [variable(1, "tmp2m", parameter(0, 0, 0, 103, 2))]],
      ["prate", [variable(1, "prate", parameter(0, 1, 7, 1, 0))]],
      ["prmsl", [variable(1, "prmsl", parameter(0, 3, 1, 101, null))]],
      ["hgt500", [variable(1, "hgt500", parameter(0, 3, 5, 100, 50_000))]],
      ["tmp850", [variable(1, "tmp850", parameter(0, 0, 0, 100, 85_000))]],
      ["rh700", [variable(1, "rh700", parameter(0, 1, 1, 100, 70_000))]],
      ["spfh850", [variable(1, "spfh850", parameter(0, 1, 0, 100, 85_000))]],
      ["cref", [variable(1, "cref", parameter(0, 16, 5, 10, null))]],
      ["dswrf", [variable(1, "dswrf", parameter(0, 4, 192, 1, 0))]],
      [
        "wind10m",
        [variable(1, "ugrd10m", parameter(0, 2, 2, 103, 10)), variable(2, "vgrd10m", parameter(0, 2, 3, 103, 10))],
      ],
      [
        "qflux850",
        [
          variable(1, "uqflx850", parameter(0, 1, 250, 100, 85_000)),
          variable(2, "vqflx850", parameter(0, 1, 251, 100, 85_000)),
        ],
      ],
    ];
    for (const [id, variables] of cases) {
      expect(sameIdentity(identityForBundleId(id), identifyBundle(variables)!.identity)).toBe(true);
      expect(registeredBundleId(identifyBundle(variables)!.identity)).toBe(id);
    }
  });
});

describe("registeredBundleId", () => {
  it("names the registry entry an identity belongs to, or nothing", () => {
    expect(registeredBundleId(null)).toBeNull();
    expect(registeredBundleId({ family: "tmp", level: 850, vector: false })).toBe("tmp850");
    expect(registeredBundleId({ family: "hgt", level: null, vector: false })).toBe("prmsl");
    expect(registeredBundleId({ family: "wind", level: null, vector: true })).toBe("wind10m");
    // Families with no surface member: a level is the only way to name them.
    expect(registeredBundleId({ family: "rh", level: null, vector: false })).toBeNull();
    expect(registeredBundleId({ family: "qflux", level: null, vector: true })).toBeNull();
    // A surface nothing is registered on has no chart knowledge to look up.
    expect(registeredBundleId({ family: "rh", level: 550, vector: false })).toBeNull();
  });
});

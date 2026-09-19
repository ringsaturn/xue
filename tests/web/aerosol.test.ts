import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/aerosol-registry.json";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId, type ChartFamily } from "../../web/src/identity";
import {
  AEROSOL_CHARTS,
  FAMILIES,
  aerosolChart,
  familyLevels,
  familyMembers,
  familyOf,
  familyVariants,
  isAerosolFamily,
  isobaricLegend,
  levelCode,
  logLegend,
  scalarLegendRange,
} from "../../web/src/levels";
import {
  AEROSOL_IDS,
  FORECAST_MODELS,
  KNOWN_BUNDLE_IDS,
  isVectorBundle,
  parseBundleMetadata,
  type BundleAerosol,
  type BundleParameter,
  type BundleVariable,
  type LogQuantization,
} from "../../web/src/manifest";
import { meteogramRows } from "../../web/src/meteogram";
import { buildPalette, decodeValue, encodeLog } from "../../web/src/palettes";
import { displayUnit } from "../../web/src/units";
import { parseModelFromSearch, parseVariableFromSearch, searchForVariable } from "../../web/src/urlstate";
import { FIELD_GROUPS, variableIds, variableSpec } from "../../web/src/variables";

/** The committed registry both encoders are held to (`tests/test_aerosol.py`,
 * and the Rust encoder's unit tests): the nine aerosol fields with their
 * parameter and aerosol blocks and their logarithmic codebooks. */
interface RegistryEntry {
  label: string;
  unit: string;
  parameter: BundleParameter;
  aerosol: BundleAerosol;
  quality: LogQuantization;
  compact: LogQuantization;
}

const registry = registryJson as unknown as Record<string, RegistryEntry>;

function bundleVariable(id: string, profile: "quality" | "compact" = "quality"): BundleVariable {
  const entry = registry[id]!;
  return {
    numericId: 1,
    id,
    label: entry.label,
    unit: entry.unit,
    parameter: entry.parameter,
    aerosol: entry.aerosol,
    quantization: entry[profile],
  };
}

function rgba(palette: Uint8Array, code: number): [number, number, number, number] {
  return [...palette.subarray(code * 4, code * 4 + 4)] as [number, number, number, number];
}

function metadata(variable: BundleVariable): string {
  return JSON.stringify({
    schemaVersion: 3,
    model: "GEFS-AEROSOLS",
    runTime: "2026-09-19T00:00:00Z",
    time: { unitSeconds: 3600, firstFrameOffset: 0, frameCount: 2, frameStep: 3 },
    grid: { width: 1440, height: 721, firstLongitude: -180, firstLatitude: 90, longitudeStep: 0.25, latitudeStep: -0.25, wrapLongitude: true },
    variables: [variable],
  });
}

const ODD_IDS = ["aod", "aoddust", "aodsalt", "aodsulf", "aodorg", "aodbc"] as const;
const PM_IDS = ["pm25", "pm10", "pm10dust"] as const;

describe("the aerosol registry", () => {
  it("names the nine fields the shell has rows for, in the encoders' order", () => {
    expect(Object.keys(registry)).toEqual([...AEROSOL_IDS]);
    for (const id of AEROSOL_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(variableIds()).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
      expect(variableSpec(id)!.group).toBe("aerosol");
      expect(variableSpec(id)!.ground).toBe("slate");
    }
    expect(FIELD_GROUPS).toEqual(["temperature", "moisture", "wind", "dynamics", "radiation", "aerosol", "ocean", "satellite"]);
  });

  it("resolves every entry's parameter and aerosol blocks to its own id, and back", () => {
    for (const id of AEROSOL_IDS) {
      const entry = registry[id]!;
      const identity = identityForParameter(entry.parameter, undefined, entry.aerosol);
      expect(identity).toEqual({ family: id, level: null, vector: false });
      expect(registeredBundleId(identity)).toBe(id);
      expect(identityForBundleId(id)).toEqual(identity);
      expect(identifyBundle([bundleVariable(id)])?.identity).toEqual(identity);
      expect(isAerosolFamily(id)).toBe(true);
      expect(aerosolChart(id as ChartFamily)).toBe(AEROSOL_CHARTS[id]);
    }
  });

  it("is what tells the species apart, since the parameter is one number for all of them", () => {
    // Every optical depth is (0,20,102) on the entire atmosphere and both
    // PM10 fields are the NCEP-local (0,13,192) on the ground: without the
    // block the field is unknown, and a block naming another species,
    // another size cut or another wavelength is unknown too.
    const { parameter, aerosol } = registry.aoddust!;
    expect(identityForParameter(parameter)).toBeNull();
    expect(identityForParameter(parameter, undefined, { ...aerosol, aerosolType: 62003 })).toBeNull();
    expect(identityForParameter(parameter, undefined, { ...aerosol, scaledValueOfFirstWavelength: 338, scaledValueOfSecondWavelength: 342 })).toBeNull();
    expect(identityForParameter(parameter, undefined, { ...aerosol, scaledValueOfFirstSize: 25, scaleFactorOfFirstSize: 7 })).toBeNull();
    expect(identityForParameter(parameter, undefined, { ...aerosol, typeOfSizeInterval: 2 })).toBeNull();
    // The two optical depths on the same parameter are their own families.
    expect(identityForParameter(registry.aod!.parameter, undefined, registry.aod!.aerosol)?.family).toBe("aod");
    expect(identityForParameter(registry.aodsalt!.parameter, undefined, registry.aodsalt!.aerosol)?.family).toBe("aodsalt");
    expect(registry.aod!.parameter).toEqual(registry.aodsalt!.parameter);
    // PM2.5 is the fine parameter; the size cut alone does not make it PM10.
    const pm = registry.pm10!;
    expect(pm.parameter).toEqual(registry.pm10dust!.parameter);
    expect(identityForParameter(pm.parameter, undefined, registry.pm25!.aerosol)).toBeNull();
    expect(identityForParameter(registry.pm25!.parameter, undefined, pm.aerosol)).toBeNull();
    expect(identityForParameter(pm.parameter, undefined, { ...pm.aerosol, aerosolType: 62008 })).toBeNull();
    // A PM field whose wavelength interval is missing carries null limits;
    // one that spells them out is not the registered field.
    expect(pm.aerosol.typeOfWavelengthInterval).toBe(255);
    expect(pm.aerosol.scaledValueOfFirstWavelength).toBeNull();
    // Off the aerosol parameters the block changes nothing.
    const tmp2m: BundleParameter = {
      discipline: 0,
      parameterCategory: 0,
      parameterNumber: 0,
      typeOfFirstFixedSurface: 103,
      scaleFactorOfFirstFixedSurface: 0,
      scaledValueOfFirstFixedSurface: 2,
    };
    expect(identityForParameter(tmp2m, undefined, aerosol)).toEqual({ family: "tmp", level: null, vector: false });
  });

  it("is parsed with its aerosol block by the metadata validator", () => {
    for (const id of AEROSOL_IDS) {
      const parsed = parseBundleMetadata(metadata(bundleVariable(id)));
      expect(parsed.variables[0]!.aerosol).toEqual(registry[id]!.aerosol);
      expect(identifyBundle(parsed.variables)?.identity.family).toBe(id);
      expect(parseBundleMetadata(metadata(bundleVariable(id, "compact"))).variables[0]!.quantization.type).toBe("log1p");
    }
  });

  it("refuses a malformed aerosol block", () => {
    const good = bundleVariable("pm25");
    const withBlock = (aerosol: Record<string, unknown>) => metadata({ ...good, aerosol: aerosol as unknown as BundleAerosol });
    const block = good.aerosol!;
    // A block is its eleven keys and nothing else.
    expect(() => parseBundleMetadata(withBlock({ ...block, extra: 1 }))).toThrow(/eleven/);
    const { scaledValueOfSecondSize: _dropped, ...tenKeys } = block;
    expect(() => parseBundleMetadata(withBlock(tenKeys))).toThrow(/eleven/);
    // A code is a non-negative integer in its table's range.
    expect(() => parseBundleMetadata(withBlock({ ...block, aerosolType: -1 }))).toThrow(/aerosolType/);
    expect(() => parseBundleMetadata(withBlock({ ...block, aerosolType: 65536 }))).toThrow(/aerosolType/);
    expect(() => parseBundleMetadata(withBlock({ ...block, typeOfSizeInterval: 256 }))).toThrow(/typeOfSizeInterval/);
    expect(() => parseBundleMetadata(withBlock({ ...block, typeOfSizeInterval: "0" }))).toThrow(/typeOfSizeInterval/);
    // A limit pair is both integers or both null.
    expect(() => parseBundleMetadata(withBlock({ ...block, scaleFactorOfFirstSize: null }))).toThrow(/wholly/);
    expect(() => parseBundleMetadata(withBlock({ ...block, scaledValueOfFirstSize: null }))).toThrow(/wholly/);
    expect(() => parseBundleMetadata(withBlock({ ...block, scaleFactorOfFirstSize: 7.5 }))).toThrow(/scaleFactorOfFirstSize/);
    expect(() => parseBundleMetadata(withBlock({ ...block, scaleFactorOfFirstSize: 128 }))).toThrow(/scaleFactorOfFirstSize/);
    expect(() => parseBundleMetadata(withBlock({ ...block, scaledValueOfFirstSize: 0xffffffff }))).toThrow(/scaleFactorOfFirstSize/);
    expect(() => parseBundleMetadata(withBlock({ ...block, scaledValueOfFirstSize: -1 }))).toThrow(/scaleFactorOfFirstSize/);
    // A missing interval (255) carries no limits; a present one carries all four.
    expect(() => parseBundleMetadata(withBlock({ ...block, scaleFactorOfFirstWavelength: 9, scaledValueOfFirstWavelength: 545 }))).toThrow(
      /typeOfWavelengthInterval is missing but carries limits/,
    );
    expect(() => parseBundleMetadata(withBlock({ ...block, typeOfWavelengthInterval: 7 }))).toThrow(/scaleFactorOfFirstWavelength/);
    const odd = bundleVariable("aod");
    expect(() =>
      parseBundleMetadata(metadata({ ...odd, aerosol: { ...odd.aerosol!, typeOfSizeInterval: 255 } })),
    ).toThrow(/typeOfSizeInterval is missing but carries limits/);
    // Never null, and never below schema version 3.
    expect(() => parseBundleMetadata(metadata({ ...good, aerosol: null as unknown as BundleAerosol }))).toThrow();
    const legacy = JSON.parse(metadata(good)) as { schemaVersion: number; variables: Record<string, unknown>[] };
    legacy.schemaVersion = 2;
    delete legacy.variables[0]!.parameter;
    expect(() => parseBundleMetadata(JSON.stringify(legacy))).toThrow(/schema version 3/);
  });
});

describe("the aerosol families", () => {
  it("puts the six optical depths behind one tile and the three particulates behind another", () => {
    expect(FAMILIES.aod.surface).toBe("aod");
    expect(FAMILIES.pm.surface).toBe("pm25");
    for (const id of ODD_IDS) expect(familyOf(id)).toBe("aod");
    for (const id of PM_IDS) expect(familyOf(id)).toBe("pm");
    // Variants, never levels: the level row has nothing to offer, the
    // sheet's chips carry the species and size cuts.
    expect(familyLevels("aod")).toEqual(["aod"]);
    expect(familyLevels("pm")).toEqual(["pm25"]);
    expect(familyVariants("aod")).toEqual([...ODD_IDS]);
    expect(familyVariants("pm")).toEqual([...PM_IDS]);
    expect(familyMembers("aod")).toEqual([...ODD_IDS]);
    expect(familyMembers("pm")).toEqual([...PM_IDS]);
    expect(levelCode("aoddust")).toBe("DUST");
    expect(levelCode("pm10dust")).toBe("10 DUST");
    expect(levelCode("pm25")).toBe("2.5");
    for (const id of AEROSOL_IDS) expect(variableSpec(id)!.family).toBe(familyOf(id));
  });

  it("labels each member in words and by its code", () => {
    expect(variableSpec("aod")!.label()).toBe("Aerosol optical depth at 550 nm");
    expect(variableSpec("aodbc")!.label()).toBe("Black carbon optical depth");
    expect(variableSpec("pm25")!.label()).toBe("PM2.5 surface concentration");
    expect(variableSpec("pm10dust")!.label()).toBe("Dust PM10 surface concentration");
    expect(variableSpec("aod")!.code).toBe("AOD 550");
    expect(variableSpec("pm10")!.code).toBe("PM10 SFC");
    expect(variableSpec("aod")!.title.join(" ")).toBe("Aerosol Optical Depth");
    for (const id of AEROSOL_IDS) {
      expect(variableSpec(id)!.legendGradient).toBe("palette");
      expect(variableSpec(id)!.legendKey).toBeNull();
    }
  });

  it("is what the aerosol run's rail carries", () => {
    expect(FORECAST_MODELS.gefsaero.railCore).toEqual(["aod", "pm25"]);
    expect(FORECAST_MODELS.gefsaero.defaultVariable).toBe("aod");
    expect(FORECAST_MODELS.gefsaero.coreBundles).toEqual(["aod"]);
    expect(parseModelFromSearch("?model=gefsaero")).toBe("gefsaero");
    expect(parseModelFromSearch("?model=GEFS-Aerosols")).toBe("gefsaero");
  });

  it("is reachable by every spelling", () => {
    expect(parseVariableFromSearch("?type=aod")).toBe("aod");
    expect(parseVariableFromSearch("?type=aerosol")).toBe("aod");
    expect(parseVariableFromSearch("?type=AOD550")).toBe("aod");
    expect(parseVariableFromSearch("?type=aoddust")).toBe("aoddust");
    expect(parseVariableFromSearch("?type=smoke")).toBe("aodorg");
    expect(parseVariableFromSearch("?type=blackcarbon")).toBe("aodbc");
    expect(parseVariableFromSearch("?type=pm25")).toBe("pm25");
    expect(parseVariableFromSearch("?type=pm2p5")).toBe("pm25");
    expect(parseVariableFromSearch("?type=airquality")).toBe("pm25");
    expect(parseVariableFromSearch("?type=pm10")).toBe("pm10");
    expect(parseVariableFromSearch("?type=pm10dust")).toBe("pm10dust");
    // The Dust RGB keeps `dust`.
    expect(parseVariableFromSearch("?type=dust")).toBe("dustrgb");
    expect(searchForVariable("aod", "")).toContain("type=aod");
    expect(searchForVariable("pm25", "")).toContain("type=pm25");
  });

  it("gives the surface particulates a meteogram row, and the column its own", () => {
    const rows = meteogramRows((id) => (AEROSOL_IDS as readonly string[]).includes(id));
    expect(rows.map((row) => row.id)).toEqual(["particulate", "aod"]);
    expect(rows[0]!.bundles).toEqual(["pm25", "pm10"]);
    expect(rows[0]!.baseline).toBe(0);
    expect(rows[1]!.bundles).toEqual(["aod"]);
    expect(meteogramRows((id) => id === "pm10dust")).toEqual([]);
  });
});

describe("the aerosol charts", () => {
  it("copy the encoders' logarithmic codebooks", () => {
    for (const id of AEROSOL_IDS) {
      const chart = AEROSOL_CHARTS[id];
      const { quality, compact } = registry[id]!;
      expect(quality).toMatchObject({ type: "log1p", trace: chart.trace, scale: chart.scale, maximum: chart.maximum });
      expect(compact).toMatchObject({ type: "log1p", trace: chart.trace, scale: chart.scale, maximum: chart.maximum });
      expect(quality).toMatchObject({ minimumCode: 1, maximumCode: 253, zeroCode: 0, overflowCode: 254, nodataCode: 255 });
      expect(compact).toMatchObject({ minimumCode: 1, maximumCode: 125, zeroCode: 0, overflowCode: 126, nodataCode: 127 });
      expect(chart.chartMax).toBeLessThanOrEqual(chart.maximum);
    }
    expect(registry.aod!.unit).toBe("1");
    expect(registry.pm25!.unit).toBe("µg/m³");
    // An optical depth is dimensionless and shows no unit.
    expect(displayUnit("1")).toBe("");
  });

  it("decode and encode each other's codes", () => {
    for (const id of AEROSOL_IDS) {
      const variable = bundleVariable(id);
      const quantization = variable.quantization as LogQuantization;
      expect(decodeValue(variable, 0)).toBe(0);
      expect(decodeValue(variable, 255)).toBeNull();
      expect(decodeValue(variable, 253)).toBeCloseTo(quantization.maximum, 6);
      for (const code of [1, 17, 64, 128, 200, 253]) {
        expect(encodeLog(quantization, decodeValue(variable, code)!)).toBe(code);
      }
      expect(encodeLog(quantization, 0)).toBe(0);
      expect(encodeLog(quantization, quantization.maximum * 2)).toBe(254);
    }
  });

  it("read over the chart ceiling with ticks where the log bar puts them", () => {
    expect(scalarLegendRange({ family: "aod", level: null, vector: false })).toEqual([0, 5]);
    expect(scalarLegendRange({ family: "pm25", level: null, vector: false })).toEqual([0, 500]);
    expect(scalarLegendRange({ family: "pm10dust", level: null, vector: false })).toEqual([0, 1000]);
    expect(isobaricLegend({ family: "aod", level: null, vector: false })).toEqual(["5", "2", "0.8", "0.3", "0.09", "0"]);
    expect(isobaricLegend({ family: "pm25", level: null, vector: false })).toEqual(["500", "200", "80", "30", "9", "0"]);
    expect(isobaricLegend({ family: "pm10", level: null, vector: false })).toEqual(["1000", "350", "120", "40", "10", "0"]);
    expect(variableSpec("aodsulf")!.legend()).toEqual(logLegend(AEROSOL_CHARTS.aod));
    // Each tick sits at the bar's even division: the value at the code that
    // fraction of the way from zero to the ceiling.
    const quantization = registry.aod!.quality;
    const ticks = logLegend(AEROSOL_CHARTS.aod).map(Number);
    const top = encodeLog(quantization, 5);
    for (const [index, tick] of ticks.entries()) {
      const code = Math.round(top * (1 - index / 5));
      const value = code === 0 ? 0 : decodeValue(bundleVariable("aod"), code)!;
      expect(Math.abs(tick - value)).toBeLessThanOrEqual(Math.max(0.011, value * 0.2));
    }
  });

  it("paint clean air as nothing and haze deeper the thicker", () => {
    const aod = bundleVariable("aod");
    const palette = buildPalette(aod);
    expect(rgba(palette, 0)[3]).toBe(0);
    expect(rgba(palette, 255)[3]).toBe(0);
    const at = (value: number) => rgba(palette, encodeLog(aod.quantization as LogQuantization, value));
    expect(at(0.02)[3]).toBeLessThan(60);
    expect(at(0.2)[3]).toBeGreaterThan(120);
    expect(at(1)[3]).toBeGreaterThan(200);
    expect(at(1)[0]).toBeLessThan(at(0.2)[0]);
    expect(at(5)[3]).toBe(255);
    // The species share the column's ramp.
    expect(buildPalette(bundleVariable("aodbc"))).toEqual(palette);
    expect(buildPalette(bundleVariable("aodsalt"))).toEqual(palette);
  });

  it("paint the particulates in the air quality index's bands", () => {
    const pm25 = bundleVariable("pm25");
    const palette = buildPalette(pm25);
    expect(rgba(palette, 0)[3]).toBe(0);
    const at = (value: number) => rgba(palette, encodeLog(pm25.quantization as LogQuantization, value));
    const hue = ([r, g, b]: number[]) => (g! > r! && g! > b! ? "green" : r! > 200 && g! > 200 ? "yellow" : r! > 200 && g! > 100 ? "orange" : r! > 200 ? "red" : b! > r! ? "purple" : "maroon");
    expect(hue(at(8))).toBe("green");
    expect(hue(at(35))).toBe("yellow");
    expect(hue(at(55))).toBe("orange");
    expect(hue(at(150))).toBe("red");
    expect(hue(at(250))).toBe("purple");
    expect(hue(at(500))).toBe("maroon");
    expect(at(500)[3]).toBe(255);
    // PM10 reads on its own breakpoints: 35 µg/m³ is still good air.
    const pm10 = bundleVariable("pm10");
    const coarse = buildPalette(pm10);
    const coarseAt = (value: number) => rgba(coarse, encodeLog(pm10.quantization as LogQuantization, value));
    expect(hue(coarseAt(35))).toBe("green");
    expect(hue(coarseAt(154))).toBe("yellow");
    expect(hue(coarseAt(354))).toBe("red");
    expect(buildPalette(bundleVariable("pm10dust"))).toEqual(coarse);
  });
});

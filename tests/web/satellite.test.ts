import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/satellite-registry.json";

import {
  identifyBundle,
  identityForBundleId,
  identityForParameter,
  identityForProducedScalar,
  identityForProducedTriple,
  isCompositeIdentity,
  registeredBundleId,
} from "../../web/src/identity";
import { BRIGHTNESS_TEMPERATURE_CHART_RANGE, ISOBARIC_FAMILIES, familyOf, isobaricLegend, scalarLegendRange } from "../../web/src/levels";
import { displayUnit, displayValue } from "../../web/src/units";
import {
  COMPOSITE_BUNDLES,
  FORECAST_MODELS,
  KNOWN_BUNDLE_IDS,
  SATELLITE_IDS,
  compositeComponents,
  isCompositeBundle,
  isVectorBundle,
  parseBundleMetadata,
  type BundleBand,
  type BundleParameter,
  type BundleProducer,
  type BundleVariable,
  type LinearQuantization,
} from "../../web/src/manifest";
import { meteogramRowCode, meteogramRows } from "../../web/src/meteogram";
import { buildPalette, decodeValue } from "../../web/src/palettes";
import { parseVariableFromSearch, searchForVariable } from "../../web/src/urlstate";
import { variableSpec } from "../../web/src/variables";

/** The committed registry both encoders are held to
 * (`tests/test_satellite.py`, and the Rust encoder's unit tests): one entry
 * per data variable under `variables` — the channels with their `band` per
 * platform, the composite's guns and the produced scalar with their
 * `producer` id — and the produced bundles' component lists under
 * `bundles` (the composite's three guns, the confidence's one variable). */
interface RegistryEntry {
  label: string;
  unit: string;
  parameter: BundleParameter;
  band?: Record<string, BundleBand>;
  producer?: { id: string };
  quality: LinearQuantization;
  compact: LinearQuantization;
}

const registryFile = registryJson as unknown as { variables: Record<string, RegistryEntry>; bundles: Record<string, string[]> };
const registry = registryFile.variables;

function bundleVariable(id: string, band: BundleBand | undefined = registry[id]!.band?.himawari): BundleVariable {
  const entry = registry[id]!;
  return {
    numericId: 1,
    id,
    label: entry.label,
    unit: entry.unit,
    parameter: entry.parameter,
    band,
    quantization: entry.quality,
  };
}

/** The guns' contract, written out so the composite is held to it whether
 * or not the registry on disk carries the guns yet: shachen's local numbers
 * 1, 2, 3 in the space-products discipline, at the top of the atmosphere,
 * a linear codebook whose code 0 is no data and code 251 is 1.0. */
const GUN_IDS = ["dustr", "dustg", "dustb"] as const;
const GUN_QUANTIZATION: LinearQuantization = { type: "linear", offset: -0.004, scale: 0.004, minimumCode: 0, maximumCode: 251, nodataCode: 255 };
const PRODUCER: BundleProducer = { id: "shachen", version: "0.3.0" };

function gunParameter(number: number): BundleParameter {
  return {
    discipline: 3,
    parameterCategory: 192,
    parameterNumber: number,
    typeOfFirstFixedSurface: 8,
    scaleFactorOfFirstFixedSurface: null,
    scaledValueOfFirstFixedSurface: null,
  };
}

function gunVariables(order: readonly number[] = [1, 2, 3], producer: BundleProducer = PRODUCER): BundleVariable[] {
  return order.map((number, at) => ({
    numericId: at + 1,
    id: GUN_IDS[number - 1]!,
    label: `Dust RGB, ${["red", "green", "blue"][number - 1]} gun`,
    unit: "1",
    parameter: gunParameter(number),
    producer,
    quantization: GUN_QUANTIZATION,
  }));
}

const DUST_IDENTITY = { family: "dustrgb" as const, level: null, vector: false };

/** The confidence's contract: shachen's local number 4 beside the guns,
 * one variable of its own bundle, the guns' codebook (code 0 no data,
 * code 1 = 0.0, code 251 = 1.0). */
const CONFIDENCE_IDENTITY = { family: "dustcf" as const, level: null, vector: false };

/** The confidence's variable; `null` for a producer leaves the block off. */
function confidenceVariable(producer: BundleProducer | null = PRODUCER): BundleVariable {
  return {
    numericId: 1,
    id: "dustcf",
    label: "DEBRA dust confidence",
    unit: "1",
    parameter: gunParameter(4),
    ...(producer === null ? {} : { producer }),
    quantization: GUN_QUANTIZATION,
  };
}

function rgba(palette: Uint8Array, code: number): [number, number, number, number] {
  return [...palette.subarray(code * 4, code * 4 + 4)] as [number, number, number, number];
}

describe("the satellite registry", () => {
  it("knows the bundles the encoders register", () => {
    // Every registry entry is a channel (a brightness temperature with a
    // band per platform) or a produced field — a gun of the composite, or
    // the dust confidence, the next local number under the same producer;
    // the shell's bundle ids are the published channel, the composite and
    // the confidence.
    for (const [id, entry] of Object.entries(registry)) {
      if (entry.producer) {
        expect([...GUN_IDS, "dustcf"]).toContain(id);
        expect(entry.producer).toEqual({ id: "shachen" });
        const number = id === "dustcf" ? 4 : GUN_IDS.indexOf(id as (typeof GUN_IDS)[number]) + 1;
        expect(entry.parameter).toEqual(gunParameter(number));
        expect(entry.unit).toBe("1");
        expect(entry.quality).toEqual(GUN_QUANTIZATION);
        expect(entry.compact).toEqual(GUN_QUANTIZATION);
      } else {
        expect(entry.parameter).toMatchObject({ discipline: 0, parameterCategory: 4, parameterNumber: 4, typeOfFirstFixedSurface: 8 });
        expect(entry.unit).toBe("K");
        expect(entry.band?.himawari).toBeDefined();
      }
    }
    expect(Object.keys(registry)).toContain("ir104");
    expect(Object.keys(registry)).toContain("dustcf");
    // The composite's components, in bundle order, are the registry's;
    // the confidence bundle is its one variable and no composite.
    const { dustcf: confidenceBundle, ...compositeBundles } = registryFile.bundles;
    expect(COMPOSITE_BUNDLES).toEqual(compositeBundles);
    expect(confidenceBundle).toEqual(["dustcf"]);
    expect(SATELLITE_IDS).toEqual(["ir104", "dustrgb", "dustcf"]);
    for (const id of SATELLITE_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
      // Three single fields, each with a tile of its own, in the sheet's
      // satellite group.
      expect(familyOf(id)).toBeNull();
      expect(variableSpec(id)?.family).toBeNull();
      expect(variableSpec(id)?.group).toBe("satellite");
    }
    expect(isCompositeBundle("dustrgb")).toBe(true);
    expect(isCompositeBundle("ir104")).toBe(false);
    expect(isCompositeBundle("dustcf")).toBe(false);
    expect(compositeComponents("dustcf")).toBeNull();
    expect(compositeComponents("dustrgb")).toEqual(GUN_IDS);
    expect(COMPOSITE_BUNDLES.dustrgb).toEqual(GUN_IDS);
  });

  it("gives the composite and the confidence core tiles beside the infrared on every satellite source", () => {
    // No family: the three are different pictures, and a source with three
    // fields has room for three tiles, so none hides behind another. The
    // confidence ships on the ten-minute disks and on the mosaic (a member
    // whose run lacks the bundle empties for it, `applyMosaicMembers`),
    // not on Meteosat's hourly cycle.
    expect(ISOBARIC_FAMILIES).not.toContain("satellite");
    expect(variableSpec("dustrgb")?.code).toBe("DUST RGB");
    expect(variableSpec("dustcf")?.code).toBe("DEBRA");
    for (const model of ["himawari", "goeseast", "goeswest", "geo"] as const) {
      expect(FORECAST_MODELS[model].railCore).toEqual(["ir104", "dustrgb", "dustcf"]);
    }
    expect(FORECAST_MODELS.meteosat.railCore).toEqual(["ir104", "dustrgb"]);
  });

  it("reads the channel off the band block, not the parameter alone", () => {
    const { parameter, band } = registry.ir104!;
    const himawari = band!.himawari as BundleBand;
    // Brightness temperature at the top of the atmosphere is one parameter
    // for every infrared band; the band's wave number says which channel.
    expect(parameter).toMatchObject({ discipline: 0, parameterCategory: 4, parameterNumber: 4, typeOfFirstFixedSurface: 8 });
    expect(identityForParameter(parameter, himawari)).toEqual({ family: "ir104", level: null, vector: false });
    expect(identityForParameter(parameter)).toBeNull();
    // GOES-19's ABI channel 13 is centred at 10.35 µm: the same chart.
    const abi: BundleBand = { ...himawari, satelliteNumber: 273, instrumentType: 617, scaledValueOfCentralWaveNumber: 96618 };
    expect(identityForParameter(parameter, abi)).toEqual({ family: "ir104", level: null, vector: false });
    // A water vapour band (6.2 µm) is not this channel, and renders
    // generically until it ships with a chart of its own.
    const wv: BundleBand = { ...himawari, scaledValueOfCentralWaveNumber: 160182 };
    expect(identityForParameter(parameter, wv)).toBeNull();
    expect(identifyBundle([bundleVariable("ir104")])?.identity).toEqual({ family: "ir104", level: null, vector: false });
    expect(identifyBundle([bundleVariable("ir104", wv)])).toBeNull();
    expect(identityForBundleId("ir104")).toEqual({ family: "ir104", level: null, vector: false });
    expect(registeredBundleId({ family: "ir104", level: null, vector: false })).toBe("ir104");
  });

  it("is parsed with its band by the metadata validator", () => {
    const metadata = {
      schemaVersion: 3,
      model: "HIMAWARI",
      runTime: "2026-09-17T03:00:00Z",
      time: { unitSeconds: 600, firstFrameOffset: 0, frameCount: 2, frameStep: 1 },
      grid: { width: 3000, height: 3000, firstLongitude: 80.72, firstLatitude: 59.98, longitudeStep: 0.04, latitudeStep: -0.04, wrapLongitude: false },
      variables: [bundleVariable("ir104")],
    };
    const parsed = parseBundleMetadata(JSON.stringify(metadata));
    expect(parsed.variables[0]!.band?.satelliteNumber).toBe(174);
    expect(identifyBundle(parsed.variables)?.identity.family).toBe("ir104");
  });

  it("paints the disk's edge as nothing and cloud tops bright and cold in colour", () => {
    const variable = bundleVariable("ir104");
    const palette = buildPalette(variable);
    // The codebook bottom is the fill outside the disk.
    expect(decodeValue(variable, 0)).toBe(180);
    expect(rgba(palette, 0)[3]).toBe(0);
    // Warm clear sea near 300 K is dark and opaque; a mid-level cloud near
    // -20 °C is light grey; the enhancement starts near -30 °C and a
    // convective top near -60 °C is saturated colour.
    const at = (kelvin: number) => rgba(palette, Math.round((kelvin - 180) / 0.6));
    const saturation = ([r, g, b]: number[]) => Math.max(r!, g!, b!) - Math.min(r!, g!, b!);
    expect(at(300)[3]).toBe(255);
    expect(at(300)[0]).toBeLessThan(80);
    expect(at(253)[0]).toBeGreaterThan(200);
    expect(saturation(at(253))).toBeLessThan(10);
    expect(saturation(at(240))).toBeGreaterThan(60);
    expect(at(213)[3]).toBe(255);
    expect(saturation(at(213))).toBeGreaterThan(150);
    expect(decodeValue(variable, 253)).toBeCloseTo(331.8, 6);
    expect(decodeValue(variable, 255)).toBeNull();
  });

  it("keeps the file in kelvin and reads it in Celsius", () => {
    // The legend bar spans the chart range in the file's unit; its ticks
    // and every readout are Celsius, the shell's temperature unit.
    const identity = { family: "ir104" as const, level: null, vector: false };
    expect(scalarLegendRange(identity)).toEqual(BRIGHTNESS_TEMPERATURE_CHART_RANGE);
    expect(BRIGHTNESS_TEMPERATURE_CHART_RANGE).toEqual([183.15, 333.15]);
    expect(isobaricLegend(identity)).toEqual(["60", "30", "0", "-30", "-60", "-90"]);
    expect(variableSpec("ir104")?.legend()).toEqual(["60", "30", "0", "-30", "-60", "-90"]);
    expect(variableSpec("ir104")?.code).toBe("IR 10.4");
    expect(registry.ir104!.unit).toBe("K");
    expect(displayUnit("K")).toBe("°C");
    expect(displayValue("K", 223.15)).toBeCloseTo(-50, 9);
    // Every other unit passes through untouched.
    expect(displayUnit("mm/h")).toBe("mm/h");
    expect(displayValue("°C", 21.5)).toBe(21.5);
  });

  it("is reached by its own type names", () => {
    expect(parseVariableFromSearch("?type=infrared")).toBe("ir104");
    expect(parseVariableFromSearch("?type=ir")).toBe("ir104");
    expect(parseVariableFromSearch("?type=satellite")).toBe("ir104");
    expect(parseVariableFromSearch("?type=ir104")).toBe("ir104");
    expect(searchForVariable("ir104", "")).toBe("?model=gfs&type=infrared");
  });

  it("fills the cloud-top row of the meteogram", () => {
    const rows = meteogramRows((id) => id === "ir104");
    expect(rows.map((row) => row.id)).toEqual(["cloudtop"]);
    expect(rows[0]!.bundles).toEqual(["ir104"]);
    expect(meteogramRowCode(rows[0]!)).toBe("IR");
    // The composite reads at no pin: no row of its own; nor does the
    // confidence, a diagnostic with no place among the meteogram's rows.
    expect(meteogramRows((id) => id === "dustrgb")).toEqual([]);
    expect(variableSpec("dustrgb")?.meteogramCode).toBeNull();
    expect(meteogramRows((id) => id === "dustcf")).toEqual([]);
    expect(variableSpec("dustcf")?.meteogramCode).toBeNull();
  });
});

describe("the Dust RGB composite", () => {
  it("is named by its producer and the guns' local numbers together", () => {
    // Three guns, red, green, blue, all shachen's: the composite.
    expect(identityForProducedTriple(gunVariables())).toEqual(DUST_IDENTITY);
    expect(isCompositeIdentity(DUST_IDENTITY)).toBe(true);
    expect(isCompositeIdentity({ family: "ir104", level: null, vector: false })).toBe(false);
    expect(isCompositeIdentity({ family: "wind", level: null, vector: true })).toBe(false);
    // The order is the file's: the encoders number the guns 1, 2, 3, and a
    // file that did not could not be drawn as a picture.
    expect(identityForProducedTriple(gunVariables([3, 2, 1]))).toBeNull();
    // Local numbers mean nothing under another producer.
    expect(identityForProducedTriple(gunVariables([1, 2, 3], { id: "other", version: "1" }))).toBeNull();
    // Nor without one: a local-range parameter on its own is unknown.
    expect(identityForProducedTriple(gunVariables().map(({ producer: _, ...rest }) => rest))).toBeNull();
    expect(identityForParameter(gunParameter(1))).toBeNull();
    // The version is not part of the identity.
    expect(identityForProducedTriple(gunVariables([1, 2, 3], { id: "shachen", version: "9.9.9" }))).toEqual(DUST_IDENTITY);
    // A gun on another surface is another thing.
    const mixed = gunVariables();
    mixed[2] = { ...mixed[2]!, parameter: { ...mixed[2]!.parameter!, typeOfFirstFixedSurface: 1 } };
    expect(identityForProducedTriple(mixed)).toBeNull();
  });

  it("is the whole bundle, in red, green, blue order", () => {
    const bundle = identifyBundle(gunVariables());
    expect(bundle?.identity).toEqual(DUST_IDENTITY);
    expect(bundle?.variables.map((variable) => variable.id)).toEqual([...GUN_IDS]);
    expect(registeredBundleId(DUST_IDENTITY)).toBe("dustrgb");
    expect(identityForBundleId("dustrgb")).toEqual(DUST_IDENTITY);
    // Two guns are not a composite, nor a pair this shell knows: the file
    // reads as whatever its first variable is, which is nothing.
    expect(identifyBundle(gunVariables().slice(0, 2))).toBeNull();
  });

  it("is parsed with its producer by the metadata validator", () => {
    const metadata = {
      schemaVersion: 3,
      model: "HIMAWARI",
      runTime: "2026-09-17T03:00:00Z",
      time: { unitSeconds: 600, firstFrameOffset: 0, frameCount: 2, frameStep: 1 },
      grid: { width: 3000, height: 3000, firstLongitude: 80.72, firstLatitude: 59.98, longitudeStep: 0.04, latitudeStep: -0.04, wrapLongitude: false },
      variables: gunVariables(),
    };
    const parsed = parseBundleMetadata(JSON.stringify(metadata));
    expect(parsed.variables.length).toBe(3);
    expect(parsed.variables[0]!.producer).toEqual(PRODUCER);
    expect(identifyBundle(parsed.variables)?.identity).toEqual(DUST_IDENTITY);
  });

  it("keeps code 0 as no data and reads the guns in [0, 1]", () => {
    const [red] = gunVariables();
    expect(decodeValue(red!, 0)).toBeCloseTo(-0.004, 9);
    expect(decodeValue(red!, 1)).toBeCloseTo(0, 9);
    expect(decodeValue(red!, 126)).toBeCloseTo(0.5, 9);
    expect(decodeValue(red!, 251)).toBeCloseTo(1, 9);
    expect(decodeValue(red!, 255)).toBeNull();
  });

  it("carries a key of swatches in place of a legend", () => {
    const spec = variableSpec("dustrgb")!;
    expect(spec.legend()).toEqual([]);
    const key = spec.legendKey?.() ?? [];
    expect(key.length).toBe(7);
    expect(key[0]!.label).toBe("Dust");
    for (const swatch of key) {
      expect(swatch.color).toMatch(/^#[0-9a-f]{6}$/);
      expect(swatch.label.length).toBeGreaterThan(0);
    }
    // Every other field has a bar.
    expect(variableSpec("ir104")?.legendKey).toBeNull();
    expect(variableSpec("tmp2m")?.legendKey).toBeNull();
    expect(spec.title.join(" ")).toBe("Dust RGB");
    expect(spec.label()).toBe("Dust RGB (infrared composite)");
    expect(spec.ground).toBe("slate");
  });

  it("is reached by its own type names", () => {
    expect(parseVariableFromSearch("?type=dustrgb")).toBe("dustrgb");
    expect(parseVariableFromSearch("?type=dust")).toBe("dustrgb");
    expect(searchForVariable("dustrgb", "")).toBe("?model=gfs&type=dustrgb");
  });
});

describe("the DEBRA dust confidence", () => {
  it("is named by its producer and its local number together, as one variable", () => {
    // A produced scalar: shachen's local number 4 beside the guns.
    expect(identityForProducedScalar(confidenceVariable())).toEqual(CONFIDENCE_IDENTITY);
    expect(isCompositeIdentity(CONFIDENCE_IDENTITY)).toBe(false);
    // The version is not part of the identity.
    expect(identityForProducedScalar(confidenceVariable({ id: "shachen", version: "9.9.9" }))).toEqual(CONFIDENCE_IDENTITY);
    // Local numbers mean nothing under another producer, and nothing
    // without one: a local-range parameter on its own is unknown, and the
    // WMO table has no row for it either.
    expect(identityForProducedScalar(confidenceVariable({ id: "other", version: "1" }))).toBeNull();
    expect(identityForProducedScalar(confidenceVariable(null))).toBeNull();
    expect(identityForParameter(gunParameter(4))).toBeNull();
    // A gun's number is a gun, not a scalar of its own; a WMO parameter is
    // not the producer's to name.
    expect(identityForProducedScalar({ parameter: gunParameter(1), producer: PRODUCER })).toBeNull();
    expect(identityForProducedScalar({ parameter: registry.ir104!.parameter, producer: PRODUCER })).toBeNull();
    expect(identityForProducedScalar({ producer: PRODUCER })).toBeNull();
  });

  it("is the whole one-variable bundle, and unknown without its producer", () => {
    const bundle = identifyBundle([confidenceVariable()]);
    expect(bundle?.identity).toEqual(CONFIDENCE_IDENTITY);
    expect(bundle?.variables.map((variable) => variable.id)).toEqual(["dustcf"]);
    expect(identifyBundle([confidenceVariable(null)])).toBeNull();
    expect(identifyBundle([confidenceVariable({ id: "other", version: "1" })])).toBeNull();
    expect(registeredBundleId(CONFIDENCE_IDENTITY)).toBe("dustcf");
    expect(identityForBundleId("dustcf")).toEqual(CONFIDENCE_IDENTITY);
    // The other one-variable bundles still read off the WMO table.
    expect(identifyBundle([bundleVariable("ir104")])?.identity).toEqual({ family: "ir104", level: null, vector: false });
  });

  it("is parsed with its producer by the metadata validator", () => {
    const metadata = {
      schemaVersion: 3,
      model: "HIMAWARI",
      runTime: "2026-09-17T03:00:00Z",
      time: { unitSeconds: 600, firstFrameOffset: 0, frameCount: 2, frameStep: 1 },
      grid: { width: 3000, height: 3000, firstLongitude: 80.72, firstLatitude: 59.98, longitudeStep: 0.04, latitudeStep: -0.04, wrapLongitude: false },
      variables: [confidenceVariable()],
    };
    const parsed = parseBundleMetadata(JSON.stringify(metadata));
    expect(parsed.variables.length).toBe(1);
    expect(parsed.variables[0]!.producer).toEqual(PRODUCER);
    expect(parsed.variables[0]!.band).toBeUndefined();
    expect(identifyBundle(parsed.variables)?.identity).toEqual(CONFIDENCE_IDENTITY);
  });

  it("paints nothing below the noise floor and dust in deepening yellow", () => {
    const variable = confidenceVariable();
    expect(decodeValue(variable, 0)).toBeCloseTo(-0.004, 9);
    expect(decodeValue(variable, 1)).toBeCloseTo(0, 9);
    expect(decodeValue(variable, 251)).toBeCloseTo(1, 9);
    expect(decodeValue(variable, 255)).toBeNull();
    expect(variableSpec("dustcf")?.floorIsNoData).toBe(true);
    const palette = buildPalette(variable);
    const at = (confidence: number) => rgba(palette, Math.round((confidence + 0.004) / 0.004));
    // Code 0 (no data) and every cell under 0.1 are the map.
    expect(rgba(palette, 0)[3]).toBe(0);
    expect(at(0)[3]).toBe(0);
    expect(at(0.1)[3]).toBe(0);
    // From there the yellow thickens with the confidence, orange at the top.
    const [r3, g3, b3, a3] = at(0.3);
    expect(a3).toBeGreaterThan(100);
    expect(a3).toBeLessThan(255);
    expect(r3).toBeGreaterThan(240);
    expect(g3).toBeGreaterThan(200);
    expect(b3).toBeLessThan(140);
    const [r6, g6, b6, a6] = at(0.6);
    expect(a6).toBeGreaterThan(a3);
    expect(r6).toBe(255);
    expect(g6).toBeGreaterThan(150);
    expect(b6).toBeLessThan(20);
    const [r1, g1, b1, a1] = at(1);
    expect(a1).toBe(255);
    expect(r1).toBe(255);
    expect(g1).toBeLessThan(g6);
    expect(b1).toBe(0);
    expect(rgba(palette, 255)[3]).toBe(0);
  });

  it("reads over [0, 1] with a bar of its own", () => {
    expect(scalarLegendRange(CONFIDENCE_IDENTITY)).toEqual([0, 1]);
    expect(isobaricLegend(CONFIDENCE_IDENTITY)).toEqual(["1", "0.8", "0.6", "0.4", "0.2", "0"]);
    const spec = variableSpec("dustcf")!;
    expect(spec.legend()).toEqual(["1", "0.8", "0.6", "0.4", "0.2", "0"]);
    expect(spec.legendKey).toBeNull();
    expect(spec.legendGradient).toBe("palette");
    expect(spec.title.join(" ")).toBe("Dust Confidence");
    expect(spec.label()).toBe("DEBRA dust confidence");
    expect(spec.ground).toBe("slate");
    expect(spec.showcaseCode).toBe("DEBRA");
    expect(spec.group).toBe("satellite");
    expect(registry.dustcf!.unit).toBe("1");
    // A dimensionless quantity shows no unit at all: "0.24", not "0.24 1".
    expect(displayUnit("1")).toBe("");
  });

  it("is reached by its own type names", () => {
    expect(parseVariableFromSearch("?type=debra")).toBe("dustcf");
    expect(parseVariableFromSearch("?type=dustcf")).toBe("dustcf");
    expect(parseVariableFromSearch("?type=dustconfidence")).toBe("dustcf");
    expect(searchForVariable("dustcf", "")).toBe("?model=gfs&type=debra");
  });
});

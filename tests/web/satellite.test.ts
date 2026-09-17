import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/satellite-registry.json";

import {
  identifyBundle,
  identityForBundleId,
  identityForParameter,
  identityForProducedTriple,
  isCompositeIdentity,
  registeredBundleId,
} from "../../web/src/identity";
import { BRIGHTNESS_TEMPERATURE_CHART_RANGE, FAMILIES, familyMembers, familyOf, familyVariants, isobaricLegend, levelCode, scalarLegendRange } from "../../web/src/levels";
import { displayUnit, displayValue } from "../../web/src/units";
import {
  COMPOSITE_BUNDLES,
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
 * platform, the composite's guns with their `producer` id — and the
 * composite bundles' component lists under `bundles`. */
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

function rgba(palette: Uint8Array, code: number): [number, number, number, number] {
  return [...palette.subarray(code * 4, code * 4 + 4)] as [number, number, number, number];
}

describe("the satellite registry", () => {
  it("knows the bundles the encoders register", () => {
    // Every registry entry is a channel (a brightness temperature with a
    // band per platform) or a gun of the composite (a produced field); the
    // shell's bundle ids are the published channel and the composite.
    for (const [id, entry] of Object.entries(registry)) {
      if (entry.producer) {
        expect(GUN_IDS).toContain(id);
        expect(entry.producer).toEqual({ id: "shachen" });
        expect(entry.parameter).toEqual(gunParameter(GUN_IDS.indexOf(id as (typeof GUN_IDS)[number]) + 1));
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
    // The composite's components, in bundle order, are the registry's.
    expect(COMPOSITE_BUNDLES).toEqual(registryFile.bundles);
    expect(SATELLITE_IDS).toEqual(["ir104", "dustrgb"]);
    for (const id of SATELLITE_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
      // Both behind the satellite family's one tile, in the sheet's
      // satellite group.
      expect(familyOf(id)).toBe("satellite");
      expect(variableSpec(id)?.group).toBe("satellite");
    }
    expect(isCompositeBundle("dustrgb")).toBe(true);
    expect(isCompositeBundle("ir104")).toBe(false);
    expect(compositeComponents("dustrgb")).toEqual(GUN_IDS);
    expect(COMPOSITE_BUNDLES.dustrgb).toEqual(GUN_IDS);
  });

  it("puts the composite behind the infrared tile as its variant", () => {
    expect(FAMILIES.satellite.surface).toBe("ir104");
    expect(familyVariants("satellite")).toEqual(["ir104", "dustrgb"]);
    expect(familyMembers("satellite")).toEqual(["ir104", "dustrgb"]);
    // A run that ships the infrared alone offers no cycle; one that ships
    // both cycles between the two.
    expect(familyVariants("satellite", (id) => id === "ir104")).toEqual(["ir104", "dustrgb"]);
    expect(levelCode("dustrgb")).toBe("DUST RGB");
    expect(levelCode("ir104")).toBe("IR 10.4");
    expect(variableSpec("dustrgb")?.family).toBe("satellite");
    expect(variableSpec("dustrgb")?.code).toBe("DUST RGB");
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
    // The composite reads at no pin: no row of its own.
    expect(meteogramRows((id) => id === "dustrgb")).toEqual([]);
    expect(variableSpec("dustrgb")?.meteogramCode).toBeNull();
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

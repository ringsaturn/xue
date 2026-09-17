import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/satellite-registry.json";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId } from "../../web/src/identity";
import { BRIGHTNESS_TEMPERATURE_CHART_RANGE, familyOf, isobaricLegend, scalarLegendRange } from "../../web/src/levels";
import { displayUnit, displayValue } from "../../web/src/units";
import {
  KNOWN_BUNDLE_IDS,
  SATELLITE_IDS,
  isVectorBundle,
  parseBundleMetadata,
  type BundleBand,
  type BundleParameter,
  type BundleVariable,
  type LinearQuantization,
} from "../../web/src/manifest";
import { meteogramRowCode, meteogramRows } from "../../web/src/meteogram";
import { buildPalette, decodeValue } from "../../web/src/palettes";
import { parseVariableFromSearch, searchForVariable } from "../../web/src/urlstate";
import { variableSpec } from "../../web/src/variables";

/** The committed registry both encoders are held to
 * (`tests/test_satellite.py`, and the Rust encoder's unit tests). */
interface RegistryEntry {
  label: string;
  unit: string;
  parameter: BundleParameter;
  band: Record<string, BundleBand>;
  quality: LinearQuantization;
  compact: LinearQuantization;
}

const registry = registryJson as unknown as Record<string, RegistryEntry>;

function bundleVariable(id: string, band: BundleBand | undefined = registry[id]!.band.himawari): BundleVariable {
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

function rgba(palette: Uint8Array, code: number): [number, number, number, number] {
  return [...palette.subarray(code * 4, code * 4 + 4)] as [number, number, number, number];
}

describe("the satellite registry", () => {
  it("knows exactly the channels the encoders register", () => {
    expect([...SATELLITE_IDS].sort()).toEqual(Object.keys(registry).sort());
    for (const id of SATELLITE_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
      // A single layer with a tile of its own, in the sheet's satellite group.
      expect(familyOf(id)).toBeNull();
      expect(variableSpec(id)?.group).toBe("satellite");
    }
  });

  it("reads the channel off the band block, not the parameter alone", () => {
    const { parameter, band } = registry.ir104!;
    const himawari = band.himawari as BundleBand;
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
  });
});

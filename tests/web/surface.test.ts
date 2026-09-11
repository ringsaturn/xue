import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/surface-registry.json";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId } from "../../web/src/identity";
import { CAPE_CHART_MAX, GUST_SPEED_MAX, familyOf, isobaricLegend, scalarLegendRange } from "../../web/src/levels";
import {
  KNOWN_BUNDLE_IDS,
  SURFACE_DIAGNOSTIC_IDS,
  isVectorBundle,
  type BundleParameter,
  type BundleVariable,
  type LinearQuantization,
} from "../../web/src/manifest";
import { CAPE_STOPS, buildPalette, buildWindFieldPalette, decodeValue, legendGradient, windFieldStops } from "../../web/src/palettes";
import { parseVariableFromSearch, searchForVariable } from "../../web/src/urlstate";

/** The committed registry both encoders are held to
 * (`tests/test_surface.py`, and the Rust encoder's unit tests). */
interface RegistryEntry {
  label: string;
  unit: string;
  parameter: BundleParameter;
  quality: LinearQuantization;
  compact: LinearQuantization;
}

const registry = registryJson as unknown as Record<string, RegistryEntry>;

function bundleVariable(id: string, profile: "quality" | "compact" = "quality"): BundleVariable {
  const entry = registry[id]!;
  return { numericId: 1, id, label: entry.label, unit: entry.unit, parameter: entry.parameter, quantization: entry[profile] };
}

describe("the surface diagnostic registry", () => {
  it("knows exactly the variables the encoders register", () => {
    expect([...SURFACE_DIAGNOSTIC_IDS].sort()).toEqual(Object.keys(registry).sort());
    for (const id of SURFACE_DIAGNOSTIC_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
      // Single layers: no family, no level row.
      expect(familyOf(id)).toBeNull();
    }
  });

  it("identifies each field from its parameter block, and from its name before the file is open", () => {
    for (const id of SURFACE_DIAGNOSTIC_IDS) {
      const identity = identityForParameter(registry[id]!.parameter);
      expect(identity).toEqual({ family: id, level: null, vector: false });
      expect(identityForBundleId(id)).toEqual(identity);
      expect(registeredBundleId(identity)).toBe(id);
      // A file named anything at all still reads as the field it carries.
      const placed = identifyBundle([{ ...bundleVariable(id), id: "x" }]);
      expect(placed?.identity).toEqual(identity);
    }
  });

  it("reads the legend over a span the codebook can hold", () => {
    for (const id of SURFACE_DIAGNOSTIC_IDS) {
      const { offset, scale, maximumCode } = registry[id]!.quality;
      const identity = identityForBundleId(id)!;
      const [low, high] = scalarLegendRange(identity)!;
      expect(low).toBe(offset);
      expect(high).toBeLessThanOrEqual(offset + scale * maximumCode);
      // Six ticks, high to low, the top one at the chart ceiling.
      const legend = isobaricLegend(identity)!;
      expect(legend).toHaveLength(6);
      expect(Number(legend[0])).toBe(high);
      expect(Number(legend[5])).toBe(low);
      for (let index = 1; index < legend.length; index += 1) {
        expect(Number(legend[index])).toBeLessThan(Number(legend[index - 1]));
      }
    }
    expect(scalarLegendRange(identityForBundleId("gust")!)).toEqual([0, GUST_SPEED_MAX]);
    expect(scalarLegendRange(identityForBundleId("tcdc")!)).toEqual([0, 100]);
    expect(scalarLegendRange(identityForBundleId("cape")!)).toEqual([0, CAPE_CHART_MAX]);
    // The cloud legend is the codebook's whole span; the other two saturate
    // short of theirs, so a probe still tells the extreme apart.
    expect(GUST_SPEED_MAX).toBeLessThan(registry.gust!.quality.scale * registry.gust!.quality.maximumCode);
    expect(CAPE_CHART_MAX).toBeLessThan(registry.cape!.quality.scale * registry.cape!.quality.maximumCode);
    expect(CAPE_STOPS[CAPE_STOPS.length - 1]![0]).toBe(CAPE_CHART_MAX);
  });

  it("paints the gust with the wind field's own ramp, stretched to its ceiling", () => {
    const stops = windFieldStops(GUST_SPEED_MAX);
    expect(stops[stops.length - 1]![0]).toBe(GUST_SPEED_MAX);
    const gust = buildPalette(bundleVariable("gust"));
    const wind = buildWindFieldPalette(GUST_SPEED_MAX);
    // The same speed is the same colour in both: a 25 m/s gust reads like a
    // 25 m/s wind on a field with the same ceiling.
    for (const speed of [0, 10, 25, 40, GUST_SPEED_MAX]) {
      const code = Math.round(speed / registry.gust!.quality.scale);
      const index = Math.round((speed / GUST_SPEED_MAX) * 255);
      for (let channel = 0; channel < 4; channel += 1) {
        expect(Math.abs(gust[code * 4 + channel]! - wind[index * 4 + channel]!)).toBeLessThanOrEqual(3);
      }
    }
    // Calm keeps a little of the map, a gale most of the ink.
    expect(gust[3]).toBeLessThan(gust[Math.round(40 / 0.5) * 4 + 3]!);
    // Past the ceiling the last colour holds, so the codebook's headroom is
    // one class rather than a stretched ramp.
    const ceiling = Math.round(GUST_SPEED_MAX / 0.5);
    expect([...gust.subarray(ceiling * 4, ceiling * 4 + 4)]).toEqual([...gust.subarray(254 * 4, 254 * 4 + 4)]);
  });

  it("paints clear sky as nothing and full cover as an opaque grey veil", () => {
    const cloud = buildPalette(bundleVariable("tcdc"));
    expect(cloud[3]).toBe(0);
    const full = Math.round(100 / registry.tcdc!.quality.scale);
    expect(cloud[full * 4 + 3]).toBeGreaterThan(240);
    // Neutral: no channel far from the others, and lighter than the slate
    // it sits on so it reads as cloud there, darker than paper so it reads
    // as overcast there.
    const [r, g, b] = [cloud[full * 4]!, cloud[full * 4 + 1]!, cloud[full * 4 + 2]!];
    expect(Math.max(r, g, b) - Math.min(r, g, b)).toBeLessThan(20);
    expect(Math.min(r, g, b)).toBeGreaterThan(180);
    expect(Math.max(r, g, b)).toBeLessThan(235);
    // Alpha climbs monotonically with cover.
    for (let code = 1; code <= full; code += 1) expect(cloud[code * 4 + 3]).toBeGreaterThanOrEqual(cloud[(code - 1) * 4 + 3]!);
    // The compact profile paints the same value the same colour.
    const compact = buildPalette(bundleVariable("tcdc", "compact"));
    expect([...compact.subarray(50 * 4, 50 * 4 + 4)]).toEqual([...cloud.subarray(100 * 4, 100 * 4 + 4)]);
  });

  it("paints stable air as nothing and climbs the severe-weather classes", () => {
    const cape = buildPalette(bundleVariable("cape"));
    expect(cape[3]).toBe(0);
    const at = (joules: number) => {
      const code = Math.round(joules / registry.cape!.quality.scale);
      return [...cape.subarray(code * 4, code * 4 + 4)] as [number, number, number, number];
    };
    // Yellow at 500, red past 2000, violet at the ceiling: hue moves the
    // way an SPC chart's does.
    const [r500, g500, b500] = at(500);
    expect(r500).toBeGreaterThan(200);
    expect(g500).toBeGreaterThan(180);
    expect(b500).toBeLessThan(120);
    const [r2500, g2500] = at(2500);
    expect(r2500).toBeGreaterThan(180);
    expect(g2500).toBeLessThan(100);
    const [rTop, , bTop, aTop] = at(CAPE_CHART_MAX);
    expect(bTop).toBeGreaterThan(rTop);
    expect(aTop).toBe(255);
    // Above the ceiling the ramp holds its last colour.
    expect(at(6350)).toEqual(at(CAPE_CHART_MAX));
    // The legend bar reads off the same palette, over the chart span.
    const gradient = legendGradient(cape, 0, Math.round(CAPE_CHART_MAX / 25));
    expect(gradient.startsWith("linear-gradient(to bottom, rgba(90, 30, 130, 1.000)")).toBe(true);
    expect(gradient.endsWith("rgba(250, 240, 180, 0.000))")).toBe(true);
  });

  it("decodes every valid code and no reserved one", () => {
    for (const id of SURFACE_DIAGNOSTIC_IDS) {
      const variable = bundleVariable(id);
      const { offset, scale, maximumCode, nodataCode } = registry[id]!.quality;
      expect(decodeValue(variable, 0)).toBe(offset);
      expect(decodeValue(variable, maximumCode)).toBeCloseTo(offset + scale * maximumCode, 9);
      expect(decodeValue(variable, nodataCode)).toBeNull();
    }
  });

  it("spells each layer in the URL under a short name", () => {
    expect(parseVariableFromSearch("?type=gust")).toBe("gust");
    expect(parseVariableFromSearch("?type=gusts")).toBe("gust");
    expect(parseVariableFromSearch("?type=cloud")).toBe("tcdc");
    expect(parseVariableFromSearch("?type=CloudCover")).toBe("tcdc");
    expect(parseVariableFromSearch("?type=tcdc")).toBe("tcdc");
    expect(parseVariableFromSearch("?type=cape")).toBe("cape");
    for (const [id, spelling] of [
      ["gust", "gust"],
      ["tcdc", "cloud"],
      ["cape", "cape"],
    ] as const) {
      expect(searchForVariable(id, "")).toBe(`?model=gfs&type=${spelling}`);
      expect(parseVariableFromSearch(searchForVariable(id, ""))).toBe(id);
    }
  });
});

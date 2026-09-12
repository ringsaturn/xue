import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/ocean-registry.json";

import {
  identifyBundle,
  identityForBundleId,
  identityForParameter,
  registeredBundleId,
} from "../../web/src/identity";
import {
  FAMILIES,
  ICE_THICKNESS_CHART_MAX,
  WAVE_HEIGHT_CHART_MAX,
  WAVE_PERIOD_CHART_MAX,
  familyMembers,
  familyOf,
  isobaricLegend,
  levelCode,
  scalarLegendRange,
  temperaturePaletteDomain,
} from "../../web/src/levels";
import {
  KNOWN_BUNDLE_IDS,
  OCEAN_IDS,
  isVectorBundle,
  type BundleParameter,
  type BundleVariable,
  type LinearQuantization,
} from "../../web/src/manifest";
import { buildPalette, decodeValue } from "../../web/src/palettes";
import {
  parseVariableFromSearch,
  searchForVariable,
} from "../../web/src/urlstate";

/** The committed registry both encoders are held to (`tests/test_ocean.py`,
 * and the Rust encoder's unit tests). */
interface RegistryEntry {
  label: string;
  unit: string;
  parameter: BundleParameter;
  quality: LinearQuantization;
  compact: LinearQuantization;
}

const registry = registryJson as unknown as Record<string, RegistryEntry>;

function bundleVariable(
  id: string,
  profile: "quality" | "compact" = "quality",
): BundleVariable {
  const entry = registry[id]!;
  return {
    numericId: 1,
    id,
    label: entry.label,
    unit: entry.unit,
    parameter: entry.parameter,
    quantization: entry[profile],
  };
}

function rgba(
  palette: Uint8Array,
  code: number,
): [number, number, number, number] {
  return [...palette.subarray(code * 4, code * 4 + 4)] as [
    number,
    number,
    number,
    number,
  ];
}

describe("the ocean registry", () => {
  it("knows exactly the variables the encoders register", () => {
    expect([...OCEAN_IDS].sort()).toEqual(Object.keys(registry).sort());
    for (const id of OCEAN_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
    }
    // Sea ice and the waves are families the level row picks within; the
    // skin temperature is a single layer, and the wave direction — no fill
    // — belongs to no tile.
    expect(familyOf("icec")).toBe("ice");
    expect(familyOf("icetk")).toBe("ice");
    expect(familyOf("htsgw")).toBe("wave");
    expect(familyOf("perpw")).toBe("wave");
    expect(familyOf("tmpsfc")).toBeNull();
    expect(familyOf("dirpw")).toBeNull();
    expect(familyMembers("ice")).toEqual(["icec", "icetk"]);
    expect(familyMembers("wave")).toEqual(["htsgw", "perpw"]);
    expect(FAMILIES.ice.surface).toBe("icec");
    expect(FAMILIES.wave.surface).toBe("htsgw");
    expect([
      levelCode("icec"),
      levelCode("icetk"),
      levelCode("htsgw"),
      levelCode("perpw"),
    ]).toEqual(["COVER", "THICK", "HEIGHT", "PERIOD"]);
  });

  it("identifies each field from its parameter block, and from its name before the file is open", () => {
    for (const id of OCEAN_IDS) {
      const identity = identityForParameter(registry[id]!.parameter);
      expect(identity).toEqual({ family: id, level: null, vector: false });
      expect(identityForBundleId(id)).toEqual(identity);
      expect(registeredBundleId(identity)).toBe(id);
      const placed = identifyBundle([{ ...bundleVariable(id), id: "x" }]);
      expect(placed?.identity).toEqual(identity);
    }
    // The wave records' surface value is not part of the identity: the
    // registry declares none, WAVEWATCH III writes 1, pgrb2 would write 0.
    const base = registry.htsgw!.parameter;
    for (const surface of [
      { scaleFactorOfFirstFixedSurface: 0, scaledValueOfFirstFixedSurface: 1 },
      { scaleFactorOfFirstFixedSurface: 0, scaledValueOfFirstFixedSurface: 0 },
    ]) {
      expect(identityForParameter({ ...base, ...surface })?.family).toBe(
        "htsgw",
      );
    }
    // The skin temperature is the 0/0/0 parameter on the ground surface —
    // the 2 m temperature is the same parameter on another surface, and
    // the two must not be confused either way.
    expect(identityForParameter(registry.tmpsfc!.parameter)).toEqual({
      family: "tmpsfc",
      level: null,
      vector: false,
    });
    expect(
      identityForParameter({
        ...registry.tmpsfc!.parameter,
        typeOfFirstFixedSurface: 103,
        scaledValueOfFirstFixedSurface: 2,
      }),
    ).toEqual({ family: "tmp", level: null, vector: false });
    // The sea ice fields are the oceanographic discipline: the same
    // category and number in discipline 0 is the wind direction, not ice.
    expect(
      identityForParameter({ ...registry.icec!.parameter, discipline: 0 }),
    ).toBeNull();
  });

  it("reads the legend over a span the codebook can hold", () => {
    for (const id of OCEAN_IDS) {
      const { offset, scale, maximumCode } = registry[id]!.quality;
      const identity = identityForBundleId(id)!;
      const [low, high] = scalarLegendRange(identity)!;
      expect(low).toBeGreaterThanOrEqual(offset);
      // The direction's legend closes the circle at 360, which the codebook
      // stops one code short of; every other span sits inside its codebook.
      if (id !== "dirpw")
        expect(high).toBeLessThanOrEqual(offset + scale * maximumCode);
      const legend = isobaricLegend(identity)!;
      expect(legend).toHaveLength(6);
      expect(Number(legend[0])).toBe(high);
      expect(Number(legend[5])).toBe(low);
      for (let index = 1; index < legend.length; index += 1) {
        expect(Number(legend[index])).toBeLessThan(Number(legend[index - 1]));
      }
    }
    expect(scalarLegendRange(identityForBundleId("tmpsfc")!)).toEqual(
      temperaturePaletteDomain(null),
    );
    expect(scalarLegendRange(identityForBundleId("icec")!)).toEqual([0, 100]);
    expect(scalarLegendRange(identityForBundleId("icetk")!)).toEqual([
      0,
      ICE_THICKNESS_CHART_MAX,
    ]);
    expect(scalarLegendRange(identityForBundleId("htsgw")!)).toEqual([
      0,
      WAVE_HEIGHT_CHART_MAX,
    ]);
    expect(scalarLegendRange(identityForBundleId("perpw")!)).toEqual([
      0,
      WAVE_PERIOD_CHART_MAX,
    ]);
    // Wave height and period saturate short of their codebooks, so a probe
    // still tells a 15 m sea from a 10 m one; ice thickness reads the whole
    // codebook.
    expect(WAVE_HEIGHT_CHART_MAX).toBeLessThan(
      registry.htsgw!.quality.scale * registry.htsgw!.quality.maximumCode,
    );
    expect(WAVE_PERIOD_CHART_MAX).toBeLessThan(
      registry.perpw!.quality.scale * registry.perpw!.quality.maximumCode,
    );
    expect(ICE_THICKNESS_CHART_MAX).toBeCloseTo(
      registry.icetk!.quality.scale * registry.icetk!.quality.maximumCode,
      0,
    );
  });

  it("paints the sea surface with the temperature's own ramp", () => {
    const skin = buildPalette(bundleVariable("tmpsfc"));
    const air = buildPalette({
      ...bundleVariable("tmpsfc"),
      id: "tmp2m",
      parameter: undefined,
      quantization: {
        type: "linear",
        offset: -60,
        scale: 0.5,
        minimumCode: 0,
        maximumCode: 220,
        nodataCode: 255,
      },
    });
    // Both codebooks start at -60 °C in half degrees, so the same code is
    // the same value — and the same colour, 25 °C being code 170 in each.
    for (const code of [0, 60, 120, 170, 220])
      expect(rgba(skin, code)).toEqual(rgba(air, code));
    // The skin's codebook runs on to 67 °C: opaque, the ramp's last colour.
    expect(rgba(skin, 254)).toEqual(rgba(skin, 220));
    expect(rgba(skin, 254)[3]).toBe(255);
  });

  it("paints open water as nothing and the pack as a deepening blue", () => {
    const cover = buildPalette(bundleVariable("icec"));
    const at = (percent: number) =>
      rgba(cover, Math.round(percent / registry.icec!.quality.scale));
    expect(at(0)[3]).toBe(0);
    expect(at(10)[3]).toBe(0);
    // The ice edge is drawn at 15 %, the way an ice chart draws it.
    expect(at(15)[3]).toBeGreaterThan(100);
    expect(at(100)[3]).toBe(255);
    // Blue throughout, deepening with concentration: readable on white
    // paper as well as on the slate.
    for (const percent of [15, 40, 70, 100]) {
      const [r, , b] = at(percent);
      expect(b).toBeGreaterThan(r);
    }
    for (let code = 1; code <= 200; code += 1)
      expect(cover[code * 4 + 3]).toBeGreaterThanOrEqual(
        cover[(code - 1) * 4 + 3]!,
      );
    expect(at(100)[0]).toBeLessThan(at(15)[0]);
    // The compact profile paints the same value the same colour.
    const compact = buildPalette(bundleVariable("icec", "compact"));
    expect(rgba(compact, 50)).toEqual(at(50));
    // Thickness likewise: nothing at zero, violet at the 5 m ceiling.
    const thickness = buildPalette(bundleVariable("icetk"));
    expect(rgba(thickness, 0)[3]).toBe(0);
    const top = rgba(
      thickness,
      Math.round(ICE_THICKNESS_CHART_MAX / registry.icetk!.quality.scale),
    );
    expect(top[3]).toBe(255);
    expect(top[2]).toBeGreaterThan(top[1]);
  });

  it("paints a calm sea and land as the map and climbs to a storm sea", () => {
    const height = buildPalette(bundleVariable("htsgw"));
    const at = (metres: number) =>
      rgba(height, Math.round(metres / registry.htsgw!.quality.scale));
    // Code 0 — land in the file, or a flat calm — is exactly the map.
    expect(at(0)[3]).toBe(0);
    expect(at(0.5)[3]).toBeGreaterThan(100);
    // Blue at a metre, yellow at three, red past six, violet at the ceiling.
    const [, , b1] = at(1);
    expect(b1).toBeGreaterThan(150);
    const [r3, g3, b3] = at(3);
    expect(r3).toBeGreaterThan(200);
    expect(g3).toBeGreaterThan(180);
    expect(b3).toBeLessThan(120);
    const [r6, g6] = at(6);
    expect(r6).toBeGreaterThan(200);
    expect(g6).toBeLessThan(100);
    const [rTop, , bTop, aTop] = at(WAVE_HEIGHT_CHART_MAX);
    expect(bTop).toBeGreaterThan(rTop);
    expect(aTop).toBe(255);
    // Past the ceiling the ramp holds its last colour; alpha never falls.
    expect(at(25.4)).toEqual(at(WAVE_HEIGHT_CHART_MAX));
    for (let code = 1; code <= 254; code += 1)
      expect(height[code * 4 + 3]).toBeGreaterThanOrEqual(
        height[(code - 1) * 4 + 3]!,
      );
    // The period: transparent under a second (land), cool for a wind sea,
    // warm for a long swell.
    const period = buildPalette(bundleVariable("perpw"));
    const atSeconds = (seconds: number) =>
      rgba(period, Math.round(seconds / registry.perpw!.quality.scale));
    expect(atSeconds(0)[3]).toBe(0);
    expect(atSeconds(4)[2]).toBeGreaterThan(atSeconds(4)[0]);
    expect(atSeconds(18)[0]).toBeGreaterThan(atSeconds(18)[2]);
    expect(atSeconds(25.4)).toEqual(atSeconds(WAVE_PERIOD_CHART_MAX));
  });

  it("closes the direction's hue wheel and leaves the file's zero to the map", () => {
    const direction = buildPalette(bundleVariable("dirpw"));
    const at = (degrees: number) =>
      rgba(direction, Math.round(degrees / registry.dirpw!.quality.scale));
    expect(at(0)[3]).toBe(0);
    // North and the last code before north are the same red, so the wheel
    // closes; east is yellow, south cyan, west blue.
    const north = at(1.5);
    const back = at(358.5);
    for (let channel = 0; channel < 3; channel += 1)
      expect(Math.abs(north[channel]! - back[channel]!)).toBeLessThanOrEqual(3);
    expect(north[0]).toBeGreaterThan(north[2]);
    expect(at(90)[1]).toBeGreaterThan(150);
    expect(at(180)[2]).toBeGreaterThan(at(180)[0]);
    expect(at(270)[2]).toBeGreaterThan(at(270)[1]);
  });

  it("decodes every valid code and no reserved one", () => {
    for (const id of OCEAN_IDS) {
      const variable = bundleVariable(id);
      const { offset, scale, maximumCode, nodataCode } = registry[id]!.quality;
      expect(decodeValue(variable, 0)).toBe(offset);
      expect(decodeValue(variable, maximumCode)).toBeCloseTo(
        offset + scale * maximumCode,
        9,
      );
      expect(decodeValue(variable, nodataCode)).toBeNull();
    }
    // The direction's compact codebook stops at 357°: no code reads as 360.
    expect(
      decodeValue(
        bundleVariable("dirpw", "compact"),
        registry.dirpw!.compact.maximumCode,
      ),
    ).toBeLessThan(360);
  });

  it("spells each layer in the URL under a short name", () => {
    expect(parseVariableFromSearch("?type=sst")).toBe("tmpsfc");
    expect(parseVariableFromSearch("?type=SkinTemp")).toBe("tmpsfc");
    expect(parseVariableFromSearch("?type=seaice")).toBe("icec");
    expect(parseVariableFromSearch("?type=ice")).toBe("icec");
    expect(parseVariableFromSearch("?type=icethickness")).toBe("icetk");
    expect(parseVariableFromSearch("?type=waves")).toBe("htsgw");
    expect(parseVariableFromSearch("?type=swh")).toBe("htsgw");
    expect(parseVariableFromSearch("?type=waveperiod")).toBe("perpw");
    expect(parseVariableFromSearch("?type=wavedirection")).toBe("dirpw");
    for (const [id, spelling] of [
      ["tmpsfc", "sst"],
      ["icec", "seaice"],
      ["icetk", "icethickness"],
      ["htsgw", "waves"],
      ["perpw", "waveperiod"],
      ["dirpw", "wavedirection"],
    ] as const) {
      expect(searchForVariable(id, "")).toBe(`?model=gfs&type=${spelling}`);
      expect(parseVariableFromSearch(searchForVariable(id, ""))).toBe(id);
    }
  });
});

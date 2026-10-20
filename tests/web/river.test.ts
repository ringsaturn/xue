import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/river-registry.json";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId } from "../../web/src/identity";
import { DISCHARGE_CHART, DISRP_CLASSES, UPSTREAM_CHART, disrpClassLabelKey, isobaricLegend, logLegend, scalarLegendRange } from "../../web/src/levels";
import {
  HYDROLOGY_IDS,
  KNOWN_BUNDLE_IDS,
  isVectorBundle,
  parseBundleMetadata,
  type BundleParameter,
  type BundleVariable,
  type LinearQuantization,
  type LogQuantization,
} from "../../web/src/manifest";
import { METEOGRAM_BUNDLE_IDS, meteogramRowCode, meteogramRows } from "../../web/src/meteogram";
import { buildPalette, decodeValue, encodeLog, readingValue } from "../../web/src/palettes";
import { displayUnit, formatValueReadout } from "../../web/src/units";
import { parseModelFromSearch, parseVariableFromSearch } from "../../web/src/urlstate";
import { variableIds, variableSpec } from "../../web/src/variables";
import { metadataJson, registryVariable, rgba, type RegistryEntry } from "./helpers";

/** The committed registry both encoders are held to: the three river
 * fields with their parameter blocks and codebooks. */
const registry = registryJson as unknown as Record<string, RegistryEntry<LinearQuantization | LogQuantization>>;

/** The parameter blocks the encoders write (xuebuild/variables.py): WMO
 * 1/0/7 river discharge averaged over the step, and the encoder's own
 * return-period class and upstream area as local numbers 1 and 2 in the
 * hydrological discipline's first local category, under producer `xue`. */
const GROUND = { typeOfFirstFixedSurface: 1, scaleFactorOfFirstFixedSurface: null, scaledValueOfFirstFixedSurface: null };
const DISCHARGE: BundleParameter = { discipline: 1, parameterCategory: 0, parameterNumber: 7, ...GROUND, typeOfStatisticalProcessing: 0 };
const RETURN_PERIOD: BundleParameter = { discipline: 1, parameterCategory: 192, parameterNumber: 1, ...GROUND };
const UPSTREAM: BundleParameter = { discipline: 1, parameterCategory: 192, parameterNumber: 2, ...GROUND };
const XUE = { id: "xue", version: "1" };

const DISCHARGE_BOOK: LogQuantization = {
  type: "log1p",
  trace: 0.1,
  scale: 10,
  maximum: 100000,
  minimumCode: 1,
  maximumCode: 253,
  zeroCode: 0,
  overflowCode: 254,
  nodataCode: 255,
};
const UPSTREAM_BOOK: LogQuantization = { ...DISCHARGE_BOOK, trace: 1, scale: 100, maximum: 10_000_000 };
const CLASS_BOOK: LinearQuantization = { type: "linear", offset: 0, scale: 1, minimumCode: 0, maximumCode: 5, nodataCode: 255 };

function variable(id: string, unit: string, parameter: BundleParameter, quantization: LinearQuantization | LogQuantization, extra: Partial<BundleVariable> = {}): BundleVariable {
  return { numericId: 1, id, label: id, unit, parameter, ...extra, quantization };
}

const dis06 = variable("dis06", "m³/s", DISCHARGE, DISCHARGE_BOOK);
const disrp = variable("disrp", "1", RETURN_PERIOD, CLASS_BOOK, { producer: XUE });
const upstream = variable("upstream", "km²", UPSTREAM, UPSTREAM_BOOK, { producer: XUE });

describe("the river fields", () => {
  it("has a row for each, under its own sheet group", () => {
    expect(HYDROLOGY_IDS).toEqual(["dis06", "disrp", "upstream"]);
    for (const id of HYDROLOGY_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(variableIds()).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
      const spec = variableSpec(id)!;
      expect(spec.group).toBe("hydrology");
      expect(spec.family).toBeNull();
      expect(spec.vector).toBe(false);
    }
  });

  it("matches the encoders' registry", () => {
    expect(Object.keys(registry)).toEqual([...HYDROLOGY_IDS]);
    for (const [id, fixture] of [
      ["dis06", dis06],
      ["disrp", disrp],
      ["upstream", upstream],
    ] as const) {
      const entry = registry[id]!;
      const { typeOfStatisticalProcessing: _process, ...parameter } = fixture.parameter!;
      expect(entry.parameter).toEqual(parameter);
      expect(entry.quality).toEqual(fixture.quantization);
      expect(entry.unit).toBe(fixture.unit);
      // Both profiles resolve to the field, with its producer where the
      // number is a local one.
      for (const profile of ["quality", "compact"] as const) {
        const bundle = registryVariable(id, entry, entry[profile], fixture.producer ? { producer: fixture.producer } : {});
        expect(identifyBundle([bundle])?.identity.family).toBe(id);
        expect(rgba(buildPalette(bundle), entry[profile].nodataCode)).toEqual([0, 0, 0, 0]);
      }
    }
  });

  it("reads each identity off its parameter block, and back", () => {
    for (const [bundle, family] of [
      [dis06, "dis06"],
      [disrp, "disrp"],
      [upstream, "upstream"],
    ] as const) {
      const identity = identifyBundle([bundle])?.identity ?? null;
      expect(identity).toEqual({ family, level: null, vector: false });
      expect(registeredBundleId(identity)).toBe(family);
      expect(identityForBundleId(family)).toEqual(identity);
    }
    // The statistical process is not part of the identity.
    const { typeOfStatisticalProcessing: _average, ...instantaneous } = DISCHARGE;
    expect(identityForParameter(instantaneous)?.family).toBe("dis06");
    // A local number means nothing without its producer, or under another.
    expect(identifyBundle([{ ...disrp, producer: undefined }])).toBeNull();
    expect(identifyBundle([{ ...disrp, producer: { id: "shachen", version: "1" } }])).toBeNull();
    expect(identifyBundle([{ ...upstream, parameter: { ...UPSTREAM, parameterNumber: 3 } }])).toBeNull();
  });

  it("validates as schema v3 bundle metadata on the EFAS grid", () => {
    const metadata = parseBundleMetadata(
      metadataJson(
        {
          model: "EFAS-v5",
          runTime: "2024-10-28T00:00:00Z",
          time: { unitSeconds: 3600, firstFrameOffset: 6, frameCount: 20, frameStep: 6 },
          grid: { width: 300, height: 240, firstLongitude: -2.991666, firstLatitude: 41.99166, longitudeStep: 1 / 60, latitudeStep: -1 / 60, wrapLongitude: false },
        },
        [disrp],
      ),
    );
    expect(identifyBundle(metadata.variables)?.identity.family).toBe("disrp");
  });

  it("decodes the return-period codes to the five classes and leaves no data transparent", () => {
    expect(DISRP_CLASSES.map((entry) => entry.code)).toEqual([1, 2, 3, 4, 5]);
    expect(DISRP_CLASSES.map((entry) => entry.labelKey)).toEqual([
      "legendDisrpBelow",
      "legendDisrp1p5",
      "legendDisrp2",
      "legendDisrp5",
      "legendDisrp20",
    ]);
    for (const entry of DISRP_CLASSES) expect(decodeValue(disrp, entry.code)).toBe(entry.code);
    expect(decodeValue(disrp, 255)).toBeNull();
    expect(decodeValue(disrp, 6)).toBeNull();
    // Code 0 is no class: it decodes, but it is no reading and no colour.
    expect(decodeValue(disrp, 0)).toBe(0);
    expect(readingValue(disrp, 0)).toBeNull();
    expect(readingValue(disrp, 4)).toBe(4);
    expect(disrpClassLabelKey(0)).toBeNull();
    expect(disrpClassLabelKey(1)).toBe("legendDisrpBelow");
    expect(disrpClassLabelKey(5)).toBe("legendDisrp20");
    const palette = buildPalette(disrp, identifyBundle([disrp])!.identity);
    for (const { code, rgb, alpha } of DISRP_CLASSES) expect(rgba(palette, code)).toEqual([...rgb, alpha]);
    // EFAS's own colours: yellow, red and purple for the 2-, 5- and 20-year
    // levels, and below 1.5 years faint rather than absent.
    expect(rgba(palette, 4).slice(0, 3)).toEqual([230, 30, 30]);
    expect(rgba(palette, 1)[3]).toBeGreaterThan(0);
    expect(rgba(palette, 1)[3]).toBeLessThan(255);
    expect(rgba(palette, 0)).toEqual([0, 0, 0, 0]);
    expect(rgba(palette, 255)).toEqual([0, 0, 0, 0]);
    // A swatch key, the highest class first, and no ramp.
    const spec = variableSpec("disrp")!;
    expect(spec.legend()).toEqual([]);
    const key = spec.legendKey!();
    expect(key).toHaveLength(5);
    expect(key[0]!.label).toBe("≥ 20-year level");
    expect(key[4]!.label).toBe("Below 1.5-year level");
    expect(scalarLegendRange({ family: "disrp", level: null, vector: false })).toBeNull();
  });

  it("paints the discharge on a log ramp from 1 to 10 000 m³/s, dry and no data transparent", () => {
    const palette = buildPalette(dis06, identifyBundle([dis06])!.identity);
    expect(rgba(palette, DISCHARGE_BOOK.zeroCode)[3]).toBe(0);
    // Off the rivers the zero code is no reading, not 0 m³/s.
    expect(readingValue(dis06, DISCHARGE_BOOK.zeroCode)).toBeNull();
    expect(readingValue(upstream, UPSTREAM_BOOK.zeroCode)).toBeNull();
    expect(readingValue(dis06, encodeLog(DISCHARGE_BOOK, 50))).toBeCloseTo(50, -1);
    expect(rgba(palette, DISCHARGE_BOOK.nodataCode)).toEqual([0, 0, 0, 0]);
    const low = rgba(palette, encodeLog(DISCHARGE_BOOK, 1));
    const high = rgba(palette, encodeLog(DISCHARGE_BOOK, 10000));
    // Light blue to purple: the low end is the lighter, the high end has
    // more red than green.
    expect(low[0] + low[1] + low[2]).toBeGreaterThan(high[0] + high[1] + high[2]);
    expect(high[0]).toBeGreaterThan(high[1]);
    expect(high[3]).toBe(255);
    expect(scalarLegendRange({ family: "dis06", level: null, vector: false })).toEqual([0, 10000]);
    expect(isobaricLegend({ family: "dis06", level: null, vector: false })).toEqual(logLegend(DISCHARGE_CHART));
    expect(logLegend(DISCHARGE_CHART)[0]).toBe("10000");
    expect(logLegend(DISCHARGE_CHART)[5]).toBe("0");
    expect(DISCHARGE_CHART).toMatchObject({ trace: DISCHARGE_BOOK.trace, scale: DISCHARGE_BOOK.scale, maximum: DISCHARGE_BOOK.maximum });
  });

  it("paints the upstream area on a grey ramp from 10 to 100 000 km²", () => {
    const palette = buildPalette(upstream, identifyBundle([upstream])!.identity);
    expect(rgba(palette, UPSTREAM_BOOK.nodataCode)).toEqual([0, 0, 0, 0]);
    expect(rgba(palette, UPSTREAM_BOOK.zeroCode)).toEqual([0, 0, 0, 0]);
    for (const area of [10, 1000, 100000]) {
      const [r, g, b, a] = rgba(palette, encodeLog(UPSTREAM_BOOK, area));
      expect(r).toBe(g);
      expect(g).toBe(b);
      expect(a).toBeGreaterThan(0);
    }
    expect(scalarLegendRange({ family: "upstream", level: null, vector: false })).toEqual([0, 100000]);
    expect(UPSTREAM_CHART).toMatchObject({ trace: UPSTREAM_BOOK.trace, scale: UPSTREAM_BOOK.scale, maximum: UPSTREAM_BOOK.maximum });
  });

  it("is drawn cell by cell, the class field without blending frames", () => {
    expect(variableSpec("dis06")!.sampling).toBe("nearest");
    expect(variableSpec("upstream")!.sampling).toBe("nearest");
    expect(variableSpec("disrp")!.sampling).toBe("classes");
    // Every model field keeps the bicubic reconstruction.
    for (const id of ["tmp2m", "prate", "ptype", "cref", "tmp850", "prmsl"]) expect(variableSpec(id)!.sampling).toBe("smooth");
  });

  it("reads the discharge whole above 100 m³/s and to a tenth below", () => {
    expect(formatValueReadout(dis06, 1234.56)).toBe("1235");
    expect(formatValueReadout(dis06, 100)).toBe("100");
    expect(formatValueReadout(dis06, 99.94)).toBe("99.9");
    expect(formatValueReadout(dis06, 3.44)).toBe("3.4");
    expect(formatValueReadout(disrp, 3)).toBe("3");
    // Every other logarithmic field keeps its own digits.
    expect(formatValueReadout({ ...upstream, parameter: UPSTREAM }, 3.456)).toBe("3.46");
    expect(displayUnit("m³/s")).toBe("m³/s");
    expect(displayUnit("m3 s-1")).toBe("m³/s");
    expect(displayUnit("m3/s")).toBe("m³/s");
    expect(displayUnit("km2")).toBe("km²");
    expect(displayUnit("km²")).toBe("km²");
  });

  it("draws the discharge at a pinned cell as a hydrograph row", () => {
    const published = new Set<string>(HYDROLOGY_IDS);
    const rows = meteogramRows((id) => published.has(id));
    expect(rows.map((row) => row.id)).toEqual(["discharge"]);
    expect(rows[0]!.bundles).toEqual(["dis06"]);
    expect(rows[0]!.baseline).toBe(0);
    expect(meteogramRowCode(rows[0]!)).toBe("DIS");
    expect(METEOGRAM_BUNDLE_IDS).toContain("dis06");
  });

  it("is named in the URL, and its models only through a case", () => {
    expect(parseVariableFromSearch("?type=discharge")).toBe("dis06");
    expect(parseVariableFromSearch("?type=returnperiod")).toBe("disrp");
    expect(parseVariableFromSearch("?type=upstream")).toBe("upstream");
    expect(parseModelFromSearch("?model=efas")).toBe("gfs");
    expect(parseModelFromSearch("?model=efaswb")).toBe("gfs");
  });
});

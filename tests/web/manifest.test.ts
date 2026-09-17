import { describe, expect, it } from "vitest";

import { CRC32_INITIAL, crc32Hex, crc32Of, crc32Update } from "../../web/src/crc32";
import {
  axisUnitSeconds,
  containerOf,
  modelDefaultVariable,
  deliveryBytes,
  FORECAST_MODEL_IDS,
  FORECAST_MODELS,
  frameOffsets,
  hasBundle,
  hasWindBundle,
  isObservationModel,
  parseBundleMetadata,
  pickBundleVariant,
  sameTimeAxis,
  validateLatestPointer,
  validateManifest,
} from "../../web/src/manifest";
import { buildPalette, buildWindSpeedPalette, decodeLinear, decodeLog } from "../../web/src/palettes";
import type { BundleVariable, LogQuantization, VariantDescriptor, ZarrStoreDescriptor } from "../../web/src/manifest";

function videoMetadataFixture() {
  return JSON.stringify({
    schemaVersion: 1,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2026-08-15T06:00:00Z",
    time: { firstForecastHour: 0, stepHours: 1, frameCount: 121 },
    grid: { width: 1440, height: 721 },
    variables: [
      {
        numericId: 1,
        id: "tmp2m",
        label: "2 meter temperature",
        unit: "°C",
        quantization: {
          type: "linear",
          offset: -60,
          scale: 0.5,
          minimumCode: 0,
          maximumCode: 220,
          nodataCode: 255,
        },
      },
    ],
  });
}

function videoFixture() {
  return {
    streamPath: "gfs.2026081506/tmp2m.h264",
    indexPath: "gfs.2026081506/tmp2m.h264.index.json",
    byteLength: 19_867_506,
    crc32: "c63a61aa",
    codec: "avc1.f40028",
    width: 1440,
    height: 721,
    gop: 6,
    frameCount: 121,
    metadataJson: videoMetadataFixture(),
  };
}

function variantFixture(): VariantDescriptor {
  return {
    path: "gfs.2026081506/tmp2m.half.xue",
    width: 720,
    height: 361,
    byteLength: 11_000_000,
    crc32: "12345678",
    bandwidth: 10_500_000,
  };
}

function zarrFixture(path = "gfs.2026081506/tmp2m.zarr"): ZarrStoreDescriptor {
  return { path, byteLength: 12_347_075, crc32: "760cef95" };
}

/** The fixture's bundles are plain literals; this is the tmp2m entry seen
 * as something a store can be hung on. */
function storeBearing(manifest: ReturnType<typeof manifestFixture>) {
  return manifest.bundles[0] as unknown as { zarr?: unknown; variants: { zarr?: ZarrStoreDescriptor }[] };
}

function manifestFixture() {
  return {
    schemaVersion: 5,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2026-08-15T06:00:00Z",
    forecastHours: 120,
    bundles: [
      {
        variable: "tmp2m",
        path: "gfs.2026081506/tmp2m.xue",
        byteLength: 45_631_552,
        crc32: "35814edd",
        variants: [variantFixture()],
        video: videoFixture(),
      },
      {
        variable: "prate",
        path: "gfs.2026081506/prate.xue",
        byteLength: 25_822_504,
        crc32: "1234abcd",
      },
    ],
  };
}

describe("validateManifest", () => {
  it("accepts a valid schema v5 manifest with a video descriptor", () => {
    const manifest = validateManifest(manifestFixture());
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate"]);
    expect(manifest.bundles[0]!.path).toBe("gfs.2026081506/tmp2m.xue");
    expect(manifest.bundles[0]!.video?.codec).toBe("avc1.f40028");
    expect(manifest.bundles[1]!.video).toBeUndefined();
  });

  it("rejects an invalid video descriptor shape", () => {
    const missingMetadata = manifestFixture();
    const { metadataJson: _metadataJson, ...rest } = missingMetadata.bundles[0]!.video!;
    missingMetadata.bundles[0]!.video = rest as ReturnType<typeof videoFixture>;
    expect(() => validateManifest(missingMetadata)).toThrow("metadata");

    const badStreamPath = manifestFixture();
    badStreamPath.bundles[0]!.video!.streamPath = "gfs.2026081506/tmp2m.mp4";
    expect(() => validateManifest(badStreamPath)).toThrow("path");
  });

  it("rejects other schema versions", () => {
    expect(() => validateManifest({ ...manifestFixture(), schemaVersion: 3 })).toThrow("manifest schema version");
  });

  it("accepts an ECMWF manifest and enforces the model/product pairing", () => {
    const ecmwf = { ...manifestFixture(), model: "ECMWF", product: "ifs-0p25" };
    expect(validateManifest(ecmwf).model).toBe("ECMWF");
    expect(validateManifest(ecmwf, "ecmwf").model).toBe("ECMWF");
    expect(() => validateManifest(ecmwf, "gfs")).toThrow("model");
    expect(() => validateManifest({ ...manifestFixture(), model: "ECMWF" })).toThrow("product");
    expect(() => validateManifest({ ...manifestFixture(), model: "ICON" })).toThrow("product");
  });

  it("rejects invalid bundle paths", () => {
    for (const path of ["/abs.xue", "http://x/y.xue", "a/../b.xue", "bundle.pmtiles", "gfs.2026081506/tmp2m.bin"]) {
      const broken = manifestFixture();
      broken.bundles[0]!.path = path;
      expect(() => validateManifest(broken)).toThrow("path");
    }
  });

  it("rejects duplicate bundle paths", () => {
    const broken = manifestFixture();
    broken.bundles[1]!.path = broken.bundles[0]!.path;
    expect(() => validateManifest(broken)).toThrow("duplicate");
  });

  it("rejects a manifest missing a variable's bundle", () => {
    const broken = manifestFixture();
    broken.bundles = broken.bundles.slice(0, 1);
    expect(() => validateManifest(broken)).toThrow("prate");
  });

  it("admits a bundle on the shape of its name, not on a registry of names", () => {
    // The point of the rule: a run may publish a layer this build has never
    // heard of, and the shell renders it generically instead of refusing the
    // whole manifest — which is what used to force a shell deploy ahead of
    // every new bundle.
    const extra = manifestFixture();
    extra.bundles.push({
      variable: "gust10m",
      path: "gfs.2026081506/gust10m.xue",
      byteLength: 30_000_000,
      crc32: "0badcafe",
    });
    const manifest = validateManifest(extra);
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate", "gust10m"]);
    expect(hasBundle(manifest, "gust10m")).toBe(true);
    // No ordering constraint either: the rail has its own order.
    const reordered = manifestFixture();
    reordered.bundles.reverse();
    expect(validateManifest(reordered).bundles[0]!.variable).toBe("prate");
  });

  it("rejects a malformed or repeated bundle name", () => {
    for (const name of ["Tmp2m", "tmp-2m", "tmp 2m", "2mtemp", "", "tmp_2m", 7, null]) {
      const broken = manifestFixture();
      broken.bundles[0]!.variable = name as string;
      expect(() => validateManifest(broken)).toThrow("bundle variable name");
    }
    const duplicate = manifestFixture();
    duplicate.bundles[1]!.variable = "tmp2m";
    expect(() => validateManifest(duplicate)).toThrow("duplicate");
  });

  it("accepts the optional wind10m bundle and reports it via hasWindBundle", () => {
    const withoutWind = validateManifest(manifestFixture());
    expect(hasWindBundle(withoutWind)).toBe(false);

    const withWind = manifestFixture();
    withWind.bundles.push({
      variable: "wind10m",
      path: "gfs.2026081506/wind10m.xue",
      byteLength: 52_000_000,
      crc32: "0badf00d",
    });
    const manifest = validateManifest(withWind);
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate", "wind10m"]);
    expect(hasWindBundle(manifest)).toBe(true);
  });

  it("accepts a GFS-SFLUX manifest with the optional dswrf bundle", () => {
    const sflux = manifestFixture();
    sflux.model = "GFS-SFLUX";
    sflux.product = "sfluxgrb";
    sflux.bundles[0]!.path = "sflux.2026081506/tmp2m.xue";
    sflux.bundles[0]!.variants![0]!.path = "sflux.2026081506/tmp2m.half.xue";
    sflux.bundles[0]!.video!.streamPath = "sflux.2026081506/tmp2m.h264";
    sflux.bundles[0]!.video!.indexPath = "sflux.2026081506/tmp2m.h264.index.json";
    sflux.bundles[1]!.path = "sflux.2026081506/prate.xue";
    sflux.bundles.push({
      variable: "dswrf",
      path: "sflux.2026081506/dswrf.xue",
      byteLength: 61_000_000,
      crc32: "5011a860",
    });
    const manifest = validateManifest(sflux, "sflux");
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate", "dswrf"]);
    expect(hasBundle(manifest, "dswrf")).toBe(true);
    expect(hasBundle(manifest, "wind10m")).toBe(false);
    expect(hasBundle(validateManifest(manifestFixture()), "dswrf")).toBe(false);
    expect(() => validateManifest(sflux, "gfs")).toThrow("model");
    expect(() => validateManifest({ ...sflux, product: "pgrb2.0p25" })).toThrow("product");
  });

  it("rejects invalid byteLength and crc32", () => {
    const zero = manifestFixture();
    zero.bundles[0]!.byteLength = 0;
    expect(() => validateManifest(zero)).toThrow("byteLength");
    const badCrc = manifestFixture();
    badCrc.bundles[0]!.crc32 = "XYZ";
    expect(() => validateManifest(badCrc)).toThrow("crc32");
  });

  it("accepts any sane forecast range and rejects broken ones", () => {
    // 120-hour and 240-hour runs coexist (the horizon is a pipeline choice).
    expect(validateManifest({ ...manifestFixture(), forecastHours: 240 }).forecastHours).toBe(240);
    expect(validateManifest({ ...manifestFixture(), forecastHours: 24 }).forecastHours).toBe(24);
    for (const broken of [0, -24, 1.5, 385, "120", undefined]) {
      expect(() => validateManifest({ ...manifestFixture(), forecastHours: broken })).toThrow("range");
    }
  });

  it("accepts variants and rejects broken variant descriptors", () => {
    const manifest = validateManifest(manifestFixture());
    expect(manifest.bundles[0]!.variants?.[0]?.width).toBe(720);
    expect(manifest.bundles[1]!.variants).toBeUndefined();

    const badPath = manifestFixture();
    badPath.bundles[0]!.variants![0]!.path = "gfs.2026081506/tmp2m.half.mp4";
    expect(() => validateManifest(badPath)).toThrow("path");

    const duplicate = manifestFixture();
    duplicate.bundles[0]!.variants![0]!.path = duplicate.bundles[0]!.path;
    expect(() => validateManifest(duplicate)).toThrow("duplicate");

    const zeroBandwidth = manifestFixture();
    zeroBandwidth.bundles[0]!.variants![0]!.bandwidth = 0;
    expect(() => validateManifest(zeroBandwidth)).toThrow("bandwidth");

    const empty = manifestFixture();
    empty.bundles[0]!.variants = [];
    expect(() => validateManifest(empty)).toThrow("variant");
  });

  it("accepts a bundle and a variant that ship only a zarr store, and rejects one with neither", () => {
    const storeOnly = manifestFixture() as unknown as { bundles: Record<string, unknown>[] };
    const bundle = storeOnly.bundles[0]!;
    delete bundle.path;
    delete bundle.byteLength;
    delete bundle.crc32;
    delete bundle.video;
    bundle.zarr = zarrFixture();
    bundle.variants = [{ width: 720, height: 361, bandwidth: 2_400_000, zarr: zarrFixture("gfs.2026081506/tmp2m.half.zarr") }];
    const manifest = validateManifest(storeOnly);
    expect(manifest.bundles[0]!.path).toBeUndefined();
    expect(containerOf(manifest.bundles[0]!)).toBeNull();
    expect(containerOf(manifest.bundles[1]!)).toEqual({
      path: "gfs.2026081506/prate.xue",
      byteLength: manifest.bundles[1]!.byteLength,
      crc32: manifest.bundles[1]!.crc32,
    });
    expect(deliveryBytes(manifest.bundles[0]!)).toBe(12_347_075);
    expect(deliveryBytes(manifest.bundles[0]!.variants![0]!)).toBe(12_347_075);
    expect(deliveryBytes(manifest.bundles[1]!)).toBe(manifest.bundles[1]!.byteLength);

    const neither = structuredClone(storeOnly);
    delete neither.bundles[0]!.zarr;
    expect(() => validateManifest(neither)).toThrow("neither a bundle path nor a zarr store");
    const neitherVariant = structuredClone(storeOnly);
    delete (neitherVariant.bundles[0]!.variants as Record<string, unknown>[])[0]!.zarr;
    expect(() => validateManifest(neitherVariant)).toThrow("variant has neither");
    const halfUnit = structuredClone(storeOnly);
    halfUnit.bundles[0]!.crc32 = "760cef95";
    expect(() => validateManifest(halfUnit)).toThrow("without a path");
    const wrongSuffix = structuredClone(storeOnly);
    wrongSuffix.bundles[0]!.path = "gfs.2026081506/tmp2m.zarr";
    wrongSuffix.bundles[0]!.byteLength = 1;
    wrongSuffix.bundles[0]!.crc32 = "760cef95";
    expect(() => validateManifest(wrongSuffix)).toThrow("path");
  });

  it("accepts an optional zarr store on a bundle and on a variant, and rejects a broken one", () => {
    const withStore = manifestFixture();
    storeBearing(withStore).zarr = zarrFixture();
    storeBearing(withStore).variants[0]!.zarr = zarrFixture("gfs.2026081506/tmp2m.half.zarr");
    const manifest = validateManifest(withStore);
    expect(manifest.bundles[0]!.zarr?.path).toBe("gfs.2026081506/tmp2m.zarr");
    expect(manifest.bundles[0]!.variants![0]!.zarr?.crc32).toBe("760cef95");
    expect(manifest.bundles[1]!.zarr).toBeUndefined();

    for (const [mutation, message] of [
      [{ path: "gfs.2026081506/tmp2m.zip" }, "path"],
      [{ path: "/gfs.2026081506/tmp2m.zarr" }, "path"],
      [{ path: "../tmp2m.zarr" }, "path"],
      [{ byteLength: 0 }, "byteLength"],
      [{ crc32: "760CEF95" }, "crc32"],
    ] as const) {
      const broken = manifestFixture();
      storeBearing(broken).zarr = { ...zarrFixture(), ...mutation };
      expect(() => validateManifest(broken)).toThrow(message);
    }
    // The same store named twice, on the bundle and on its variant.
    const twice = manifestFixture();
    storeBearing(twice).zarr = zarrFixture();
    storeBearing(twice).variants[0]!.zarr = zarrFixture();
    expect(() => validateManifest(twice)).toThrow("duplicate");
    const notAnObject = manifestFixture();
    storeBearing(notAnObject).zarr = "gfs.2026081506/tmp2m.zarr";
    expect(() => validateManifest(notAnObject)).toThrow("object");
  });
});

describe("pickBundleVariant", () => {
  const half = variantFixture();

  it("returns null without variants", () => {
    expect(pickBundleVariant(undefined, 500, false)).toBeNull();
    expect(pickBundleVariant([], 500, true)).toBeNull();
  });

  it("always picks the smallest tier on constrained networks", () => {
    expect(pickBundleVariant([half], 4000, true)).toEqual(half);
  });

  it("picks the smallest tier that covers the view, else full resolution", () => {
    // Zoomed far out on a 1x display: 720 columns cover the view.
    expect(pickBundleVariant([half], 512, false)).toEqual(half);
    // Default view on a retina display needs more than 720 columns.
    expect(pickBundleVariant([half], 1607, false)).toBeNull();
  });

  it("lets a pinned preference override the view and the network", () => {
    // ?res=half: the reduced tier however wide the view is.
    expect(pickBundleVariant([half], 4000, false, "half")).toEqual(half);
    // ?res=full: the canonical bundle even on a metered connection.
    expect(pickBundleVariant([half], 512, true, "full")).toBeNull();
    // Explicit auto is the heuristic, unchanged.
    expect(pickBundleVariant([half], 512, false, "auto")).toEqual(half);
    expect(pickBundleVariant([half], 1607, false, "auto")).toBeNull();
  });

  it("falls back to full resolution when a dataset ships no tiers", () => {
    expect(pickBundleVariant(undefined, 512, false, "half")).toBeNull();
    expect(pickBundleVariant([], 512, true, "half")).toBeNull();
  });

  it("scales the need to a regional grid's own longitude span", () => {
    // The MRMS mosaic: 3500 columns over 70° of longitude, a 1750-column
    // half tier. A phone on the national view (world width 4096 px at
    // dpr 2) needs 4096 × 70 / 360 ≈ 800 columns of it — the half tier —
    // where the world's width compared bare would never take it.
    const regionalHalf = { ...half, width: 1750, height: 875 };
    expect(pickBundleVariant([regionalHalf], 4096, false, "auto", 70)).toEqual(regionalHalf);
    expect(pickBundleVariant([regionalHalf], 4096, false)).toBeNull();
    // A retina desktop on the same view (14 000 px) needs ~2 700 columns:
    // full resolution, and rightly so.
    expect(pickBundleVariant([regionalHalf], 14000, false, "auto", 70)).toBeNull();
    // A global grid's span is the world's, so the default is unchanged.
    expect(pickBundleVariant([half], 1607, false, "auto", 360)).toBeNull();
    expect(pickBundleVariant([half], 512, false, "auto", 360)).toEqual(half);
    // The pins and the constrained network still outrank the view.
    expect(pickBundleVariant([regionalHalf], 14000, true, "auto", 70)).toEqual(regionalHalf);
    expect(pickBundleVariant([regionalHalf], 512, false, "full", 70)).toBeNull();
  });
});

describe("crc32", () => {
  it("matches the IEEE reference vector", () => {
    // CRC-32/IEEE("123456789") = 0xCBF43926.
    expect(crc32Of(new TextEncoder().encode("123456789"))).toBe("cbf43926");
  });

  it("is chunking-invariant", () => {
    const data = new Uint8Array(1024).map((_, index) => (index * 31) & 0xff);
    let crc = CRC32_INITIAL;
    for (let offset = 0; offset < data.length; offset += 100) {
      crc = crc32Update(crc, data.subarray(offset, offset + 100));
    }
    expect(crc32Hex(crc)).toBe(crc32Of(data));
  });
});

describe("modelDefaultVariable", () => {
  it("opens the radar mosaic on its reflectivity and a forecast on the app's default", () => {
    expect(modelDefaultVariable("mrms", "prate")).toBe("cref");
    // The JMA nowcast publishes rate classes, not a reflectivity.
    expect(modelDefaultVariable("jma", "tmp2m")).toBe("prate");
    for (const model of ["gfs", "sflux", "ecmwf", "hrrr"] as const) {
      expect(modelDefaultVariable(model, "prate")).toBe("prate");
    }
  });
});

describe("dataset kinds", () => {
  it("marks the radar mosaics as observations and the forecasts as forecasts", () => {
    // Mirrors SourceSpec.observation in xue/sources.py; the viewer titles its
    // timeline off this (run cycle and lead time vs. series start and elapsed).
    expect(isObservationModel("cma")).toBe(true);
    expect(isObservationModel("mrms")).toBe(true);
    expect(isObservationModel("jma")).toBe(true);
    for (const model of ["gfs", "sflux", "ecmwf", "aifs", "hrrr"] as const) expect(isObservationModel(model)).toBe(false);
  });

  it("lists the live feeds, the three observation windows among them", () => {
    // Every live feed has a pointer to poll (mirrors
    // SourceSpec.latest_filename); the three observation windows are the
    // last of the switch order.
    expect(FORECAST_MODEL_IDS).toEqual(["gfs", "sflux", "ecmwf", "aifs", "hrrr", "mrms", "jma", "cma"]);
    for (const model of FORECAST_MODEL_IDS) expect(FORECAST_MODELS[model].latestFilename).toBeDefined();
    expect(FORECAST_MODELS.aifs).toMatchObject({ label: "AIFS", product: "aifs-single-0p25", latestFilename: "latest-aifs.json" });
    expect(FORECAST_MODELS.mrms.latestFilename).toBe("latest-mrms.json");
    expect(FORECAST_MODELS.jma.latestFilename).toBe("latest-jma.json");
    expect(FORECAST_MODELS.cma.latestFilename).toBe("latest-cma.json");
  });

  it("opens the CMA mosaic on its reflectivity over its own region", () => {
    // Mirrors the `cma` entry of SOURCES: the mosaic ships cref alone, on
    // the zoom-5 tile grid the archive keeps.
    expect(modelDefaultVariable("cma", "tmp2m")).toBe("cref");
    expect(FORECAST_MODELS.cma.coreBundles).toEqual(["cref"]);
    expect(FORECAST_MODELS.cma.region).toEqual([67.5, 11.25, 146.25, 56.25]);
  });

  it("accepts a live pointer naming a round of a rolling window", () => {
    // The MRMS pointer names `<model>.<run>/<HHMM>/manifest.json`: one
    // directory deeper than a forecast's, relative like it, and the run
    // stays the window's first hour.
    const pointer = {
      schemaVersion: 1,
      model: "NOAA-MRMS",
      product: "conus-cref",
      run: "2026091321",
      runTime: "2026-09-13T21:00:00Z",
      manifestPath: "mrms.2026091321/0035/manifest.json",
      manifestCrc32: "0badf00d",
    };
    expect(validateLatestPointer(pointer, "mrms").manifestPath).toBe("mrms.2026091321/0035/manifest.json");
    expect(() => validateLatestPointer(pointer, "cma")).toThrow();
  });

  it("admits an mrms case manifest by its own identity and core set", () => {
    // Mirrors the `mrms` entry of SOURCES: the NOAA mosaic is a second
    // radar dataset, keyed by its own model and product strings, with the
    // reflectivity as its one required bundle and the rate optional.
    const mrms = {
      schemaVersion: 5,
      model: "NOAA-MRMS",
      product: "conus-cref",
      runTime: "2021-08-29T12:00:00Z",
      forecastHours: 12,
      bundles: [
        { variable: "cref", path: "showcase/ida-2021/cref.xue", byteLength: 1, crc32: "00000000" },
        { variable: "prate", path: "showcase/ida-2021/prate.xue", byteLength: 1, crc32: "00000001" },
      ],
    };
    expect(validateManifest(mrms, "mrms").bundles).toHaveLength(2);
    expect(validateManifest({ ...mrms, bundles: [mrms.bundles[0]!] }, "mrms").bundles).toHaveLength(1);
    expect(() => validateManifest({ ...mrms, bundles: [mrms.bundles[1]!] }, "mrms")).toThrow(/no bundle for variable cref/);
    // The two mosaics are not interchangeable.
    expect(() => validateManifest(mrms, "cma")).toThrow();
    expect(FORECAST_MODELS.mrms.region).toEqual([-130, 20, -60, 55]);
  });

  it("admits a jma manifest by its own identity, with the rate as its core", () => {
    // Mirrors the `jma` entry of SOURCES: the nowcast is precipitation
    // intensity classes under prate, the one bundle a live window must ship.
    const jma = {
      schemaVersion: 5,
      model: "JMA-HRPNS",
      product: "japan-prate",
      runTime: "2026-09-16T00:00:00Z",
      forecastHours: 3,
      bundles: [{ variable: "prate", path: "prate.xue", byteLength: 1, crc32: "00000000" }],
    };
    expect(validateManifest(jma, "jma").bundles).toHaveLength(1);
    expect(() => validateManifest({ ...jma, bundles: [{ ...jma.bundles[0]!, variable: "cref" }] }, "jma")).toThrow(
      /no bundle for variable prate/,
    );
    expect(() => validateManifest(jma, "mrms")).toThrow();
    expect(FORECAST_MODELS.jma.region).toEqual([121, 20.5, 149, 45.5]);
    expect(FORECAST_MODELS.jma.coreBundles).toEqual(["prate"]);
  });

  it("requires each dataset's own core bundles of a live manifest", () => {
    // Mirrors SourceSpec.core_bundle_ids: the tmp2m/prate pair on a
    // forecast, the reflectivity alone on a radar mosaic, which has no
    // temperature to publish.
    const radar = {
      schemaVersion: 5,
      model: "CMA-RADAR",
      product: "l3-mst-cref",
      runTime: "2026-05-10T00:00:00Z",
      forecastHours: 6,
      bundles: [{ variable: "cref", path: "showcase/x/cref.xue", byteLength: 1, crc32: "00000000" }],
    };
    expect(validateManifest(radar, "cma").bundles).toHaveLength(1);
    const withoutReflectivity = { ...radar, bundles: [{ ...radar.bundles[0]!, variable: "prate" }] };
    expect(() => validateManifest(withoutReflectivity, "cma")).toThrow(/no bundle for variable cref/);
    expect(() => validateManifest(withoutReflectivity, "cma", { requireCoreVariables: false })).not.toThrow();
    for (const model of ["gfs", "sflux", "ecmwf", "hrrr"] as const) expect(FORECAST_MODELS[model].coreBundles).toBeUndefined();
    expect(FORECAST_MODELS.mrms.coreBundles).toEqual(["cref"]);
  });
});

describe("parseBundleMetadata", () => {
  const metadata = {
    schemaVersion: 1,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2026-08-15T06:00:00Z",
    time: { firstForecastHour: 0, stepHours: 1, frameCount: 121 },
    grid: { width: 1440, height: 721 },
    variables: [
      {
        numericId: 1,
        id: "tmp2m",
        label: "2 meter temperature",
        unit: "°C",
        quantization: {
          type: "linear",
          offset: -60,
          scale: 0.5,
          minimumCode: 0,
          maximumCode: 220,
          nodataCode: 255,
        },
      },
    ],
  };

  it("accepts valid embedded metadata", () => {
    const parsed = parseBundleMetadata(JSON.stringify(metadata));
    expect(parsed.time.frameCount).toBe(121);
  });

  const mixedHours = [0, 1, 2, 3, 6, 9];
  const mixedMetadata = {
    ...metadata,
    schemaVersion: 2,
    time: { firstForecastHour: 0, frameCount: mixedHours.length, hours: mixedHours },
  };

  const parameter = {
    discipline: 0,
    parameterCategory: 0,
    parameterNumber: 0,
    typeOfFirstFixedSurface: 103,
    scaleFactorOfFirstFixedSurface: 0,
    scaledValueOfFirstFixedSurface: 2,
  };
  const v3Metadata = {
    ...metadata,
    schemaVersion: 3,
    time: { unitSeconds: 3600, firstFrameOffset: 0, frameCount: 121, frameStep: 1 },
    variables: [{ ...metadata.variables[0], parameter }],
  };

  it("still reads the schemaVersion 1 and 2 whole-hour axes", () => {
    // Published runs carry these; their offsets are their forecast hours.
    const mixed = parseBundleMetadata(JSON.stringify(mixedMetadata));
    expect(frameOffsets(mixed.time)).toEqual(mixedHours);
    expect(axisUnitSeconds(mixed.time)).toBe(3600);
    const uniform = parseBundleMetadata(JSON.stringify(metadata));
    const offsets = frameOffsets(uniform.time);
    expect(offsets).toHaveLength(121);
    expect(offsets[0]).toBe(0);
    expect(offsets[120]).toBe(120);
    expect(axisUnitSeconds(uniform.time)).toBe(3600);
  });

  it("reads a sub-hourly schemaVersion 3 axis", () => {
    // The radar mosaic publishes every six minutes, with gaps where a slot
    // was never published.
    const sixMinute = {
      ...v3Metadata,
      time: { unitSeconds: 360, firstFrameOffset: 0, frameCount: 4, frameOffsets: [0, 1, 2, 4] },
    };
    const parsed = parseBundleMetadata(JSON.stringify(sixMinute));
    expect(axisUnitSeconds(parsed.time)).toBe(360);
    expect(frameOffsets(parsed.time)).toEqual([0, 1, 2, 4]);
    expect(sameTimeAxis(parsed.time, metadata.time as never)).toBe(false);
  });

  it("rejects a unit finer or coarser than the axis needs", () => {
    // An hourly series cannot call itself six-minute...
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({
          ...v3Metadata,
          time: { unitSeconds: 360, firstFrameOffset: 0, frameCount: 3, frameStep: 10 },
        }),
      ),
    ).toThrow();
    // ...and the unit must divide an hour.
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({
          ...v3Metadata,
          time: { unitSeconds: 7, firstFrameOffset: 0, frameCount: 3, frameStep: 1 },
        }),
      ),
    ).toThrow();
  });

  it("keeps the two time-block shapes apart", () => {
    // A v3 block below version 3, and the v1/v2 keys inside a v3 file.
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({
          ...metadata,
          time: { unitSeconds: 3600, firstFrameOffset: 0, frameCount: 3, frameStep: 1 },
        }),
      ),
    ).toThrow();
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({ ...v3Metadata, time: { firstForecastHour: 0, stepHours: 1, frameCount: 3 } }),
      ),
    ).toThrow();
  });

  it("compares axes by frame list", () => {
    const uniform = parseBundleMetadata(JSON.stringify(metadata)).time;
    const mixed = parseBundleMetadata(JSON.stringify(mixedMetadata)).time;
    expect(sameTimeAxis(uniform, { ...uniform })).toBe(true);
    expect(sameTimeAxis(uniform, mixed)).toBe(false);
  });

  it("accepts a schemaVersion 3 GRIB2 parameter block", () => {
    const parsed = parseBundleMetadata(JSON.stringify(v3Metadata));
    expect(parsed.variables[0]!.parameter?.typeOfFirstFixedSurface).toBe(103);
    // A surface with no value writes both halves null, as GRIB2 does.
    const entireAtmosphere = {
      ...v3Metadata,
      variables: [
        {
          ...v3Metadata.variables[0],
          id: "cref",
          parameter: {
            discipline: 0,
            parameterCategory: 16,
            parameterNumber: 5,
            typeOfFirstFixedSurface: 10,
            scaleFactorOfFirstFixedSurface: null,
            scaledValueOfFirstFixedSurface: null,
          },
        },
      ],
    };
    expect(parseBundleMetadata(JSON.stringify(entireAtmosphere)).schemaVersion).toBe(3);
  });

  it("ties the parameter block to schema version 3", () => {
    // Required at 3, forbidden below, and the version is the lowest able to
    // express the metadata — a v3 file with a uniform axis is still v3.
    expect(() =>
      parseBundleMetadata(JSON.stringify({ ...v3Metadata, variables: metadata.variables })),
    ).toThrow();
    expect(() => parseBundleMetadata(JSON.stringify({ ...v3Metadata, schemaVersion: 1 }))).toThrow();
    expect(() => parseBundleMetadata(JSON.stringify({ ...v3Metadata, schemaVersion: 2 }))).toThrow();
    const mixedV3 = {
      ...v3Metadata,
      time: { unitSeconds: 3600, firstFrameOffset: 0, frameCount: mixedHours.length, frameOffsets: mixedHours },
    };
    expect(parseBundleMetadata(JSON.stringify(mixedV3)).schemaVersion).toBe(3);
  });

  it("rejects malformed parameter blocks", () => {
    for (const broken of [
      { ...parameter, parameterNumber: 256 },
      { ...parameter, discipline: "0" },
      { ...parameter, scaledValueOfFirstFixedSurface: null },
      { ...parameter, levelValue: 2 },
    ]) {
      expect(() =>
        parseBundleMetadata(
          JSON.stringify({ ...v3Metadata, variables: [{ ...metadata.variables[0], parameter: broken }] }),
        ),
      ).toThrow();
    }
    const { scaleFactorOfFirstFixedSurface: _dropped, ...incomplete } = parameter;
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({ ...v3Metadata, variables: [{ ...metadata.variables[0], parameter: incomplete }] }),
      ),
    ).toThrow();
  });

  it("reads the optional band and producer blocks beside the parameter", () => {
    const band = {
      satelliteSeries: 0,
      satelliteNumber: 174,
      instrumentType: 297,
      scaleFactorOfCentralWaveNumber: 0,
      scaledValueOfCentralWaveNumber: 96061,
    };
    const producer = { id: "shachen", version: "0.3.1" };
    const withBlocks = (extra: Record<string, unknown>, schemaVersion = 3) =>
      JSON.stringify({
        ...v3Metadata,
        schemaVersion,
        variables: [{ ...metadata.variables[0], parameter, ...extra }],
      });
    const parsed = parseBundleMetadata(withBlocks({ band, producer }));
    expect(parsed.schemaVersion).toBe(3);
    expect(parsed.variables[0]!.band?.scaledValueOfCentralWaveNumber).toBe(96061);
    expect(parsed.variables[0]!.producer?.id).toBe("shachen");
    expect(parseBundleMetadata(withBlocks({ band })).variables[0]!.producer).toBeUndefined();
    // Valid only at schema version 3, like the parameter block they sit beside.
    expect(() =>
      parseBundleMetadata(JSON.stringify({ ...metadata, variables: [{ ...metadata.variables[0], band }] })),
    ).toThrow();
    expect(() =>
      parseBundleMetadata(JSON.stringify({ ...metadata, variables: [{ ...metadata.variables[0], producer }] })),
    ).toThrow();
    // Present whole or absent: null, a missing field, a value out of range,
    // a wrong type and an extra key are all refused.
    const { instrumentType: _dropped, ...incompleteBand } = band;
    for (const broken of [
      { band: null },
      { band: incompleteBand },
      { band: { ...band, satelliteNumber: 65536 } },
      { band: { ...band, satelliteNumber: "174" } },
      { band: { ...band, channel: 13 } },
      { producer: null },
      { producer: { id: "shachen" } },
      { producer: { id: "Shachen", version: "0.3.1" } },
      { producer: { id: "shachen", version: "" } },
      { producer: { ...producer, url: "x" } },
    ]) {
      expect(() => parseBundleMetadata(withBlocks(broken))).toThrow();
    }
  });

  it("rejects unsupported schema versions and broken time axes", () => {
    expect(() => parseBundleMetadata(JSON.stringify({ ...metadata, schemaVersion: 4 }))).toThrow();
    expect(() =>
      parseBundleMetadata(JSON.stringify({ ...metadata, time: { frameCount: 0 } })),
    ).toThrow();
    // Every axis has exactly one encoding: a uniform stepHours axis must not
    // declare schemaVersion 2, an hours axis must not declare schemaVersion 1.
    expect(() => parseBundleMetadata(JSON.stringify({ ...metadata, schemaVersion: 2 }))).toThrow();
    expect(() => parseBundleMetadata(JSON.stringify({ ...mixedMetadata, schemaVersion: 1 }))).toThrow();
    // Declaring both stepHours and hours, or neither.
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({ ...mixedMetadata, time: { ...mixedMetadata.time, stepHours: 1 } }),
      ),
    ).toThrow();
    expect(() =>
      parseBundleMetadata(
        JSON.stringify({ ...mixedMetadata, time: { firstForecastHour: 0, frameCount: 6 } }),
      ),
    ).toThrow();
    // A uniform hours array, a non-increasing axis, a length mismatch, a
    // wrong first element, and an hour beyond the u16 payload range.
    for (const hours of [
      [0, 1, 2, 3, 4, 5],
      [0, 1, 1, 3, 6, 9],
      [0, 1, 2, 3, 6],
      [1, 2, 3, 4, 6, 9],
      [0, 1, 2, 3, 6, 65535],
    ]) {
      expect(() =>
        parseBundleMetadata(
          JSON.stringify({
            ...mixedMetadata,
            time: { firstForecastHour: hours[0] === 1 ? 0 : hours[0], frameCount: 6, hours },
          }),
        ),
      ).toThrow();
    }
  });
});

describe("palettes", () => {
  const logQuantization: LogQuantization = {
    type: "log1p",
    trace: 0.01,
    scale: 0.05,
    maximum: 128,
    minimumCode: 1,
    maximumCode: 253,
    zeroCode: 0,
    overflowCode: 254,
    nodataCode: 255,
  };

  it("decodes linear codes and rejects invalid ones", () => {
    const quantization = {
      type: "linear",
      offset: -60,
      scale: 0.5,
      minimumCode: 0,
      maximumCode: 220,
      nodataCode: 255,
    } as const;
    expect(decodeLinear(quantization, 0)).toBe(-60);
    expect(decodeLinear(quantization, 220)).toBe(50);
    expect(decodeLinear(quantization, 255)).toBeNull();
  });

  it("decodes log codes with a strictly increasing overflow step", () => {
    expect(decodeLog(logQuantization, 0)).toBe(0);
    expect(decodeLog(logQuantization, 253)).toBeCloseTo(128, 6);
    const overflow = decodeLog(logQuantization, 254);
    expect(overflow).not.toBeNull();
    expect(overflow!).toBeGreaterThan(128);
    expect(decodeLog(logQuantization, 255)).toBeNull();
  });

  it("builds a 256x1 RGBA palette with transparent dry and nodata codes", () => {
    const variable: BundleVariable = {
      numericId: 2,
      id: "prate",
      label: "Precipitation rate",
      unit: "mm/h",
      quantization: logQuantization,
    };
    const palette = buildPalette(variable);
    expect(palette.length).toBe(1024);
    expect(palette[3]).toBe(0); // code 0: dry, fully transparent
    expect(palette[253 * 4 + 3]).toBe(255); // top in-range code opaque
    expect(palette[255 * 4 + 3]).toBe(0); // nodata transparent
  });

  it("builds the solar ramp with a transparent night side and opaque noon glare", () => {
    const variable: BundleVariable = {
      numericId: 5,
      id: "dswrf",
      label: "Downward shortwave radiation flux",
      unit: "W/m²",
      quantization: {
        type: "linear",
        offset: 0,
        scale: 5,
        minimumCode: 0,
        maximumCode: 254,
        nodataCode: 255,
      },
    };
    const palette = buildPalette(variable);
    expect(palette.length).toBe(1024);
    expect(palette[3]).toBe(0); // 0 W/m²: night, fully transparent
    expect(palette[254 * 4 + 3]).toBe(255); // 1270 W/m² opaque
    expect(palette[255 * 4 + 3]).toBe(0); // nodata transparent
    // High end is warm (red-dominant), unlike the temperature ramp's violet top.
    expect(palette[254 * 4]!).toBeGreaterThan(200);
  });

  it("builds an opaque wind speed ramp from calm blue to violent violet", () => {
    const palette = buildWindSpeedPalette();
    expect(palette.length).toBe(1024);
    expect(palette[3]).toBe(255); // calm end opaque
    expect(palette[255 * 4 + 3]).toBe(255); // ramp ceiling opaque
    expect(palette[2]!).toBeGreaterThan(palette[0]!); // blue-dominant at 0 m/s
    expect(palette[255 * 4]!).toBeGreaterThan(palette[255 * 4 + 1]!); // red over green at 40 m/s
  });
});

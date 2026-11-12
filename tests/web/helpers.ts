/** Fixture builders the web unit tests share. */

import registryText from "../../rust/xue/registry.json?raw";

import type {
  BundleAerosol,
  BundleBand,
  BundleEnsemble,
  BundleMetadata,
  BundleParameter,
  BundleVariable,
  LinearQuantization,
  LogQuantization,
} from "../../web/src/manifest";

/** One variable of the registry document (`rust/xue/registry.json`, the
 * one both encoders build from): its label, unit, parameter block, the
 * optional `aerosol` / `producer` / `ensemble` blocks, a `band` block per satellite
 * platform that publishes it, and its two smallest codebooks lifted out
 * of `quantization`. */
export interface RegistryEntry<Q extends LinearQuantization | LogQuantization = LinearQuantization> {
  label: string;
  unit: string;
  parameter: BundleParameter;
  aerosol?: BundleAerosol;
  producer?: { id: string };
  ensemble?: BundleEnsemble;
  band?: Record<string, BundleBand>;
  quality: Q;
  compact: Q;
}

interface RegistryDocument {
  variables: Record<
    string,
    {
      label: string;
      unit: string;
      parameter: BundleParameter;
      aerosol?: BundleAerosol;
      producer?: { id: string };
      ensemble?: BundleEnsemble;
      quantization?: Record<"quality" | "compact" | "balanced", LinearQuantization | LogQuantization>;
    }
  >;
  bundles: Record<string, { kind: string; members?: string[] }>;
  sources: Record<
    string,
    {
      platform: string | null;
      bands: [string, { satelliteSeries: number; satelliteNumber: number; instrumentType: number; centralWavenumber: number }][];
      bundleScalarIds: string[];
      bundleVectorIds: string[];
      bundleCompositeIds: string[];
      bundleVolumeIds: string[];
    }
  >;
}

/** The whole registry document, imported as text so the type checker does
 * not infer a type for its twenty thousand lines. */
export const REGISTRY_DOCUMENT: RegistryDocument = JSON.parse(registryText) as RegistryDocument;

/** The registry's entries for `ids`, in that order. */
export function registryEntries<Q extends LinearQuantization | LogQuantization = LinearQuantization>(
  ids: readonly string[],
): Record<string, RegistryEntry<Q>> {
  const entries: Record<string, RegistryEntry<Q>> = {};
  for (const id of ids) {
    const variable = REGISTRY_DOCUMENT.variables[id];
    if (!variable?.quantization) throw new Error(`${id} is no published variable of the registry`);
    const entry: RegistryEntry<Q> = {
      label: variable.label,
      unit: variable.unit,
      parameter: variable.parameter,
      quality: variable.quantization.quality as Q,
      compact: variable.quantization.compact as Q,
    };
    if (variable.aerosol) entry.aerosol = variable.aerosol;
    if (variable.producer) entry.producer = variable.producer;
    if (variable.ensemble) entry.ensemble = variable.ensemble;
    for (const source of Object.values(REGISTRY_DOCUMENT.sources)) {
      const band = source.bands.find(([bandId]) => bandId === id)?.[1];
      if (!band || source.platform === null) continue;
      entry.band ??= {};
      entry.band[source.platform] ??= {
        satelliteSeries: band.satelliteSeries,
        satelliteNumber: band.satelliteNumber,
        instrumentType: band.instrumentType,
        scaleFactorOfCentralWaveNumber: 0,
        scaledValueOfCentralWaveNumber: band.centralWavenumber,
      };
    }
    entries[id] = entry;
  }
  return entries;
}

/** Every bundle id some source publishes. */
export function publishedBundleIds(): Set<string> {
  const ids = new Set<string>();
  for (const source of Object.values(REGISTRY_DOCUMENT.sources)) {
    for (const id of [...source.bundleScalarIds, ...source.bundleVectorIds, ...source.bundleCompositeIds, ...source.bundleVolumeIds]) {
      ids.add(id);
    }
  }
  return ids;
}

/** The metadata variable a registry entry describes, as variable 1 of its
 * bundle; `extra` adds the optional blocks (`band`, `aerosol`, …). */
export function registryVariable(
  id: string,
  entry: Pick<RegistryEntry, "label" | "unit" | "parameter">,
  quantization: LinearQuantization | LogQuantization,
  extra: Partial<BundleVariable> = {},
): BundleVariable {
  return {
    numericId: 1,
    id,
    label: entry.label,
    unit: entry.unit,
    parameter: entry.parameter,
    ...extra,
    quantization,
  };
}

/** A palette's colour at one code. */
export function rgba(palette: Uint8Array, code: number): [number, number, number, number] {
  return [...palette.subarray(code * 4, code * 4 + 4)] as [number, number, number, number];
}

/** Everything of a schema v3 bundle metadata document but its variables. */
interface MetadataEnvelope {
  model: string;
  runTime: string;
  time: Record<string, number>;
  grid: Record<string, number | boolean>;
}

/** A Himawari full-disk window: ten-minute frames on the 0.04° disk grid. */
export const HIMAWARI_ENVELOPE: MetadataEnvelope = {
  model: "HIMAWARI",
  runTime: "2026-09-17T03:00:00Z",
  time: { unitSeconds: 600, firstFrameOffset: 0, frameCount: 2, frameStep: 1 },
  grid: { width: 3000, height: 3000, firstLongitude: 80.72, firstLatitude: 59.98, longitudeStep: 0.04, latitudeStep: -0.04, wrapLongitude: false },
};

/** A schema v3 bundle metadata document, as the JSON a container carries. */
export function metadataJson(envelope: MetadataEnvelope, variables: BundleVariable[]): string {
  return JSON.stringify({ schemaVersion: 3, ...envelope, variables });
}

/** The GFS quarter-degree global grid, three hourly frames and no
 * variables; `overrides` replaces grid fields. */
export function globalGrid(overrides: Record<string, unknown> = {}): BundleMetadata {
  return {
    schemaVersion: 3,
    model: "GFS",
    runTime: "2026-08-15T06:00:00Z",
    time: { frameCount: 3, unitSeconds: 3600, firstFrameOffset: 0, frameStep: 1 },
    grid: {
      width: 1440,
      height: 721,
      firstLongitude: -180,
      firstLatitude: 90,
      longitudeStep: 0.25,
      latitudeStep: -0.25,
      wrapLongitude: true,
      ...overrides,
    },
    variables: [],
  } as unknown as BundleMetadata;
}

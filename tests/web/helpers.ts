/** Fixture builders the web unit tests share. */

import type {
  BundleMetadata,
  BundleParameter,
  BundleVariable,
  LinearQuantization,
  LogQuantization,
} from "../../web/src/manifest";

/** One entry of a committed registry fixture (`tests/fixtures/*-registry.json`)
 * that both encoders are held to: a variable's label, unit, parameter block
 * and its two codebooks. */
export interface RegistryEntry<Q extends LinearQuantization | LogQuantization = LinearQuantization> {
  label: string;
  unit: string;
  parameter: BundleParameter;
  quality: Q;
  compact: Q;
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

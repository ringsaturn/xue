import { REGISTRY } from "./registry";

/**
 * The registry's `quality` codebooks as the chart reads them: the span a
 * linear codebook covers and the shape of a logarithmic one. The palette
 * domains and legend ranges in levels.ts and pressure.ts are drawn over
 * these, so a codebook that moves in xuebuild/quantize.py moves the chart
 * with it.
 */

export type RegistryVariableId = keyof typeof REGISTRY.variables;

/** A code's value, rounded to twelve significant digits: offset + scale ×
 * code in binary floating point leaves a tail (0.2 × 254 is
 * 50.800000000000004) that would otherwise reach a legend. */
function codeValue(offset: number, scale: number, code: number): number {
  return Number((offset + scale * code).toPrecision(12));
}

/** The values a linear codebook holds, from its first code to its last. */
export function linearCodebookRange(id: RegistryVariableId): readonly [number, number] {
  const quality = REGISTRY.variables[id].quality;
  if (quality.type !== "linear") throw new Error(`${id} has no linear codebook`);
  return [codeValue(quality.offset, 0, 0), codeValue(quality.offset, quality.scale, quality.maximumCode)];
}

/** The first value of a linear codebook. */
export function linearCodebookOffset(id: RegistryVariableId): number {
  return linearCodebookRange(id)[0];
}

/** A logarithmic codebook's trace, scale and maximum. */
export function logCodebook(id: RegistryVariableId): { trace: number; scale: number; maximum: number } {
  const quality = REGISTRY.variables[id].quality;
  if (quality.type !== "log1p") throw new Error(`${id} has no log1p codebook`);
  return { trace: quality.trace, scale: quality.scale, maximum: quality.maximum };
}

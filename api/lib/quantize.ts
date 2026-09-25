/** Code → physical value, mirroring `web/src/palettes.ts::decodeLinear` /
 * `decodeLog` (and `decodeValue`). Kept inline so the API does not import the
 * frontend's palette module; `palettes.ts` is the reference. */

export interface LinearQuantization {
  type: "linear";
  offset: number;
  scale: number;
  minimumCode: number;
  maximumCode: number;
  nodataCode: number;
}

export interface LogQuantization {
  type: "log1p";
  trace: number;
  scale: number;
  maximum: number;
  minimumCode: number;
  maximumCode: number;
  zeroCode: number;
  overflowCode: number;
  nodataCode: number;
}

export type Quantization = LinearQuantization | LogQuantization;

export function decodeLinear(quantization: LinearQuantization, code: number): number | null {
  if (code === quantization.nodataCode || code > quantization.maximumCode) return null;
  return quantization.offset + code * quantization.scale;
}

export function decodeLog(quantization: LogQuantization, code: number): number | null {
  if (code === quantization.nodataCode) return null;
  if (code === quantization.zeroCode) return 0;
  if (code > quantization.overflowCode) return null;
  const lo = Math.log1p(quantization.trace / quantization.scale);
  const hi = Math.log1p(quantization.maximum / quantization.scale);
  const unit = (code - quantization.minimumCode) / (quantization.maximumCode - quantization.minimumCode);
  return quantization.scale * Math.expm1(lo + unit * (hi - lo));
}

export function decodeValue(quantization: Quantization, code: number): number | null {
  return quantization.type === "linear" ? decodeLinear(quantization, code) : decodeLog(quantization, code);
}

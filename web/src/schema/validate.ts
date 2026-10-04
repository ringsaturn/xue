/** The little validators the point products' parsers (`stations/schema.ts`,
 * `tc/schema.ts`) are written in. Each names the field it refused, so a
 * rejected product says which value cost it. */

export const CRC32 = /^[0-9a-f]{8}$/;

export function object(input: unknown, label: string): Record<string, unknown> {
  if (typeof input !== "object" || input === null || Array.isArray(input))
    throw new Error(`${label} must be an object`);
  return input as Record<string, unknown>;
}

/** A UTC ISO 8601 timestamp (`…Z`) that parses. */
export function timestamp(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.endsWith("Z"))
    throw new Error(`${label} must be a UTC timestamp`);
  if (!Number.isFinite(Date.parse(value)))
    throw new Error(`${label} is not a valid timestamp`);
  return value;
}

export function integer(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isInteger(value))
    throw new Error(`${label} must be an integer`);
  if (value < minimum) throw new Error(`${label} must be at least ${minimum}`);
  return value;
}

export function string(value: unknown, label: string, pattern?: RegExp): string {
  if (typeof value !== "string") throw new Error(`${label} must be a string`);
  if (pattern && !pattern.test(value)) throw new Error(`${label} is malformed`);
  return value;
}

/** A version this reader implements, or a refusal. Only an *over*
 * declaration is refused: the product may add an optional field without
 * bumping the version, and a reader that insisted on equality would
 * reject its own contract's next revision for nothing. */
export function schemaVersion(value: unknown, label: string, supported: number): number {
  const version = integer(value, `${label}.schemaVersion`, 1);
  if (version > supported)
    throw new Error(`unsupported ${label} schema version ${version}`);
  return version;
}

/** A point product's live pointer (`latest-<product>.json`), schema v1. */
export interface ProductPointer<P extends string> {
  schemaVersion: 1;
  product: P;
  issued: string;
  path: string;
  byteLength: number;
  crc32: string;
}

export function pointer<P extends string>(
  input: unknown,
  product: P,
  path: RegExp,
  supported: number,
): ProductPointer<P> {
  const label = `${product} pointer`;
  const value = object(input, label);
  schemaVersion(value.schemaVersion, label, supported);
  if (value.product !== product)
    throw new Error(`${label} product must be ${product}`);
  return {
    schemaVersion: 1,
    product,
    issued: timestamp(value.issued, `${label}.issued`),
    path: string(value.path, `${label}.path`, path),
    byteLength: integer(value.byteLength, `${label}.byteLength`, 1),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
  };
}

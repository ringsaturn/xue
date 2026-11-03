/** The checks the generated validators (`generated/`, from
 * `web/tooling/schemagen.ts`) are written in: one per JSON Schema keyword
 * family, each naming the field it refused. Kept here, once, so a generated
 * module is a list of calls rather than the same messages over again. */

export function fail(message: string): never {
  throw new Error(message);
}

export function isBoolean(value: unknown, at: string): asserts value is boolean {
  if (typeof value !== "boolean") fail(`${at} must be a boolean`);
}

function bounded(value: number, at: string, minimum?: number, maximum?: number): void {
  if (minimum !== undefined && value < minimum) fail(`${at} must be at least ${minimum}`);
  if (maximum !== undefined && value > maximum) fail(`${at} must be at most ${maximum}`);
}

export function isInteger(value: unknown, at: string, minimum?: number, maximum?: number): asserts value is number {
  if (typeof value !== "number" || !Number.isInteger(value)) fail(`${at} must be an integer`);
  bounded(value, at, minimum, maximum);
}

export function isNumber(value: unknown, at: string, minimum?: number, maximum?: number): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(`${at} must be a number`);
  bounded(value, at, minimum, maximum);
}

export function isAbove(value: number, at: string, exclusiveMinimum: number): void {
  if (value <= exclusiveMinimum) fail(`${at} must be above ${exclusiveMinimum}`);
}

export function isMultipleOf(value: number, at: string, factor: number): void {
  if (value % factor !== 0) fail(`${at} must be a multiple of ${factor}`);
}

/** A string, matching `pattern`, at least `minLength` code points long,
 * and parsing as a date when `timestamp` is set (`format: date-time`). */
export function isString(
  value: unknown,
  at: string,
  pattern?: RegExp,
  minLength?: number,
  timestamp?: boolean,
): asserts value is string {
  if (typeof value !== "string") fail(`${at} must be a string`);
  if (minLength !== undefined && [...value].length < minLength) fail(`${at} must be at least ${minLength} characters`);
  if (pattern && !pattern.test(value)) fail(`${at} is malformed`);
  if (timestamp && !Number.isFinite(Date.parse(value))) fail(`${at} is not a valid timestamp`);
}

export function isList(value: unknown, at: string, minItems?: number, maxItems?: number): asserts value is unknown[] {
  if (!Array.isArray(value)) fail(`${at} must be a list`);
  if (minItems !== undefined && value.length < minItems) fail(`${at} must have at least ${minItems} items`);
  if (maxItems !== undefined && value.length > maxItems) fail(`${at} must have at most ${maxItems} items`);
}

export function isObject(value: unknown, at: string): asserts value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail(`${at} must be an object`);
}

/** A required field's value. */
export function field(record: Record<string, unknown>, key: string, at: string): unknown {
  const value = record[key];
  if (value === undefined) fail(`${at}.${key} is missing`);
  return value;
}

export function isOneOf(value: unknown, at: string, allowed: readonly unknown[]): void {
  if (!allowed.includes(value)) fail(`${at} must be one of ${allowed.map((item) => JSON.stringify(item)).join(", ")}`);
}

/** `anyOf`: the value passes at least one branch. */
export function isAnyOf(value: unknown, at: string, branches: ((value: unknown, at: string) => void)[]): void {
  const passes = branches.some((branch) => {
    try {
      branch(value, at);
      return true;
    } catch {
      return false;
    }
  });
  if (!passes) fail(`${at} matches none of its allowed shapes`);
}

/** Generates the shell's point-product validators from the normative JSON
 * Schemas under `docs/schemas/` (draft 2020-12): one module per schema in
 * `web/src/schema/generated/`, each a type and a `check…` function that
 * throws an `Error` naming the refused field. The output is plain
 * TypeScript, so the shell gains no runtime dependency.
 *
 * The generator implements a closed subset of the vocabulary and refuses a
 * schema that uses anything else, so a keyword the shell would silently
 * ignore cannot reach a contract. Unknown object fields are always
 * admitted: a reader ignores what a later revision adds.
 *
 *   node web/tooling/schemagen.ts          (rewrite the modules)
 *
 * `tests/web/schemagen.test.ts` holds the committed modules to a fresh run. */

import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type Schema = { [key: string]: unknown };

const ANNOTATIONS = new Set(["$schema", "$id", "$comment", "title", "description", "examples"]);
const VALIDATION = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
  "items",
  "prefixItems",
  "minItems",
  "maxItems",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "multipleOf",
  "minLength",
  "pattern",
  "format",
  "enum",
  "const",
  "$ref",
  "$defs",
  "anyOf",
]);
const TYPES = new Set(["object", "array", "string", "integer", "number", "boolean", "null"]);

export const SCHEMA_SUFFIX = ".schema.json";

/** `fire-index` → `FireIndex`. */
function pascal(name: string): string {
  return name
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((part) => part[0]!.toUpperCase() + part.slice(1))
    .join("");
}

function literal(value: unknown): string {
  return JSON.stringify(value);
}

class Generator {
  private counter = 0;
  private readonly defs: Record<string, Schema>;
  private readonly name: string;
  private readonly root: Schema;

  constructor(name: string, root: Schema) {
    this.name = name;
    this.root = root;
    this.defs = (root.$defs as Record<string, Schema> | undefined) ?? {};
    this.audit(root, "#");
  }

  /** Refuses what the generator does not implement. */
  private audit(schema: unknown, at: string): void {
    if (typeof schema !== "object" || schema === null || Array.isArray(schema))
      throw new Error(`${this.name}: ${at} must be a schema object`);
    const node = schema as Schema;
    for (const key of Object.keys(node)) {
      if (!ANNOTATIONS.has(key) && !VALIDATION.has(key))
        throw new Error(`${this.name}: ${at} uses ${key}, which schemagen does not implement`);
    }
    if (node.$defs !== undefined && at !== "#") throw new Error(`${this.name}: $defs only at the root`);
    if (node.format !== undefined && node.format !== "date-time")
      throw new Error(`${this.name}: ${at} format ${String(node.format)} is not implemented`);
    if (node.additionalProperties === false)
      throw new Error(`${this.name}: ${at} closes an object; readers admit unknown fields`);
    if (node.$ref !== undefined) {
      const ref = String(node.$ref);
      const match = /^#\/\$defs\/([A-Za-z][A-Za-z0-9]*)$/.exec(ref);
      if (!match || !this.defs[match[1]!]) throw new Error(`${this.name}: ${at} $ref ${ref} names no local definition`);
      if (Object.keys(node).some((key) => VALIDATION.has(key) && key !== "$ref"))
        throw new Error(`${this.name}: ${at} mixes $ref with other keywords`);
    }
    const types = this.types(node);
    for (const type of types) if (!TYPES.has(type)) throw new Error(`${this.name}: ${at} type ${type} is unknown`);
    if (types.filter((type) => type !== "null").length > 1)
      throw new Error(`${this.name}: ${at} allows two non-null types; use anyOf`);
    const children: [string, unknown][] = [];
    for (const [key, value] of Object.entries((node.properties as Schema | undefined) ?? {})) children.push([`${at}/properties/${key}`, value]);
    for (const [key, value] of Object.entries((node.$defs as Schema | undefined) ?? {})) children.push([`${at}/$defs/${key}`, value]);
    if (typeof node.additionalProperties === "object") children.push([`${at}/additionalProperties`, node.additionalProperties]);
    if (node.items !== undefined && node.items !== false) children.push([`${at}/items`, node.items]);
    ((node.prefixItems as unknown[] | undefined) ?? []).forEach((value, index) => children.push([`${at}/prefixItems/${index}`, value]));
    ((node.anyOf as unknown[] | undefined) ?? []).forEach((value, index) => children.push([`${at}/anyOf/${index}`, value]));
    for (const [where, child] of children) this.audit(child, where);
  }

  private types(schema: Schema): string[] {
    if (schema.type === undefined) return [];
    return Array.isArray(schema.type) ? (schema.type as string[]) : [schema.type as string];
  }

  private readonly patterns = new Map<string, string>();

  /** The module-level constant holding a pattern, compiled once. */
  private pattern(source: string): string {
    let name = this.patterns.get(source);
    if (name === undefined) {
      name = `PATTERN_${this.patterns.size + 1}`;
      this.patterns.set(source, name);
    }
    return name;
  }

  /** The label expression `at` followed by the literal `text`, folded
   * into one string literal where `at` ends in one. */
  private label(at: string, text: string): string {
    const tail = /^(.*?)(?: \+ )?"((?:[^"\\]|\\.)*)"$/.exec(at);
    if (tail && !tail[1]!.endsWith("+")) {
      const head = tail[1]!;
      const joined = literal((JSON.parse(`"${tail[2]}"`) as string) + text);
      return head ? `${head} + ${joined}` : joined;
    }
    return `${at} + ${literal(text)}`;
  }

  private fail(at: string, text: string): string {
    return `fail(${this.label(at, text)})`;
  }

  private fresh(prefix: string): string {
    this.counter += 1;
    return `${prefix}${this.counter}`;
  }

  /** The TypeScript type a value admitted by `schema` has. */
  type(schema: Schema): string {
    if (schema.$ref !== undefined) return String(schema.$ref).slice("#/$defs/".length);
    if (schema.const !== undefined) return literal(schema.const);
    if (Array.isArray(schema.enum)) return schema.enum.map(literal).join(" | ");
    // anyOf narrows a node that has a type of its own; alone, it is a union.
    if (Array.isArray(schema.anyOf) && this.types(schema).length === 0)
      return [...new Set((schema.anyOf as Schema[]).map((branch) => this.type(branch)))].map((branch) => `(${branch})`).join(" | ");
    const types = this.types(schema);
    if (types.length === 0) return "unknown";
    return types.map((type) => this.single(schema, type)).join(" | ");
  }

  private single(schema: Schema, type: string): string {
    switch (type) {
      case "null":
        return "null";
      case "boolean":
        return "boolean";
      case "integer":
      case "number":
        return "number";
      case "string":
        return "string";
      case "array":
        return this.arrayType(schema);
      default:
        return this.objectType(schema);
    }
  }

  private arrayType(schema: Schema): string {
    const prefix = (schema.prefixItems as Schema[] | undefined) ?? [];
    const rest = schema.items === undefined ? "unknown" : schema.items === false ? null : this.type(schema.items as Schema);
    if (prefix.length === 0) return `${rest && rest.includes(" ") ? `(${rest})` : rest}[]`;
    const minimum = typeof schema.minItems === "number" ? schema.minItems : 0;
    const parts = prefix.map((item, index) => `${this.type(item)}${index < minimum ? "" : "?"}`);
    if (rest !== null) parts.push(`...(${rest})[]`);
    return `[${parts.join(", ")}]`;
  }

  private objectType(schema: Schema): string {
    const properties = (schema.properties as Record<string, Schema> | undefined) ?? {};
    const required = new Set((schema.required as string[] | undefined) ?? []);
    const fields = Object.entries(properties).map(
      ([key, value]) => `${/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key) ? key : literal(key)}${required.has(key) ? "" : "?"}: ${this.type(value)}`,
    );
    const extra = typeof schema.additionalProperties === "object" ? this.type(schema.additionalProperties as Schema) : null;
    if (fields.length === 0) return extra === null ? "Record<string, unknown>" : `Record<string, ${extra}>`;
    const known = `{ ${fields.join("; ")} }`;
    return extra === null ? known : `${known} & Record<string, ${extra}>`;
  }

  /** Statements that throw unless the value in the variable `v` is
   * admitted by `schema`; `at` is a TypeScript expression for its label. */
  check(schema: Schema, v: string, at: string, indent: string): string[] {
    const out: string[] = [];
    if (schema.$ref !== undefined) {
      out.push(`${indent}check${this.type(schema)}(${v}, ${at});`);
      return out;
    }
    const types = this.types(schema);
    const nullable = types.includes("null");
    const kind = types.find((type) => type !== "null");
    let inner = indent;
    if (nullable && kind !== undefined) {
      out.push(`${indent}if (${v} !== null) {`);
      inner = `${indent}  `;
    } else if (nullable) {
      out.push(`${indent}if (${v} !== null) ${this.fail(at, ` must be null`)};`);
    }
    if (kind !== undefined) out.push(...this.typed(schema, kind, v, at, inner, nullable));
    if (schema.const !== undefined)
      out.push(`${indent}if (${v} !== ${literal(schema.const)}) ${this.fail(at, ` must be ${literal(schema.const)}`)};`);
    if (Array.isArray(schema.enum))
      out.push(
        `${indent}if (!(${literal(schema.enum)} as unknown[]).includes(${v})) ${this.fail(at, ` must be one of ${schema.enum.map(literal).join(", ")}`)};`,
      );
    if (Array.isArray(schema.anyOf)) {
      const branches = (schema.anyOf as Schema[]).map((branch) => {
        const body = this.check(branch, "x", "a", "      ");
        return [`    (x: unknown, a: string): void => {`, ...body, `    },`].join("\n");
      });
      out.push(`${indent}if (!passesOne(${v}, ${at}, [\n${branches.join("\n")}\n${indent}])) ${this.fail(at, ` matches none of its allowed shapes`)};`);
    }
    if (nullable && kind !== undefined) out.push(`${indent}}`);
    return out;
  }

  private typed(schema: Schema, kind: string, v: string, at: string, indent: string, nullable: boolean): string[] {
    const out: string[] = [];
    const orNull = nullable ? " or null" : "";
    switch (kind) {
      case "boolean":
        out.push(`${indent}if (typeof ${v} !== "boolean") ${this.fail(at, ` must be a boolean${orNull}`)};`);
        break;
      case "integer":
      case "number": {
        const guard = kind === "integer" ? `Number.isInteger(${v})` : `Number.isFinite(${v})`;
        out.push(`${indent}if (typeof ${v} !== "number" || !${guard}) ${this.fail(at, ` must be ${kind === "integer" ? "an integer" : "a number"}${orNull}`)};`);
        if (typeof schema.minimum === "number")
          out.push(`${indent}if (${v} < ${schema.minimum}) ${this.fail(at, ` must be at least ${schema.minimum}`)};`);
        if (typeof schema.exclusiveMinimum === "number")
          out.push(`${indent}if (${v} <= ${schema.exclusiveMinimum}) ${this.fail(at, ` must be above ${schema.exclusiveMinimum}`)};`);
        if (typeof schema.maximum === "number")
          out.push(`${indent}if (${v} > ${schema.maximum}) ${this.fail(at, ` must be at most ${schema.maximum}`)};`);
        if (typeof schema.multipleOf === "number")
          out.push(`${indent}if (${v} % ${schema.multipleOf} !== 0) ${this.fail(at, ` must be a multiple of ${schema.multipleOf}`)};`);
        break;
      }
      case "string":
        out.push(`${indent}if (typeof ${v} !== "string") ${this.fail(at, ` must be a string${orNull}`)};`);
        if (typeof schema.minLength === "number")
          out.push(`${indent}if ([...${v}].length < ${schema.minLength}) ${this.fail(at, ` must be at least ${schema.minLength} characters`)};`);
        if (typeof schema.pattern === "string")
          out.push(`${indent}if (!${this.pattern(schema.pattern)}.test(${v})) ${this.fail(at, ` is malformed`)};`);
        if (schema.format === "date-time")
          out.push(`${indent}if (!Number.isFinite(Date.parse(${v}))) ${this.fail(at, ` is not a valid timestamp`)};`);
        break;
      case "array": {
        out.push(`${indent}if (!Array.isArray(${v})) ${this.fail(at, ` must be a list${orNull}`)};`);
        if (typeof schema.minItems === "number")
          out.push(`${indent}if (${v}.length < ${schema.minItems}) ${this.fail(at, ` must have at least ${schema.minItems} items`)};`);
        if (typeof schema.maxItems === "number")
          out.push(`${indent}if (${v}.length > ${schema.maxItems}) ${this.fail(at, ` must have at most ${schema.maxItems} items`)};`);
        const prefix = (schema.prefixItems as Schema[] | undefined) ?? [];
        prefix.forEach((item, index) => {
          const value = this.fresh("v");
          out.push(`${indent}if (${v}.length > ${index}) {`);
          out.push(`${indent}  const ${value}: unknown = ${v}[${index}];`);
          out.push(...this.check(item, value, this.label(at, `[${index}]`), `${indent}  `));
          out.push(`${indent}}`);
        });
        if (schema.items === false) {
          out.push(`${indent}if (${v}.length > ${prefix.length}) ${this.fail(at, ` must have at most ${prefix.length} items`)};`);
        } else if (schema.items !== undefined) {
          const position = this.fresh("i");
          const value = this.fresh("v");
          out.push(`${indent}for (let ${position} = ${prefix.length}; ${position} < ${v}.length; ${position}++) {`);
          out.push(`${indent}  const ${value}: unknown = ${v}[${position}];`);
          out.push(...this.check(schema.items as Schema, value, `${this.label(at, "[")} + ${position} + "]"`, `${indent}  `));
          out.push(`${indent}}`);
        }
        break;
      }
      case "object": {
        const record = this.fresh("o");
        out.push(`${indent}if (typeof ${v} !== "object" || ${v} === null || Array.isArray(${v})) ${this.fail(at, ` must be an object${orNull}`)};`);
        out.push(`${indent}const ${record} = ${v} as Record<string, unknown>;`);
        const required = new Set((schema.required as string[] | undefined) ?? []);
        const properties = (schema.properties as Record<string, Schema> | undefined) ?? {};
        for (const key of required)
          if (!(key in properties)) out.push(`${indent}if (${record}[${literal(key)}] === undefined) ${this.fail(at, `.${key} is missing`)};`);
        for (const [key, child] of Object.entries(properties)) {
          const value = this.fresh("v");
          out.push(`${indent}const ${value} = ${record}[${literal(key)}];`);
          if (required.has(key)) {
            out.push(`${indent}if (${value} === undefined) ${this.fail(at, `.${key} is missing`)};`);
            out.push(...this.check(child, value, this.label(at, `.${key}`), indent));
          } else {
            out.push(`${indent}if (${value} !== undefined) {`);
            out.push(...this.check(child, value, this.label(at, `.${key}`), `${indent}  `));
            out.push(`${indent}}`);
          }
        }
        if (typeof schema.additionalProperties === "object") {
          const key = this.fresh("k");
          const value = this.fresh("v");
          const known = Object.keys(properties);
          out.push(`${indent}for (const [${key}, ${value}] of Object.entries(${record})) {`);
          if (known.length > 0) out.push(`${indent}  if ((${literal(known)} as string[]).includes(${key})) continue;`);
          out.push(...this.check(schema.additionalProperties as Schema, value, `${this.label(at, ".")} + ${key}`, `${indent}  `));
          out.push(`${indent}}`);
        }
        break;
      }
    }
    return out;
  }

  module(source: string): string {
    const title = typeof this.root.title === "string" ? this.root.title : pascal(this.name);
    const out: string[] = [
      `// Generated by web/tooling/schemagen.ts from ${source}. Do not edit:`,
      `// change the schema and run \`node web/tooling/schemagen.ts\`.`,
      "",
      "function fail(message: string): never {",
      "  throw new Error(message);",
      "}",
    ];
    const usesAnyOf = JSON.stringify(this.root).includes('"anyOf"');
    if (usesAnyOf) {
      out.push(
        "",
        "function passesOne(value: unknown, label: string, branches: ((value: unknown, label: string) => void)[]): boolean {",
        "  return branches.some((branch) => {",
        "    try {",
        "      branch(value, label);",
        "      return true;",
        "    } catch {",
        "      return false;",
        "    }",
        "  });",
        "}",
      );
    }
    const body: string[] = [];
    const definitions: [string, Schema][] = [...Object.entries(this.defs), [title, { ...this.root, $defs: undefined }]];
    for (const [name, schema] of definitions) {
      const description = typeof schema.description === "string" ? schema.description : null;
      body.push("");
      if (description) body.push(`/** ${description.replace(/\*\//g, "*\\/")} */`);
      body.push(`export type ${name} = ${this.type(schema)};`);
      body.push("");
      body.push(`export function check${name}(v: unknown, at: string): asserts v is ${name} {`);
      body.push(...this.check(schema, "v", "at", "  "));
      body.push("}");
    }
    if (this.patterns.size > 0) out.push("");
    for (const [source, name] of this.patterns) out.push(`const ${name} = new RegExp(${literal(source)}, "u");`);
    out.push(...body);
    return `${out.join("\n")}\n`;
  }
}

/** Every schema's module, keyed by its file name under `generated/`. */
export function generate(schemaDirectory: string): Map<string, string> {
  const modules = new Map<string, string>();
  const names = readdirSync(schemaDirectory)
    .filter((name) => name.endsWith(SCHEMA_SUFFIX))
    .sort();
  for (const file of names) {
    const name = file.slice(0, -SCHEMA_SUFFIX.length);
    const schema = JSON.parse(readFileSync(join(schemaDirectory, file), "utf8")) as Schema;
    modules.set(`${name}.ts`, new Generator(name, schema).module(`docs/schemas/${file}`));
  }
  return modules;
}

const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
export const SCHEMA_DIRECTORY = join(REPO_ROOT, "docs/schemas");
export const GENERATED_DIRECTORY = join(REPO_ROOT, "web/src/schema/generated");

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  for (const [file, text] of generate(SCHEMA_DIRECTORY)) writeFileSync(join(GENERATED_DIRECTORY, file), text);
}

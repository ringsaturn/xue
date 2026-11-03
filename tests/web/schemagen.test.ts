/** The generated point-product validators (`web/src/schema/generated/`)
 * are a pure function of `docs/schemas/`: regenerating on an unchanged tree
 * is a no-op, and the generator refuses a keyword it does not implement. */

import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { GENERATED_DIRECTORY, SCHEMA_DIRECTORY, generate } from "../../web/tooling/schemagen";

const REPO_ROOT = resolve(__dirname, "../..");

describe("schemagen", () => {
  it("regenerates the committed modules exactly", () => {
    const modules = generate(join(REPO_ROOT, SCHEMA_DIRECTORY));
    const generated = join(REPO_ROOT, GENERATED_DIRECTORY);
    const committed = readdirSync(generated).filter((name) => name.endsWith(".ts")).sort();
    expect(committed).toEqual([...modules.keys()].sort());
    for (const [file, text] of modules) expect(readFileSync(join(generated, file), "utf8"), file).toBe(text);
  });

  it("refuses what it does not implement", () => {
    const write = (schema: object): string => {
      const directory = mkdtempSync(join(tmpdir(), "schemagen-"));
      writeFileSync(join(directory, "x.schema.json"), JSON.stringify(schema));
      return directory;
    };
    expect(() => generate(write({ type: "object", if: {} }))).toThrow(/if/);
    expect(() => generate(write({ type: "object", additionalProperties: false }))).toThrow(/unknown fields/);
    expect(() => generate(write({ $ref: "#/$defs/Missing" }))).toThrow(/no local definition/);
  });
});

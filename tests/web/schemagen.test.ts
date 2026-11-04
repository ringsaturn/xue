/** The generated validators (`web/src/schema/generated/`) are a pure
 * function of `docs/schemas/`: regenerating on an unchanged tree is a no-op,
 * the generator refuses a keyword it does not implement, and what it does
 * implement behaves as JSON Schema says: a closed block refuses an unknown
 * field, and a `$ref` into another schema calls that schema's check. */

import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { checkTime } from "../../web/src/schema/generated/bundle-metadata";
import { checkStoreGroup } from "../../web/src/schema/generated/store-group";
import { GENERATED_DIRECTORY, SCHEMA_DIRECTORY, generate } from "../../web/tooling/schemagen";

const REPO_ROOT = resolve(__dirname, "../..");

/** A directory holding `schemas`, by file stem. */
function write(schemas: Record<string, object>): string {
  const directory = mkdtempSync(join(tmpdir(), "schemagen-"));
  for (const [name, schema] of Object.entries(schemas)) writeFileSync(join(directory, `${name}.schema.json`), JSON.stringify(schema));
  return directory;
}

describe("schemagen", () => {
  it("regenerates the committed modules exactly", () => {
    const modules = generate(join(REPO_ROOT, SCHEMA_DIRECTORY));
    const generated = join(REPO_ROOT, GENERATED_DIRECTORY);
    const committed = readdirSync(generated).filter((name) => name.endsWith(".ts")).sort();
    expect(committed).toEqual([...modules.keys()].sort());
    for (const [file, text] of modules) expect(readFileSync(join(generated, file), "utf8"), file).toBe(text);
  });

  it("refuses what it does not implement", () => {
    expect(() => generate(write({ x: { type: "object", if: {} } }))).toThrow(/if/);
    expect(() => generate(write({ x: { type: "object", oneOf: [] } }))).toThrow(/oneOf/);
    expect(() => generate(write({ x: { type: "object", additionalProperties: false } }))).toThrow(/without naming its properties/);
    expect(() => generate(write({ x: { $ref: "#/$defs/Missing" } }))).toThrow(/names no definition/);
    expect(() => generate(write({ x: { $ref: "y.schema.json" } }))).toThrow(/names no definition/);
    expect(() => generate(write({ x: { $ref: "y.schema.json#/$defs/Missing" }, y: { type: "object" } }))).toThrow(/names no definition/);
    // An imported name must not shadow one of the module's own.
    expect(() =>
      generate(
        write({
          x: { type: "object", properties: { a: { $ref: "y.schema.json#/$defs/A" } }, $defs: { A: { type: "string" } } },
          y: { $defs: { A: { type: "integer" } } },
        }),
      ),
    ).toThrow(/defines too/);
  });

  it("imports the check of a definition in another schema", () => {
    const modules = generate(write({ x: { type: "object", properties: { a: { $ref: "y.schema.json#/$defs/A" } } }, y: { $defs: { A: { type: "integer" } } } }));
    expect(modules.get("x.ts")).toContain('import { checkA, type A } from "./y";');
    expect(modules.get("x.ts")).toContain("checkA(v1, at + \".a\");");
  });

  it("closes the blocks the format closes, and only those", () => {
    const time = { unitSeconds: 3600, firstFrameOffset: 0, frameCount: 3, frameStep: 1 };
    expect(() => checkTime(time, "time")).not.toThrow();
    expect(() => checkTime({ ...time, stepHours: 1 }, "time")).toThrow("time.stepHours is not a field of this block");
    expect(() => checkTime({ unitSeconds: 3600, firstFrameOffset: 0, frameCount: 3 }, "time")).toThrow(/time matches none of its allowed shapes \(time\.frameStep is missing; time\.frameOffsets is missing\)/);
  });

  it("holds an embedded document to the schema it names", () => {
    const group = JSON.parse(readFileSync(join(REPO_ROOT, "tests/fixtures/river/network/plain.zarr/zarr.json"), "utf8"));
    expect(() => checkStoreGroup(group, "group")).toThrow(/xue_profile is missing/);
    const shared = { zarr_format: 3, node_type: "group", attributes: { xue_profile: 2, xue_bundles: { tmp2m: { schemaVersion: 3 } } } };
    expect(() => checkStoreGroup(shared, "group")).toThrow("group.attributes.xue_bundles.tmp2m.model is missing");
  });
});

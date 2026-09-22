import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { identityForBundleId, registeredBundleId } from "../../web/src/identity";
import { FAMILIES, UNTILED_BUNDLE_IDS, familyMembers, familyOf } from "../../web/src/levels";
import { KNOWN_BUNDLE_IDS } from "../../web/src/manifest";
import { FIELD_GROUPS, specForIdentity, variableIds, variableSpec } from "../../web/src/variables";

// Read off disk rather than imported: the markup is what the rail is
// built from, and this holds it to the table.
const INDEX_HTML = readFileSync(resolve(process.cwd(), "web", "index.html"), "utf8");

/** The rail's written field tiles, by the bundle id each carries. */
function railTileIds(): string[] {
  const section = /<div class="rail-section" data-section="field"[\s\S]*?<\/div>\s*<div class="rail-section" data-section="overlay"/.exec(INDEX_HTML);
  expect(section).not.toBeNull();
  return [...section![0].matchAll(/data-variable="([a-z0-9]+)"/g)].map((match) => match[1]!);
}

describe("the variable table", () => {
  it("has one row per registered id, in a fixed order", () => {
    const ids = variableIds();
    expect(new Set(ids).size).toBe(ids.length);
    expect([...ids].sort()).toEqual([...KNOWN_BUNDLE_IDS].sort());
    for (const id of ids) expect(variableSpec(id)?.id).toBe(id);
    expect(variableSpec("vorticity")).toBeNull();
  });

  it("is the identity maps' source: id to identity and back", () => {
    for (const id of variableIds()) {
      const spec = variableSpec(id)!;
      const identity = identityForBundleId(id);
      expect(identity).toEqual({ family: spec.chart, level: spec.level, vector: spec.vector });
      expect(registeredBundleId(identity)).toBe(id);
      expect(specForIdentity(identity)?.id).toBe(id);
    }
  });

  it("names each field's rail family the way the family registry does", () => {
    for (const id of variableIds()) {
      const spec = variableSpec(id)!;
      expect(spec.family).toBe(familyOf(id));
      if (spec.family !== null) expect(familyMembers(spec.family)).toContain(id);
    }
    // The lines are no field; every field is in a listed group.
    for (const id of variableIds()) {
      const spec = variableSpec(id)!;
      if (spec.chart === "hgt") expect(spec.group).toBeNull();
      else expect(FIELD_GROUPS).toContain(spec.group);
    }
  });

  it("keeps every URL spelling unique", () => {
    const seen = new Map<string, string>();
    for (const id of variableIds()) {
      const spec = variableSpec(id)!;
      for (const alias of [spec.urlName, id, ...spec.urlAliases]) {
        expect(alias).toMatch(/^[a-z][a-z0-9]*$/);
        const owner = seen.get(alias);
        if (owner !== undefined && owner !== id) throw new Error(`${alias} names both ${owner} and ${id}`);
        seen.set(alias, id);
      }
    }
  });

  it("stands behind every tile the rail writes", () => {
    const tiles = railTileIds();
    expect(tiles.length).toBeGreaterThan(10);
    for (const id of tiles) {
      const spec = variableSpec(id);
      expect(spec, `tile ${id} has no row`).not.toBeNull();
      expect(spec!.group, `tile ${id} is no field`).not.toBeNull();
    }
    // And every field has a way onto the screen: its own tile, its family's
    // tile, or a deliberate absence.
    const tileFamilies = new Set(tiles.map((id) => familyOf(id)).filter((family) => family !== null));
    for (const id of variableIds()) {
      const spec = variableSpec(id)!;
      if (spec.group === null) continue;
      const reachable =
        tiles.includes(id) ||
        (spec.family !== null && tileFamilies.has(spec.family)) ||
        UNTILED_BUNDLE_IDS.includes(id) ||
        // Specific humidity is registered, shipped by no source, and has
        // no tile (levels.ts).
        spec.family === "spfh";
      expect(reachable, `${id} is reachable from no tile`).toBe(true);
    }
    expect(FAMILIES.spfh.glossKey).toBeNull();
  });

  it("tiles the new GFS fields under their own sheet groups", () => {
    // Each has a written rail tile, so the field sheet files it under its
    // group rather than the catch-all OTHER.
    for (const id of ["pwat", "ptype", "cin", "hpbl"] as const) {
      expect(railTileIds()).toContain(id);
      expect(variableSpec(id)!.family).toBeNull();
    }
    expect(variableSpec("pwat")!.group).toBe("moisture");
    expect(variableSpec("ptype")!.group).toBe("moisture");
    expect(variableSpec("cin")!.group).toBe("dynamics");
    expect(variableSpec("hpbl")!.group).toBe("dynamics");
    // The 100 m wind has no tile of its own: it is a member of the wind
    // family, behind the wind tile.
    expect(railTileIds()).not.toContain("wind100m");
    expect(variableSpec("wind100m")!.group).toBe("wind");
    expect(variableSpec("wind100m")!.family).toBe("wind");
    expect(familyMembers("wind")).toContain("wind100m");
  });

  it("carries the instrument copy the panels read", () => {
    const temperature = variableSpec("tmp2m")!;
    expect(temperature.code).toBe("TMP 2M");
    expect(temperature.legend()).toEqual(["50", "30", "10", "-10", "-30", "-60"]);
    expect(temperature.legendGradient).toBe("stylesheet");
    expect(temperature.meteogramCode).toBe("TMP");
    expect(temperature.showcaseCode).toBe("TEMP");
    expect(temperature.label()).toBe("2 m temperature");
    const upper = variableSpec("tmp850")!;
    expect(upper.code).toBe("TMP 850MB");
    expect(upper.title).toEqual(["850 hPa", "Temperature"]);
    expect(upper.label()).toBe("850 hPa temperature");
    expect(upper.showcaseCode).toBe("T 850MB");
    expect(upper.legend().length).toBe(6);
    const pressure = variableSpec("prmsl")!;
    expect(pressure.urlName).toBe("pressure");
    expect(pressure.meteogramCode).toBe("PRMSL");
    expect(pressure.ground).toBe("chart");
    expect(variableSpec("hgt500")!.urlAliases).toContain("subtropicalhigh");
    // The new GFS fields: the 100 m wind is a vector of its own family, and
    // the precipitation type's legend is a swatch key rather than a bar.
    const wind100 = variableSpec("wind100m")!;
    expect(wind100.vector).toBe(true);
    expect(wind100.family).toBe("wind");
    expect(wind100.code).toBe("WIND 100M");
    expect(wind100.label()).toBe("100 m wind");
    expect(wind100.legend()).toEqual(["40", "30", "20", "10", "5", "0"]);
    const ptype = variableSpec("ptype")!;
    expect(ptype.legend()).toEqual([]);
    expect(ptype.legendKey?.().map((swatch) => swatch.label)).toEqual([
      "Rain",
      "Freezing rain",
      "Snow",
      "Ice pellets",
    ]);
    expect(variableSpec("cin")!.label()).toBe("Convective inhibition");
    expect(variableSpec("pwat")!.label()).toBe("Precipitable water");
    expect(variableSpec("hpbl")!.label()).toBe("Planetary boundary layer height");
  });
});

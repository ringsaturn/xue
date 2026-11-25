/**
 * The section's samples: the codes of the cells the plan is sampled from, and
 * a frame of them as values at its points.
 *
 * Cells arrive in any order, an array of a block at a time, and are kept as
 * the codes they arrived as. A frame is blended out of them when it is drawn:
 * four lookups a value, which is cheaper than holding every frame's values at
 * twice the points as there are cells.
 *
 * The codebooks come as tables, a value per code and NaN for the codes that
 * are none, so that a worker needs nothing of what reads a variable.
 */

import type { ColumnQuantity } from "../identity";
import type { SectionArray, SectionFill, SectionStore } from "./types";

/** The levelled quantities a section reads. */
const LEVELLED: readonly ColumnQuantity[] = ["tmp", "rh", "hgt", "ugrd", "vgrd"];

/** An array as a section names it: by the quantity it is of, whatever its
 * name in the store. */
export type SectionArrayId = ColumnQuantity;

/** Every array a section has a use for, the ones its first picture needs
 * first. */
export const SECTION_ARRAYS: readonly SectionArrayId[] = [
  "hgt",
  "orog",
  "prmsl",
  "tmp",
  "tmp2m",
  "rh",
  "dpt2m",
  "ugrd",
  "vgrd",
  "ugrd10m",
  "vgrd10m",
];

/** The arrays to read of a store for a section, in `SECTION_ARRAYS` order: the
 * first of each quantity in the shape it is used in. */
export function sectionArrays(store: Pick<SectionStore, "arrays">): Map<SectionArrayId, SectionArray> {
  const found = new Map<SectionArrayId, SectionArray>();
  for (const id of SECTION_ARRAYS) {
    for (const array of store.arrays.values()) {
      if (array.quantity !== id || (array.levels !== null) !== LEVELLED.includes(id)) continue;
      found.set(id, array);
      break;
    }
  }
  return found;
}

/** What a fill or a line is drawn from, beside the ground. */
export type SectionGroup = "t" | "td" | "wind";

/** The arrays of each, levelled first. Only the levelled ones are necessary:
 * a store without the 2 m or the 10 m field has columns that begin at their
 * lowest level. */
const GROUPS: Record<SectionGroup, { levelled: readonly SectionArrayId[]; surface: readonly SectionArrayId[] }> = {
  t: { levelled: ["tmp"], surface: ["tmp2m"] },
  td: { levelled: ["tmp", "rh"], surface: ["tmp2m", "dpt2m"] },
  wind: { levelled: ["ugrd", "vgrd"], surface: ["ugrd10m", "vgrd10m"] },
};

/** Where a column's ground is read from. */
export const GROUND_ARRAYS: readonly SectionArrayId[] = ["hgt", "orog", "prmsl"];

const FILL_GROUP: Record<SectionFill, SectionGroup> = { thetae: "td", rh: "td", tmp: "t", wind: "wind", cross: "wind" };

export function fillGroup(fill: SectionFill): SectionGroup {
  return FILL_GROUP[fill];
}

export function groupArrays(group: SectionGroup): readonly SectionArrayId[] {
  return [...GROUPS[group].levelled, ...GROUPS[group].surface];
}

/** Whether a group can be drawn from what a store has. */
export function groupAvailable(group: SectionGroup, has: (id: SectionArrayId) => boolean): boolean {
  return GROUPS[group].levelled.every(has);
}

/** Whether a store has what a fill is drawn from. */
export function fillAvailable(fill: SectionFill, store: Pick<SectionStore, "arrays">): boolean {
  const ids = sectionArrays(store);
  return groupAvailable(FILL_GROUP[fill], (id) => ids.has(id));
}

/** One array as a field reads it. */
export interface FieldArray {
  id: SectionArrayId;
  /** hPa, descending; null on a surface field. */
  levels: number[] | null;
  /** Lead time of each frame, seconds. */
  offsets: number[];
  /** 256 values per member. */
  tables: Float32Array;
}

/** A section as a field reads it: what of the store and the plan can be
 * posted to a worker. */
export interface FieldSpec {
  arrays: FieldArray[];
  points: number;
  cells: number;
  corners: Uint32Array;
  weights: Float32Array;
  /** Radians clockwise from north, per point. */
  bearings: Float64Array;
  /** Degrees, per point. */
  latitudes: Float64Array;
}

/**
 * The frame an axis has for a lead time, or -1. The frame at it; between two
 * frames the earlier one, which is what the axis says until the next; past
 * either end none.
 */
export function frameAt(offsets: readonly number[], lead: number): number {
  if (offsets.length === 0 || lead < offsets[0]! || lead > offsets[offsets.length - 1]!) return -1;
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (offsets[middle]! <= lead) low = middle;
    else high = middle - 1;
  }
  return low;
}

interface Held {
  spec: FieldArray;
  frames: number;
  members: number;
  /** Cell by cell, each frame by frame, each level by level. */
  codes: Uint8Array;
  present: Uint8Array;
  arrived: number;
  /** Per point, whether every corner that weighs anything is here, and there
   * is one: a point off the grid never is. */
  ready: Uint8Array;
  readyAt: number;
}

export class SectionField {
  /** Moves with every arrival. */
  version = 0;
  private readonly held = new Map<string, Held>();

  constructor(readonly spec: FieldSpec) {
    for (const array of spec.arrays) {
      const members = array.levels?.length ?? 1;
      this.held.set(array.id, {
        spec: array,
        frames: array.offsets.length,
        members,
        codes: new Uint8Array(spec.cells * array.offsets.length * members),
        present: new Uint8Array(spec.cells),
        arrived: 0,
        ready: new Uint8Array(spec.points),
        readyAt: 0,
      });
    }
  }

  has(id: SectionArrayId): boolean {
    return this.held.has(id);
  }

  array(id: SectionArrayId): FieldArray | null {
    return this.held.get(id)?.spec ?? null;
  }

  /** Take the codes of some cells of an array, cells × frames × levels. An
   * array the section has no use for is ignored. */
  add(id: string, indices: Uint32Array, codes: Uint8Array): void {
    const held = this.held.get(id);
    if (!held) return;
    const size = held.frames * held.members;
    if (codes.length !== indices.length * size) throw new Error(`section: ${id} has ${codes.length} codes for ${indices.length} cells`);
    for (let at = 0; at < indices.length; at += 1) {
      const cell = indices[at]!;
      if (cell >= this.spec.cells) throw new Error(`section: ${id} has no cell ${cell}`);
      held.codes.set(codes.subarray(at * size, (at + 1) * size), cell * size);
      if (!held.present[cell]) {
        held.present[cell] = 1;
        held.arrived += 1;
      }
    }
    this.version += 1;
  }

  /** The share of the cells of every array that have arrived. */
  get complete(): number {
    let arrived = 0;
    for (const held of this.held.values()) arrived += held.arrived;
    const all = this.held.size * this.spec.cells;
    return all === 0 ? 0 : arrived / all;
  }

  /** Per point, whether it can be blended from what has arrived of an array. */
  ready(id: SectionArrayId): Uint8Array | null {
    const held = this.held.get(id);
    if (!held) return null;
    if (held.readyAt !== this.version) {
      const { corners, weights, points } = this.spec;
      for (let point = 0; point < points; point += 1) {
        let here = 1;
        let weight = 0;
        for (let corner = point * 4; corner < point * 4 + 4; corner += 1) {
          weight += weights[corner]!;
          if (weights[corner]! > 0 && !held.present[corners[corner]!]) here = 0;
        }
        if (weight === 0) here = 0;
        held.ready[point] = here;
      }
      held.readyAt = this.version;
    }
    return held.ready;
  }

  /** The frame of an array at a lead time, or -1. A field with a single frame
   * (the terrain) holds at every one. */
  frame(id: SectionArrayId, lead: number): number {
    const held = this.held.get(id);
    if (!held) return -1;
    return held.frames === 1 && held.spec.levels === null ? 0 : frameAt(held.spec.offsets, lead);
  }

  /** The lead time, seconds, of the frame a section shows at the playhead, or
   * null where the store has none. */
  leadOf(lead: number): number | null {
    for (const id of LEVELLED) {
      const held = this.held.get(id);
      if (!held) continue;
      const frame = frameAt(held.spec.offsets, lead);
      return frame < 0 ? null : held.spec.offsets[frame]!;
    }
    return null;
  }

  /** The lead time, seconds, of the frame after the one at a frame's, or null
   * past the last. */
  leadAfter(stored: number): number | null {
    for (const id of LEVELLED) {
      const held = this.held.get(id);
      if (held) return held.spec.offsets[frameAt(held.spec.offsets, stored) + 1] ?? null;
    }
    return null;
  }

  /** One frame of an array at the points from `first` up to `end`, points ×
   * levels. A code that is no reading leaves the point to its other corners; a
   * point of none is NaN, as one off the grid is, and so is every point where
   * there is no such frame. */
  blend(id: SectionArrayId, frame: number, out: Float32Array, first = 0, end = this.spec.points): void {
    const held = this.held.get(id);
    const { corners, weights } = this.spec;
    if (!held || frame < 0) {
      out.fill(Number.NaN);
      return;
    }
    const { codes, members, frames } = held;
    const { tables } = held.spec;
    const size = frames * members;
    for (let point = first; point < end; point += 1) {
      const c = point * 4;
      const a0 = (corners[c]! * frames + frame) * members;
      const a1 = (corners[c + 1]! * frames + frame) * members;
      const a2 = (corners[c + 2]! * frames + frame) * members;
      const a3 = (corners[c + 3]! * frames + frame) * members;
      const w0 = weights[c]!;
      const w1 = weights[c + 1]!;
      const w2 = weights[c + 2]!;
      const w3 = weights[c + 3]!;
      if (w0 + w1 + w2 + w3 === 0) {
        out.fill(Number.NaN, (point - first) * members, (point - first + 1) * members);
        continue;
      }
      for (let level = 0; level < members; level += 1) {
        const table = level * 256;
        let value =
          w0 * tables[table + codes[a0 + level]!]! +
          w1 * tables[table + codes[a1 + level]!]! +
          w2 * tables[table + codes[a2 + level]!]! +
          w3 * tables[table + codes[a3 + level]!]!;
        if (value !== value) {
          let sum = 0;
          let weight = 0;
          for (let corner = c; corner < c + 4; corner += 1) {
            const part = tables[table + codes[corners[corner]! * size + frame * members + level]!]!;
            if (part !== part) continue;
            sum += weights[corner]! * part;
            weight += weights[corner]!;
          }
          value = weight > 0 ? sum / weight : Number.NaN;
        }
        out[(point - first) * members + level] = value;
      }
    }
  }
}

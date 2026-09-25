/** One source's live summary, including its full variable list.
 *
 * The cheap place to learn a source's variables is the **live STAC Item**
 * (`<source>/item.json`): its `cube:variables` is one entry per array the run
 * publishes (a vector bundle's components carry `xue:bundle`), so one read
 * gives the whole vocabulary `/v1/point?variables=` accepts. The Collection
 * document does not list variables; the manifest only names bundle ids. */

import { fetchJson } from "./bucket";
import { resolveRun } from "./point";

interface CubeVariable {
  description?: string;
  title?: string;
  unit?: string;
  "xue:bundle"?: string;
}

function bundlesFromItem(item: Record<string, any>): Record<string, unknown> {
  const variables = (item.properties?.["cube:variables"] ?? {}) as Record<string, CubeVariable>;
  const groups = new Map<string, Array<{ id: string; label: string | null; unit: string | null }>>();
  for (const [arrayId, variable] of Object.entries(variables)) {
    const bundleId = variable["xue:bundle"] ?? arrayId;
    const arrays = groups.get(bundleId) ?? [];
    arrays.push({
      id: arrayId,
      label: variable.description ?? variable.title ?? null,
      unit: variable.unit ?? null,
    });
    groups.set(bundleId, arrays);
  }
  const bundles: Record<string, unknown> = {};
  for (const [bundleId, arrays] of groups) {
    const only = arrays[0]!;
    bundles[bundleId] =
      arrays.length === 1 && only.id === bundleId
        ? { label: only.label, unit: only.unit }
        : { components: Object.fromEntries(arrays.map((array) => [array.id, { label: array.label, unit: array.unit }])) };
  }
  return bundles;
}

export async function readSource(source: string): Promise<Record<string, unknown>> {
  const collection = await fetchJson<Record<string, any>>(`${source}/collection.json`);
  const summary: Record<string, unknown> = {
    source,
    title: collection.title ?? null,
    description: collection.description ?? null,
    license: collection.license ?? null,
    run: collection["xue:live"] ?? null,
    bundles: null,
  };

  try {
    const item = await fetchJson<Record<string, any>>(`${source}/item.json`);
    summary.run = item.id ?? summary.run;
    summary.bbox = item.bbox ?? null;
    summary.runTime = item.properties?.["forecast:reference_datetime"] ?? item.properties?.datetime ?? null;
    summary.time = item.properties?.["cube:dimensions"]?.time ?? null;
    summary.bundles = bundlesFromItem(item);
  } catch {
    // No live STAC Item (an older bucket or a local build): fall back to the
    // manifest's bundle ids, which the point path already reads.
    try {
      const { manifest } = await resolveRun(source);
      summary.bundles = Object.fromEntries(manifest.bundles.map((bundle) => [bundle.variable, { label: null, unit: null }]));
    } catch {
      // Leave bundles null.
    }
  }
  return summary;
}

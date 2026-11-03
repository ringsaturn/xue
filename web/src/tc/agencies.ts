import { REGISTRY } from "../registry";

/** Who says what about a storm: the encoder's registry
 * (`xuebuild/tc/registry.py`) as the shell's registry carries it, so a
 * color or an id cannot drift between the two; the codes are the shell's.
 *
 * The product admits any `^[a-z][a-z0-9]*$` key: a centre or a model this
 * table does not know is still drawn, in the neutral ink, under its own
 * id. The names here are the instrument codes (`NHC`, `JTWC`), English in
 * every locale like the rest of the panel's codes. */

interface AgencyInfo {
  id: string;
  /** The code the map and the panel print. */
  code: string;
  /** English name, for a tooltip. */
  name: string;
  /** Fixed line color — Okabe–Ito, never the theme's. */
  color: string;
  /** ATCF basins it forecasts in. */
  basins: readonly string[];
  /** Whose intensity classes its `class` strings are. */
  scale: string;
}

interface ModelInfo {
  id: string;
  code: string;
  name: string;
  ensemble: boolean;
}

export const TC_AGENCIES: Record<string, AgencyInfo> = Object.fromEntries(
  REGISTRY.tc.agencies.map((agency) => [
    agency.id,
    { id: agency.id, code: agency.id.toUpperCase(), name: agency.name, color: agency.color, basins: agency.basins, scale: agency.scale },
  ]),
);

/** The instrument code of each model, where it is not the id upper-cased. */
const MODEL_CODES: Record<string, string> = { ecmwfens: "ENS" };

export const TC_MODELS: Record<string, ModelInfo> = Object.fromEntries(
  REGISTRY.tc.models.map((model) => [
    model.id,
    { id: model.id, code: MODEL_CODES[model.id] ?? model.id.toUpperCase(), name: model.name, ensemble: model.ensemble },
  ]),
);

/** The IBTrACS best-track keys and the centre each stands for; `usa` is
 * the NHC / JTWC joint best track and takes the neutral ink. */
export const TC_BEST_AGENCY: Record<string, string | null> = {
  usa: null,
  jma: "jma",
  cma: "cma",
  hko: "hko",
  kma: "kma",
  imd: "imd",
  mfr: "mfr",
  bom: "bom",
  fms: "fms",
  mnz: "mnz",
};

/** The neutral ink for anything without a centre's color: model tracks,
 * the US best track, an unknown key. Set per ground by the caller. */
export const NEUTRAL_LIGHT_GROUND = "#333333";
export const NEUTRAL_DARK_GROUND = "#d8d8d8";

export function agencyColor(id: string): string | null {
  return TC_AGENCIES[id]?.color ?? null;
}

export function agencyCode(id: string): string {
  return TC_AGENCIES[id]?.code ?? TC_MODELS[id]?.code ?? id.toUpperCase();
}

export const TC_BASINS: Record<string, string> = REGISTRY.tc.basins;

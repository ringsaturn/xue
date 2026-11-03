// The basemap's style: the Protomaps flavor per theme, the grounds under
// each field, the relief's hillshade and the coastline, and the property
// diff that moves the map onto a fresh build without `map.setStyle`.
import { layers as basemapLayers, namedFlavor } from "@protomaps/basemaps";
import type { Map as MaplibreMap, MapOptions } from "maplibre-gl";
import { basemapLang } from "./i18n";
import { TERRAIN_ATTRIBUTION, TERRAIN_MESH_SOURCE, TERRAIN_SOURCE } from "./terrain";
import { TERRAIN_DETAIL_MAX_ZOOM, TERRAIN_DETAIL_TILES } from "./terrainprotocol";
import { isDark } from "./theme";
import { type GroundId, variableSpec } from "./variables";

interface BasemapTones {
  ocean: string;
  land: string;
  /** Painted behind everything — visible past the coastlines' antialiasing
   * and across the whole canvas until the first tiles arrive. Defaults to
   * `ocean`, which is what a themed slate wants; a layer that leaves the
   * flavor alone names the flavor's own background instead. */
  background?: string;
}

/** Protomaps' data-viz flavor for the light theme: white land, pale grey
 * water, and no landcover tinting the continents — a base that stays out of
 * the data's way. The dark theme keeps the "dark" flavor. */
const LIGHT_FLAVOR = "white";

/** The white flavor's own ground, read from the flavor rather than copied
 * into hexes here: a light-theme layer that takes these tones leaves the
 * basemap exactly as Protomaps drew it. */
const PAPER_GROUND: BasemapTones = {
  ocean: namedFlavor(LIGHT_FLAVOR).water,
  land: namedFlavor(LIGHT_FLAVOR).earth,
  background: namedFlavor(LIGHT_FLAVOR).background,
};

/** The grounds, per theme (`VariableSpec.ground` says which a field takes).
 *
 * On the dark theme each is a slate the field's palette needs: an opaque
 * coat (the temperatures, θe) paints over its ground, so that one is nearly
 * a void; the translucent washes (precipitation, humidity, cloud, CAPE,
 * visibility, the ice and wave fields) composite over theirs, which has to
 * carry the geography and keep the low end of the ramp readable, so it is
 * lighter; the wind's is between; the radiation's and the radar's are their
 * own — radar echoes are small and bright, and the ground stays dark enough
 * for a 5 dBZ edge to read against it. The contour lines are drawn thin
 * over a nearly bare map, which is what a chart looks like: the base
 * carries the geography on its own, so it is the plainest of the set.
 *
 * Light means a light map: every field sits on the same white ground, the
 * translucent ones included, so the page is one sheet rather than paper
 * chrome floating over a dark map. Drizzle, the faintest wind and a 5 dBZ
 * edge read weaker on white than on the dark slate — the accepted cost of
 * one palette serving both themes (see PRECIPITATION_STOPS in palettes.ts).
 * The lines keep their chart stock: contours are a weather chart, and the
 * warm sheet is the design's own paper for one. */
const GROUND_TONES: Record<GroundId, { dark: BasemapTones; light: BasemapTones }> = {
  coat: { dark: { ocean: "#0b1826", land: "#182c3d" }, light: PAPER_GROUND },
  slate: { dark: { ocean: "#16344a", land: "#28495f" }, light: PAPER_GROUND },
  wind: { dark: { ocean: "#0e2131", land: "#1d3849" }, light: PAPER_GROUND },
  solar: { dark: { ocean: "#0d1b2b", land: "#1c3242" }, light: PAPER_GROUND },
  radar: { dark: { ocean: "#0c1a26", land: "#1a2f3d" }, light: PAPER_GROUND },
  chart: { dark: { ocean: "#101f2c", land: "#22384a" }, light: { ocean: "#dcd6c8", land: "#c9c2b2" } },
};

/** The ground under the field on screen. Read at every use rather than
 * once: the theme toggles in place. An unregistered field takes the coat's. */
export function currentBasemapTheme(): BasemapTones {
  const id = document.body.dataset.variable;
  const ground = (id === undefined ? null : variableSpec(id)?.ground) ?? "coat";
  return isDark ? GROUND_TONES[ground].dark : GROUND_TONES[ground].light;
}

/** Relative luminance of a `#rrggbb` tone. */
export function luminance(color: string): number {
  const value = Number.parseInt(color.slice(1), 16);
  return (0.2126 * ((value >> 16) & 255) + 0.7152 * ((value >> 8) & 255) + 0.0722 * (value & 255)) / 255;
}

/** The coastline drawn as a line of its own. Protomaps has no coastline
 * layer: the coast is only where the `earth` fill meets the `water` fill,
 * and under a translucent field that edge all but disappears (Windy draws
 * its coast as a dark line over the data, which is what the eye expects).
 * A line layer over the `earth` polygons traces their outline — the ocean
 * coast, since lakes are `water` features inside the land — and the tile
 * clip edges fall in the tile buffer, where the renderer never paints. Its
 * width is a style property; its color follows the ground in
 * `applyBasemapInk`, like the boundaries'. */
export const COASTLINE_LAYER = "coastline";

/** Coastline width in CSS px by zoom: a hairline would vanish under the
 * data at the global view, and past the regional scale the coast stays a
 * line rather than growing into an edge. */
const COASTLINE_WIDTH: NonNullable<NonNullable<LineLayer["paint"]>["line-width"]> = [
  "interpolate",
  ["linear"],
  ["zoom"],
  0,
  0.9,
  4,
  1.3,
  8,
  1.8,
];

/** Protomaps hosted basemap (real coastlines, waterways, boundaries and
 * labels). The forecast layers insert themselves before this layer — the
 * coastline, which sits just under the boundaries — keeping the coast, the
 * boundaries and the place labels legible above the data. */
export const FORECAST_ANCHOR_LAYER = COASTLINE_LAYER;

// A Protomaps key is bound to the origins it serves, so a page that builds
// this shell on its own host brings its own (`VITE_PROTOMAPS_KEY`).
const PROTOMAPS_KEY = import.meta.env.VITE_PROTOMAPS_KEY || "249bb192fefe0a77";

// A self-hosted shell can point the basemap at its own mirror: the vector
// tile template (`VITE_BASEMAP_TILES`) and the root holding Protomaps'
// `fonts/` and `sprites/` (`VITE_BASEMAP_ASSETS`), both absolute.
const BASEMAP_TILES =
  import.meta.env.VITE_BASEMAP_TILES || `https://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt?key=${PROTOMAPS_KEY}`;

const BASEMAP_ASSETS = import.meta.env.VITE_BASEMAP_ASSETS || "https://protomaps.github.io/basemaps-assets";

// maplibre-gl 6 does not re-export the style-spec types; take the style
// object's type from the map options that consume it.
export type BasemapStyle = Exclude<MapOptions["style"], string | undefined>;
type LineLayer = Extract<BasemapStyle["layers"][number], { type: "line" }>;
type HillshadeLayer = Extract<BasemapStyle["layers"][number], { type: "hillshade" }>;
type HillshadePaint = NonNullable<HillshadeLayer["paint"]>;

/** The relief, drawn as a hillshade under the fields. The source and the
 * readout's own sampler live in `terrain.ts`, the tiles' coverage past z12 in
 * `terrainprotocol.ts`; this is the style's half of the set.
 *
 * `maxzoom` is the deepest regional archive's, not left to the TileJSON,
 * which names none. The protocol answers 404 locally wherever no archive
 * reaches a tile's zoom — most of the world, China included — and MapLibre
 * then overzooms the z12 tile, which costs sharpness and no requests. */
export const TERRAIN_LAYER = "hillshade";

/** The relief's ink, per ground. On paper the shadow is the chart's own warm
 * brown and the highlight is paper white; on the dark grounds both steps hug
 * the land tone, so the relief reads as texture rather than as a light
 * source. The alpha lives in the colors — MapLibre mixes the pair by aspect
 * and the shade's alpha scales the result — which keeps the field and the
 * place labels legible where they cross a slope. */
const HILLSHADE_INK: Record<"light" | "dark", HillshadePaint> = {
  light: {
    "hillshade-shadow-color": "rgba(107, 92, 66, 0.5)",
    "hillshade-highlight-color": "rgba(255, 255, 255, 0.45)",
    "hillshade-accent-color": "rgba(138, 122, 92, 0.3)",
    "hillshade-exaggeration": 0.35,
  },
  dark: {
    "hillshade-shadow-color": "rgba(0, 0, 0, 0.55)",
    "hillshade-highlight-color": "rgba(143, 168, 191, 0.38)",
    "hillshade-accent-color": "rgba(0, 0, 0, 0.3)",
    "hillshade-exaggeration": 0.25,
  },
};

export function hillshadePaint(darkGround: boolean): HillshadePaint {
  return HILLSHADE_INK[darkGround ? "dark" : "light"];
}

export function buildBasemapStyle(): BasemapStyle {
  const theme = currentBasemapTheme();
  const darkGround = luminance(theme.ocean) < 0.5;
  const flavorName = isDark ? "dark" : LIGHT_FLAVOR;
  const flavor = {
    ...namedFlavor(flavorName),
    background: theme.background ?? theme.ocean,
    water: theme.ocean,
    earth: theme.land,
  };
  return {
    version: 8,
    glyphs: `${BASEMAP_ASSETS}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${BASEMAP_ASSETS}/sprites/v4/${flavorName}`,
    sources: {
      // Inline tile URLs (no TileJSON fetch) so the shell still boots — dark
      // ocean, no basemap — when api.protomaps.com is unreachable.
      protomaps: {
        type: "vector",
        tiles: [BASEMAP_TILES],
        maxzoom: 15,
        attribution: "Protomaps © OpenStreetMap contributors",
      },
      // The relief is inline too, and for the same reason: a TileJSON round
      // trip in front of the first paint buys nothing the constants above
      // do not already say.
      [TERRAIN_SOURCE]: {
        type: "raster-dem",
        tiles: TERRAIN_DETAIL_TILES,
        encoding: "terrarium",
        maxzoom: TERRAIN_DETAIL_MAX_ZOOM,
        attribution: TERRAIN_ATTRIBUTION,
      },
      [TERRAIN_MESH_SOURCE]: {
        type: "raster-dem",
        tiles: TERRAIN_DETAIL_TILES,
        encoding: "terrarium",
        maxzoom: TERRAIN_DETAIL_MAX_ZOOM,
      },
    },
    // The flavor's landcover layer repaints the whole landmass in its own
    // near-black tones, defeating the per-variable earth color — drop it so
    // land stays a flat themed slate under the data.
    // Basemap labels follow the UI locale.
    layers: withCoastline(
      withHillshade(
        quietedUnderData(
          basemapLayers("protomaps", flavor, { lang: basemapLang }).filter((layer) => layer.id !== "landcover"),
        ),
        hillshadePaint(darkGround),
      ),
    ),
  };
}

/** The relief, inserted just above the land fill. Everything the flavor puts
 * over the ground — landuse, waterways, roads and their casings, boundaries,
 * labels — then draws on top of it, and the forecast layers insert above all
 * of those (before the coastline). Putting it lower would bury it under the
 * opaque `earth` fill; putting it higher would run it over the roads. */
function withHillshade(
  layers: BasemapStyle["layers"],
  paint: HillshadePaint,
): BasemapStyle["layers"] {
  const hillshade: BasemapStyle["layers"][number] = {
    id: TERRAIN_LAYER,
    type: "hillshade",
    source: TERRAIN_SOURCE,
    paint,
  };
  const at = layers.findIndex((layer) => layer.id === "earth");
  if (at < 0) throw new Error("basemap style has no earth layer");
  return [...layers.slice(0, at + 1), hillshade, ...layers.slice(at + 1)];
}

/** The flavor's street-level detail, toned down so a field stays the
 * picture at a city zoom. The finest grids (the 0.005° JMA nowcast) earn
 * zoom 12, where a flavor draws every street, the building footprints and
 * the landuse fills; under a translucent field the street grid reads as
 * bright patches over the data and the bridges and major roads as white
 * bars across it. Buildings are dropped outright — a weather map has no
 * use for footprints — and the roads keep a fraction of their opacity,
 * the major ones more so the eye still has the arterials to hold on to.
 * Water, coast, boundaries and labels are untouched. */
function quietedUnderData(layers: BasemapStyle["layers"]): BasemapStyle["layers"] {
  return layers.flatMap((layer) => {
    if (layer.id === "buildings") return [];
    if (layer.type === "line" && layer.id.startsWith("roads_")) {
      const arterial = /highway|major/.test(layer.id);
      return [{ ...layer, paint: { ...layer.paint, "line-opacity": arterial ? 0.55 : 0.3 } }];
    }
    if (layer.type === "fill" && layer.id.startsWith("landuse_")) {
      return [{ ...layer, paint: { ...layer.paint, "fill-opacity": 0.5 } }];
    }
    return [layer];
  });
}

/** The flavor's layers with the coastline inserted just under the country
 * boundaries, so the two read as one set of lines over the data. */
function withCoastline(layers: BasemapStyle["layers"]): BasemapStyle["layers"] {
  const coastline: BasemapStyle["layers"][number] = {
    id: COASTLINE_LAYER,
    type: "line",
    source: "protomaps",
    "source-layer": "earth",
    filter: ["==", "$type", "Polygon"],
    layout: { "line-join": "round", "line-cap": "round" },
    // The color is the ground's ink, set by `applyBasemapInk`; this is only
    // what the layer carries until the style has loaded.
    paint: { "line-color": "rgba(27, 26, 23, 0.6)", "line-width": COASTLINE_WIDTH },
  };
  const at = layers.findIndex((layer) => layer.id === "boundaries_country");
  if (at < 0) throw new Error("basemap style has no boundaries_country layer");
  return [...layers.slice(0, at), coastline, ...layers.slice(at)];
}

type StyleProperties = Record<string, unknown> | undefined;
export type PaintName = Parameters<MaplibreMap["setPaintProperty"]>[1];
export type PaintValue = Parameters<MaplibreMap["setPaintProperty"]>[2];
type LayoutName = Parameters<MaplibreMap["setLayoutProperty"]>[1];
type LayoutValue = Parameters<MaplibreMap["setLayoutProperty"]>[2];

/** Apply every property of `next` that differs from `before`, and reset the
 * ones `next` no longer has. */
function diffStyleProperties(
  before: StyleProperties,
  next: StyleProperties,
  set: (name: string, value: unknown) => void,
): void {
  for (const name of new Set([...Object.keys(before ?? {}), ...Object.keys(next ?? {})])) {
    const previous = before?.[name];
    const value = next?.[name];
    if (JSON.stringify(previous) !== JSON.stringify(value)) set(name, value);
  }
}

/** Apply the difference between the style the map carries and a fresh
 * build, property by property, plus the flavor's sprite. */
export function applyBasemapStyleDiff(target: MaplibreMap, applied: BasemapStyle, next: BasemapStyle): void {
  if (next.sprite !== applied.sprite && typeof next.sprite === "string") target.setSprite(next.sprite);
  const previous = new Map(applied.layers.map((layer) => [layer.id, layer]));
  for (const layer of next.layers) {
    const before = previous.get(layer.id);
    if (!before || !target.getLayer(layer.id)) continue;
    const id = layer.id;
    const paintBefore = (before as { paint?: StyleProperties }).paint;
    const paintNext = (layer as { paint?: StyleProperties }).paint;
    const layoutBefore = (before as { layout?: StyleProperties }).layout;
    const layoutNext = (layer as { layout?: StyleProperties }).layout;
    // The names come from the style itself; the typed overloads only know
    // the property names written out in the spec.
    diffStyleProperties(paintBefore, paintNext, (name, value) =>
      target.setPaintProperty(id, name as PaintName, value as PaintValue),
    );
    diffStyleProperties(layoutBefore, layoutNext, (name, value) =>
      target.setLayoutProperty(id, name as LayoutName, value as LayoutValue),
    );
    const filterBefore = (before as { filter?: unknown }).filter;
    const filterNext = (layer as { filter?: unknown }).filter;
    if (JSON.stringify(filterBefore) !== JSON.stringify(filterNext)) {
      target.setFilter(id, filterNext as Parameters<MaplibreMap["setFilter"]>[1]);
    }
  }
}

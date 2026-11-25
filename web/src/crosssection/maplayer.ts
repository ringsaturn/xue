/**
 * The line of a cross-section on the map: the great circle, its two ends
 * lettered as the chart's are, and the point read on the chart. One GeoJSON
 * source, its layers on top of the fields and everything drawn over them, as
 * the ruler's are, and inked from the ground the same way.
 */

import type { Feature, FeatureCollection, Geometry } from "geojson";
import type {
  CircleLayerSpecification,
  LineLayerSpecification,
  Map as MaplibreMap,
  SymbolLayerSpecification,
} from "maplibre-gl";

import { contextInk } from "../casecontext";
import { greatCircle } from "../measure";
import type { LonLat, SectionLine } from "./types";

interface LineProperties {
  role: "line" | "end" | "hover";
  letter?: string;
  /** Whether there is a section under the line: it is dashed where there is
   * none. */
  live?: boolean;
}

/** What the layers draw. */
export function sectionFeatures(
  line: SectionLine | null,
  live: boolean,
  hover: LonLat | null,
): FeatureCollection<Geometry, LineProperties> {
  const features: Feature<Geometry, LineProperties>[] = [];
  if (line) {
    const path = greatCircle(line.a, line.b);
    features.push({ type: "Feature", geometry: { type: "LineString", coordinates: path }, properties: { role: "line", live } });
    for (const [letter, at] of [["A", path[0]!], ["B", path[path.length - 1]!]] as const) {
      features.push({ type: "Feature", geometry: { type: "Point", coordinates: at }, properties: { role: "end", letter } });
    }
    if (hover) features.push({ type: "Feature", geometry: { type: "Point", coordinates: hover }, properties: { role: "hover" } });
  }
  return { type: "FeatureCollection", features };
}

const SOURCE = "cross-section";

/** The layers, bottom to top. */
function layerSpecs(darkGround: boolean): (LineLayerSpecification | CircleLayerSpecification | SymbolLayerSpecification)[] {
  const ink = contextInk(darkGround);
  const line = (id: string, live: boolean, dash: number[] | null): LineLayerSpecification => ({
    id,
    type: "line",
    source: SOURCE,
    filter: ["all", ["==", ["get", "role"], "line"], ["==", ["get", "live"], live]],
    layout: { "line-join": "round", "line-cap": "butt" },
    paint: { "line-color": ink.measure, "line-width": 2, ...(dash && { "line-dasharray": dash }) },
  });
  return [
    {
      id: "cross-section-casing",
      type: "line",
      source: SOURCE,
      filter: ["==", ["get", "role"], "line"],
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": ink.halo, "line-width": 4.5 },
    },
    line("cross-section-line", true, null),
    line("cross-section-line-empty", false, [3, 3]),
    {
      id: "cross-section-end",
      type: "circle",
      source: SOURCE,
      filter: ["==", ["get", "role"], "end"],
      paint: {
        "circle-radius": 9,
        "circle-color": ink.halo,
        "circle-stroke-color": ink.measure,
        "circle-stroke-width": 1.6,
      },
    },
    {
      id: "cross-section-hover",
      type: "circle",
      source: SOURCE,
      filter: ["==", ["get", "role"], "hover"],
      paint: {
        "circle-radius": 5,
        "circle-color": ink.measure,
        "circle-stroke-color": ink.halo,
        "circle-stroke-width": 2,
      },
    },
    {
      id: "cross-section-letter",
      type: "symbol",
      source: SOURCE,
      filter: ["==", ["get", "role"], "end"],
      layout: {
        "text-field": ["get", "letter"],
        "text-font": ["Noto Sans Medium"],
        "text-size": 12,
        "text-allow-overlap": true,
        "text-ignore-placement": true,
      },
      paint: { "text-color": ink.measure },
    },
  ];
}

export class SectionMapLayer {
  private line: SectionLine | null = null;
  private live = false;
  private hover: LonLat | null = null;
  private darkGround = false;

  constructor(private readonly map: MaplibreMap) {}

  /** Draw a line, solid where there is a section under it, or take it off. */
  setLine(line: SectionLine | null, live: boolean): void {
    this.line = line;
    this.live = live;
    this.hover = null;
    this.render();
  }

  /** Mark the point the chart is read at. */
  setHover(hover: LonLat | null): void {
    if (this.hover === hover) return;
    this.hover = hover;
    this.render();
  }

  /** Repaint the colours for the ground. */
  setInk(darkGround: boolean): void {
    this.darkGround = darkGround;
    if (!this.map.getSource(SOURCE)) return;
    for (const layer of layerSpecs(darkGround)) {
      for (const [name, value] of Object.entries(layer.paint ?? {})) {
        if (!name.endsWith("-color") || !this.map.getLayer(layer.id)) continue;
        this.map.setPaintProperty(layer.id, name as never, value as never);
      }
    }
  }

  private render(): void {
    const source = this.map.getSource(SOURCE) as { setData?: (data: unknown) => void } | undefined;
    if (!this.line) {
      if (!source) return;
      for (const { id } of layerSpecs(this.darkGround)) if (this.map.getLayer(id)) this.map.removeLayer(id);
      this.map.removeSource(SOURCE);
      return;
    }
    const data = sectionFeatures(this.line, this.live, this.hover);
    if (source?.setData) {
      source.setData(data);
      return;
    }
    this.map.addSource(SOURCE, { type: "geojson", data });
    for (const layer of layerSpecs(this.darkGround)) this.map.addLayer(layer);
  }
}

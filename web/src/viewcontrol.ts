import type { IControl, Map as MaplibreMap } from "maplibre-gl";

import { onLocaleChange, t, type MessageKey } from "./i18n";

/**
 * The view tile under the zoom tile: the globe, the 3D relief and the
 * terrain's cast shadows as switches, then a way back to north and a way
 * back to a flat camera. The last two are there only while the camera has
 * turned or tilted, so the column is as short as the view lets it be; the
 * north button's needle turns with the map so it says which way north is
 * before it is pressed.
 *
 * One MapLibre control group for all four, styled like the zoom tile: the
 * library's own globe and terrain controls are a group each, and the column
 * then reads as a stack of unrelated tiles.
 */

/** Below these the camera counts as facing north and as flat. */
const BEARING_EPSILON = 0.5;
const PITCH_EPSILON = 0.5;

export interface ViewControlOptions {
  /** The raster-dem source the relief is built from. */
  terrainSource: string;
  /** The exaggeration the relief is switched on at, read when it is. */
  exaggeration: () => number;
  /** The column's height changed (a reset button came or went). */
  onResize?: () => void;
  /** One of the radar volume's tools was pressed (`main.ts` arms it, or
   * clears what it selected). */
  onVolumeTool?: (kind: "box" | "section") => void;
}

/** The terrain shadows' switch, handed over once `main.ts` has built them
 * (the button stays hidden until then). */
export interface ShadowSwitch {
  enabled: () => boolean;
  toggle: () => void;
}

/** What the volume tools show: whether they are offered at all (only over
 * a volume), and whether each is armed or holds a selection. */
export interface VolumeToolState {
  available: boolean;
  box: boolean;
  section: boolean;
}

const BOX_ICON = `<svg viewBox="0 0 29 29" width="29" height="29" aria-hidden="true">
  <rect x="7.5" y="8.5" width="14" height="12" fill="none" stroke="#333" stroke-width="1.6" stroke-dasharray="2.6 2"/>
  <path d="M12 17.5v-5M14.5 17.5v-7M17 17.5v-4" fill="none" stroke="#333" stroke-width="1.4" stroke-linecap="round"/>
</svg>`;

const SECTION_ICON = `<svg viewBox="0 0 29 29" width="29" height="29" aria-hidden="true">
  <path d="M7 20.5 22 9.5" fill="none" stroke="#333" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M10.5 18v-6.5M14.5 15v-7M18.5 12V6.5" fill="none" stroke="#333" stroke-width="1.3" stroke-linecap="round"/>
</svg>`;

const SHADOW_ICON = `<svg viewBox="0 0 29 29" width="29" height="29" aria-hidden="true">
  <circle cx="9" cy="9" r="2.6" fill="none" stroke="#333" stroke-width="1.5"/>
  <path d="M9 3.8v1.2M9 13v1.2M3.8 9h1.2M13 9h1.2M5.3 5.3l.9.9M11.8 11.8l.9.9M12.7 5.3l-.9.9M6.2 11.8l-.9.9" fill="none" stroke="#333" stroke-width="1.2" stroke-linecap="round"/>
  <path d="M18 13.5 22.5 21.5H26z" fill="#333" fill-opacity="0.55"/>
  <path d="M13.5 21.5 18 13.5l4.5 8z" fill="none" stroke="#333" stroke-width="1.5" stroke-linejoin="round"/>
</svg>`;

const PITCH_ICON = `<svg viewBox="0 0 29 29" width="29" height="29" aria-hidden="true">
  <path d="M7 19.5h15" fill="none" stroke="#333" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M8.5 15.5 20.5 9.5" fill="none" stroke="#333" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="2 2.2"/>
  <path d="M12 19.5a5.5 5.5 0 0 0-.9-3.1" fill="none" stroke="#333" stroke-width="1.3" stroke-linecap="round"/>
</svg>`;

export class ViewControl implements IControl {
  private map: MaplibreMap | null = null;
  private container: HTMLElement | null = null;
  private globe: HTMLButtonElement | null = null;
  private terrain: HTMLButtonElement | null = null;
  private shadow: HTMLButtonElement | null = null;
  private shadowSwitch: ShadowSwitch | null = null;
  private north: HTMLButtonElement | null = null;
  private needle: HTMLElement | null = null;
  private flat: HTMLButtonElement | null = null;
  private box: HTMLButtonElement | null = null;
  private section: HTMLButtonElement | null = null;
  /** Locale listeners cannot be removed; one is enough for the page. */
  private listening = false;

  constructor(private readonly options: ViewControlOptions) {}

  onAdd(map: MaplibreMap): HTMLElement {
    this.map = map;
    const container = document.createElement("div");
    container.className = "maplibregl-ctrl maplibregl-ctrl-group view-control";
    this.globe = this.button(container, "maplibregl-ctrl-globe", "viewGlobeAria", () => {
      const globe = map.getProjection()?.type === "globe";
      map.setProjection({ type: globe ? "mercator" : "globe" });
    });
    this.terrain = this.button(container, "maplibregl-ctrl-terrain", "viewTerrainAria", () => {
      if (map.getTerrain()) map.setTerrain(null);
      else map.setTerrain({ source: this.options.terrainSource, exaggeration: this.options.exaggeration() });
    });
    this.shadow = this.button(container, "view-control-shadow", "viewShadowAria", () => this.shadowSwitch?.toggle());
    this.shadow.querySelector(".maplibregl-ctrl-icon")!.innerHTML = SHADOW_ICON;
    this.shadow.hidden = this.shadowSwitch === null;
    this.north = this.button(container, "maplibregl-ctrl-compass", "viewResetNorthAria", () => {
      map.easeTo({ bearing: 0 });
    });
    this.needle = this.north.querySelector<HTMLElement>(".maplibregl-ctrl-icon");
    this.flat = this.button(container, "view-control-flat", "viewResetPitchAria", () => {
      map.easeTo({ pitch: 0 });
    });
    this.flat.querySelector(".maplibregl-ctrl-icon")!.innerHTML = PITCH_ICON;
    this.box = this.button(container, "view-control-box", "viewBoxAria", () => this.options.onVolumeTool?.("box"));
    this.box.querySelector(".maplibregl-ctrl-icon")!.innerHTML = BOX_ICON;
    this.section = this.button(container, "view-control-section", "viewSectionAria", () =>
      this.options.onVolumeTool?.("section"),
    );
    this.section.querySelector(".maplibregl-ctrl-icon")!.innerHTML = SECTION_ICON;
    this.box.hidden = true;
    this.section.hidden = true;
    this.container = container;
    map.on("projectiontransition", this.sync);
    map.on("terrain", this.sync);
    map.on("rotate", this.sync);
    map.on("pitch", this.sync);
    map.on("styledata", this.sync);
    if (!this.listening) {
      onLocaleChange(this.label);
      this.listening = true;
    }
    this.label();
    this.sync();
    return container;
  }

  onRemove(): void {
    const map = this.map;
    if (map) {
      map.off("projectiontransition", this.sync);
      map.off("terrain", this.sync);
      map.off("rotate", this.sync);
      map.off("pitch", this.sync);
      map.off("styledata", this.sync);
    }
    this.container?.remove();
    this.map = null;
  }

  private button(container: HTMLElement, className: string, label: MessageKey, onClick: () => void): HTMLButtonElement {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.dataset.label = label;
    const icon = document.createElement("span");
    icon.className = "maplibregl-ctrl-icon";
    icon.setAttribute("aria-hidden", "true");
    button.append(icon);
    button.addEventListener("click", onClick);
    container.append(button);
    return button;
  }

  private readonly label = (): void => {
    for (const button of [this.globe, this.terrain, this.shadow, this.north, this.flat, this.box, this.section]) {
      if (!button) continue;
      const text = t(button.dataset.label as MessageKey);
      button.title = text;
      button.setAttribute("aria-label", text);
    }
  };

  /** Offer the shadow switch, and show whether it is on. */
  setShadow(shadowSwitch: ShadowSwitch): void {
    this.shadowSwitch = shadowSwitch;
    if (this.shadow?.hidden) {
      this.shadow.hidden = false;
      this.options.onResize?.();
    }
    this.syncShadow();
  }

  /** Show whether the shadows are on, after they were switched. */
  syncShadow(): void {
    this.shadow?.setAttribute("aria-pressed", String(this.shadowSwitch?.enabled() ?? false));
  }

  /** Offer the volume tools, or not, and show which is armed or active. */
  setVolumeTools(state: VolumeToolState): void {
    if (!this.box || !this.section) return;
    this.box.setAttribute("aria-pressed", String(state.box));
    this.section.setAttribute("aria-pressed", String(state.section));
    if (this.box.hidden === state.available) {
      this.box.hidden = !state.available;
      this.section.hidden = !state.available;
      this.options.onResize?.();
    }
  }

  private readonly sync = (): void => {
    const map = this.map;
    if (!map || !this.globe || !this.terrain || !this.north || !this.flat) return;
    // On reads in the rail's language (an inked tile, the stylesheet's), not
    // in MapLibre's blue "enabled" icons.
    this.globe.setAttribute("aria-pressed", String(map.getProjection()?.type === "globe"));
    this.terrain.setAttribute("aria-pressed", String(map.getTerrain() !== null));
    const bearing = map.getBearing();
    if (this.needle) this.needle.style.transform = `rotate(${-bearing}deg)`;
    const turned = Math.abs(bearing) > BEARING_EPSILON;
    const tilted = map.getPitch() > PITCH_EPSILON;
    if (this.north.hidden === turned || this.flat.hidden === tilted) {
      this.north.hidden = !turned;
      this.flat.hidden = !tilted;
      this.options.onResize?.();
    }
  };
}

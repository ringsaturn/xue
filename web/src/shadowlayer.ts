import type { ImageSource, Map as MaplibreMap } from "maplibre-gl";

import { setFieldShadow } from "./layer";
import type { ShadowRequest, ShadowResponse, ShadowResult } from "./shadowtypes";

/**
 * Terrain cast shadows on the main thread: drives `shadow.worker.ts` and
 * hands each sunlit mask it answers with to the three things that draw it.
 *
 * 1. The basemap: the mask, inked for the ground, as a MapLibre `image`
 *    source under a `raster` layer directly above the hillshade. A raster
 *    layer is something MapLibre itself drapes onto the 3D terrain and the
 *    globe, which a custom layer never is (projection.ts), so the shadow lies
 *    on the very relief the hillshade shades.
 * 2. The forecast fields, which cover that layer: the mask goes to every
 *    field layer (`layer.ts::setFieldShadow`), which darkens by it, with the
 *    sun as the light the 3D relief is shaded from.
 * 3. The hillshade, lit from the sun's azimuth and altitude instead of
 *    MapLibre's fixed north-west light.
 *
 * Nothing here is part of the style `syncBasemapStyle` builds and diffs: the
 * source and layer are added at runtime and the three illumination
 * properties are ones the built hillshade never names, so a theme or locale
 * switch — a property diff between two builds — leaves all of them alone.
 *
 * A new mask is asked for when the time, the view, the ground or the
 * switch changes, one request at a time (the newest view wins when the
 * answer lands). The last mask stays up until the next one replaces it, so a
 * pan or a frame step never flashes bare ground. While the timeline plays
 * nothing is cast: every frame would be a new march competing with the
 * frame decoders for the CPU, and a mask a frame or two late would put the
 * shadows on the wrong side of the hills, so they go and come back on the
 * frame playback stops at.
 */

export const SHADOW_SOURCE = "terrain-shadow";
export const SHADOW_LAYER = "terrain-shadow";

/** How strongly a shadow darkens the forecast fields over it. */
const FIELD_SHADOW_STRENGTH = 0.45;
/** The lowest sun the 3D relief is shaded from, in degrees: below it the
 * sunward slopes saturate and everything else goes black, while the cast
 * shadows already say the sun is low. */
const MIN_LIGHT_ELEVATION = 5;
/** The coarsest zoom shadows are cast at. Below it a DEM pixel is hundreds
 * of metres to kilometres and a mountain's shadow is a few pixels at most,
 * while the march still costs a full view's worth of pixels; the switch
 * stays on and the shadows come back once the camera is close again. */
export const MIN_SHADOW_ZOOM = 9;
/** The widest mask side asked for, margin included. The worker marches it
 * on the GPU in tens of milliseconds; when it falls back to the CPU it
 * caps the size itself. */
const SHADOW_MAX_PIXELS = 2048;
/** The hillshade is relit only when the sun has moved this far, in
 * degrees: a paint change makes MapLibre redraw every draped terrain tile. */
const LIGHT_STEP = 2;
/** How long an answer may take before the next request goes out anyway. */
const REQUEST_TIMEOUT_MS = 15000;
const MERCATOR_MAX_LATITUDE = 85.05112878;
const TRANSPARENT_PIXEL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

/** The shadow ink per ground: the hillshade's own shadow hue (main.ts's
 * HILLSHADE_INK), warm brown on paper and black on the dark grounds. */
const SHADOW_INK = {
  light: { rgb: [107, 92, 66], alpha: 0.45 },
  dark: { rgb: [0, 0, 0], alpha: 0.55 },
} as const;

export interface TerrainShadowOptions {
  map: MaplibreMap;
  /** The hillshade layer the shadows sit directly above and relight. */
  hillshadeLayer: string;
  /** Whether the basemap's ground is a dark one, read at use. */
  darkGround: () => boolean;
  /** The switch changed (the address bar and the view tile follow). */
  onChange?: () => void;
}

export class TerrainShadows {
  private readonly map: MaplibreMap;
  private enabledState = false;
  private worker: Worker | null = null;
  private nextId = 0;
  private inFlight: { id: number; at: number } | null = null;
  private lastKey = "";
  private time = Date.now();
  private playing = false;
  /** Whether a mask is on screen. */
  private shown = false;
  private light = "";
  private darkGround: boolean;

  constructor(private readonly options: TerrainShadowOptions) {
    this.map = options.map;
    this.darkGround = options.darkGround();
    this.map.on("moveend", this.request);
  }

  get enabled(): boolean {
    return this.enabledState;
  }

  setEnabled(enabled: boolean): void {
    if (this.enabledState === enabled) return;
    this.enabledState = enabled;
    if (enabled) {
      this.lastKey = "";
      this.request();
    } else {
      this.hide();
    }
    this.options.onChange?.();
  }

  /** The instant the sun is placed at (the playhead's valid time, or now),
   * and whether the timeline is playing, which casts nothing. */
  setTime(time: number, playing: boolean): void {
    if (time === this.time && playing === this.playing) return;
    this.time = time;
    this.playing = playing;
    this.request();
  }

  /** Re-ink the basemap layer for the ground (the worker inks it). */
  setDarkGround(dark: boolean): void {
    if (dark === this.darkGround) return;
    this.darkGround = dark;
    this.request();
  }

  private readonly request = (): void => {
    if (!this.enabledState) return;
    if (this.playing || this.map.getZoom() < MIN_SHADOW_ZOOM) {
      this.lastKey = "";
      if (this.shown) this.hide();
      return;
    }
    const now = performance.now();
    if (this.inFlight && now - this.inFlight.at < REQUEST_TIMEOUT_MS) return;
    const zoom = this.map.getZoom();
    const bounds = viewBounds(this.map);
    const key = `${bounds.west.toFixed(4)},${bounds.south.toFixed(4)},${bounds.east.toFixed(4)},${bounds.north.toFixed(4)}@${zoom.toFixed(2)}:${this.time}:${this.darkGround}`;
    if (key === this.lastKey) return;
    this.lastKey = key;
    const id = (this.nextId += 1);
    this.inFlight = { id, at: now };
    const message: ShadowRequest = {
      type: "shadow",
      id,
      time: this.time,
      bounds,
      zoom,
      maxPixels: SHADOW_MAX_PIXELS,
      ink: SHADOW_INK[this.darkGround ? "dark" : "light"],
    };
    this.ensureWorker().postMessage(message);
  };

  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    const worker = new Worker(new URL("./shadow.worker.ts", import.meta.url), { type: "module" });
    worker.addEventListener("message", (event: MessageEvent<ShadowResponse>) => this.receive(event.data));
    worker.addEventListener("error", (event) => {
      console.warn("terrain shadow worker failed", event.message);
      this.inFlight = null;
    });
    this.worker = worker;
    return worker;
  }

  private receive(response: ShadowResponse): void {
    // Only one request is out at a time; anything else is an answer to one
    // that timed out and was superseded.
    if (response.id !== this.inFlight?.id) return;
    this.inFlight = null;
    if (response.type === "error") {
      console.warn("terrain shadows:", response.message);
      // Let the same view be asked for again on the next trigger.
      this.lastKey = "";
    } else if (this.enabledState && !this.playing && this.map.getZoom() >= MIN_SHADOW_ZOOM) {
      this.show(response);
    }
    // The view or the time may have moved while this one was computed.
    this.request();
  }

  private show(result: ShadowResult): void {
    this.showOnBasemap(result);
    const { azimuth, elevation } = result.sun;
    const az = (azimuth * Math.PI) / 180;
    const el = (Math.max(elevation, MIN_LIGHT_ELEVATION) * Math.PI) / 180;
    setFieldShadow({
      rect: result.rect,
      width: result.width,
      height: result.height,
      lit: result.lit,
      strength: FIELD_SHADOW_STRENGTH,
      light: [Math.sin(az) * Math.cos(el), Math.cos(az) * Math.cos(el), Math.sin(el)],
    });
    this.setHillshadeLight(azimuth, Math.max(elevation, 1));
    this.shown = true;
    this.map.triggerRepaint();
  }

  private showOnBasemap(result: ShadowResult): void {
    const map = this.map;
    const image = result.image;
    if (!image) return;
    const coordinates = rectCorners(result.rect);
    const source = map.getSource(SHADOW_SOURCE) as ImageSource | undefined;
    if (source) {
      source.updateImage({ image, coordinates });
    } else {
      // The style validator still requires a url even though the source
      // can start empty: a transparent pixel stands in until the mask lands.
      map.addSource(SHADOW_SOURCE, { type: "image", url: TRANSPARENT_PIXEL, coordinates });
      (map.getSource(SHADOW_SOURCE) as ImageSource | undefined)?.updateImage({ image, coordinates });
    }
    if (!map.getLayer(SHADOW_LAYER)) {
      map.addLayer(
        {
          id: SHADOW_LAYER,
          type: "raster",
          source: SHADOW_SOURCE,
          paint: { "raster-fade-duration": 0, "raster-resampling": "linear" },
        },
        layerAbove(map, this.options.hillshadeLayer),
      );
    } else {
      map.setLayoutProperty(SHADOW_LAYER, "visibility", "visible");
    }
  }

  private hide(): void {
    const map = this.map;
    this.shown = false;
    if (map.getLayer(SHADOW_LAYER)) map.setLayoutProperty(SHADOW_LAYER, "visibility", "none");
    setFieldShadow(null);
    this.setHillshadeLight(null, null);
    map.triggerRepaint();
  }

  /** Light the hillshade from the sun, or (null) give it MapLibre's own
   * defaults back by unsetting the properties. */
  private setHillshadeLight(azimuth: number | null, altitude: number | null): void {
    const map = this.map;
    const layer = this.options.hillshadeLayer;
    if (!map.getLayer(layer)) return;
    if (azimuth !== null && altitude !== null) {
      azimuth = Math.round(azimuth / LIGHT_STEP) * LIGHT_STEP;
      altitude = Math.round(altitude / LIGHT_STEP) * LIGHT_STEP || 1;
    }
    const light = `${azimuth}/${altitude}`;
    if (light === this.light) return;
    this.light = light;
    // Typed per property in the spec; the values are plain numbers either way.
    const set = map.setPaintProperty.bind(map) as (id: string, name: string, value: unknown) => void;
    set(layer, "hillshade-illumination-direction", azimuth ?? undefined);
    set(layer, "hillshade-illumination-altitude", altitude ?? undefined);
    set(layer, "hillshade-illumination-anchor", azimuth === null ? undefined : "map");
  }

}

/** The view's bounds as the worker wants them: unwrapped longitudes with
 * east > west, the western edge brought into [-180, 180), at most a world
 * wide, latitudes inside Web Mercator. */
export function viewBounds(map: MaplibreMap): ShadowRequest["bounds"] {
  const box = map.getBounds();
  return normalizeBounds(box.getWest(), box.getSouth(), box.getEast(), box.getNorth());
}

export function normalizeBounds(west: number, south: number, east: number, north: number): ShadowRequest["bounds"] {
  if (east - west >= 360) {
    const middle = (west + east) / 2;
    west = middle - 180;
    east = middle + 180;
  }
  const shift = Math.floor((west + 180) / 360) * 360;
  return {
    west: west - shift,
    east: east - shift,
    south: Math.max(-MERCATOR_MAX_LATITUDE, south),
    north: Math.min(MERCATOR_MAX_LATITUDE, north),
  };
}

/** The image source's corners for a Mercator rectangle (top left, then
 * clockwise). The longitudes stay unwrapped past ±180, which MapLibre's
 * image source places across the antimeridian as one picture. */
export function rectCorners(rect: ShadowResult["rect"]): [[number, number], [number, number], [number, number], [number, number]] {
  const west = rect.x0 * 360 - 180;
  const east = rect.x1 * 360 - 180;
  const north = latitudeOf(rect.y0);
  const south = latitudeOf(rect.y1);
  return [
    [west, north],
    [east, north],
    [east, south],
    [west, south],
  ];
}

function latitudeOf(y: number): number {
  return (Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180) / Math.PI;
}


/** The id of the layer just above `id`, which a layer inserted before it
 * lands directly over `id`; undefined puts it on top. */
function layerAbove(map: MaplibreMap, id: string): string | undefined {
  const layers = map.getStyle().layers ?? [];
  const at = layers.findIndex((layer) => layer.id === id);
  return at >= 0 ? layers[at + 1]?.id : undefined;
}

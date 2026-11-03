import type { BundleMetadata, BundleVariable, ForecastBundleId, KnownBundleId, VariantDescriptor } from "./manifest";
import type { VariableIdentity } from "./identity";
import type { TileGeometry, TileRect } from "./tiles";
import type { VolumeLevels } from "./volume";
import type { DecodeChannel } from "./webcodecs";

/** One resident per-variable bundle: its decode channel and embedded metadata.
 * `worker` is either a real Worker (Xue/WASM path) or a WebCodecs
 * `DecodeChannel` (video path, tmp2m only, when the browser supports it) —
 * both speak the same booted/init/ready/decode/frame/error protocol. */
export interface VariableSession {
  /** Bundle-level id this session was loaded for (wind10m carries two data
   * variables). */
  id: ForecastBundleId;
  /** This session's identity in the frame cache and the probe series.
   * Variables inside a `.xue` file are numbered 1..n *per file*, so a
   * numericId is meaningful only next to the session it came from: two open
   * bundles — a temperature fill under pressure lines — both carry variable
   * 1, and every key that leaves a session has to say which. */
  key: number;
  worker: DecodeChannel;
  metadata: BundleMetadata;
  /** What this bundle is, from its variables' GRIB2 parameter blocks (or,
   * for a schemaVersion 1/2 file, from the closed legacy id list). Null when
   * the file describes something this build has no chart knowledge for — it
   * is then drawn as a plain scalar off its own codebook. */
  identity: VariableIdentity | null;
  /** The registered bundle id `identity` corresponds to: the key every chart
   * registry (palette, legend, ground tone, instrument copy) is written
   * under. Null exactly when `identity` is, or when the field sits on a
   * surface nothing is registered on. */
  chartId: KnownBundleId | null;
  /** True when the bundle carries a u/v component pair — read from the
   * parameter blocks, not from the id string. */
  vector: boolean;
  /** True when the bundle carries three colour guns drawn as a picture —
   * read from the parameter and producer blocks, like `vector`. */
  composite: boolean;
  /** Primary data variable (drives palette/unit for scalars; the u component
   * for wind; the red gun for a composite). */
  variable: BundleVariable;
  /** Every data variable a frame of this session needs decoded — one for
   * scalars, the u and v pair for wind, the three guns for a composite. */
  variables: BundleVariable[];
  /** Delivery format actually in use for this variable: the container,
   * the same bytes read through the Zarr channel, or the video companion. */
  format: "H.264" | "Xue" | "Zarr";
  /** The resolution tier in use, as the data card names it beside the
   * format: empty at full resolution, else the rung's fraction of the
   * canonical grid ("½", "¼", "⅛"). */
  tier: string;
  /** The rung the session reads, or null on the canonical bundle: what a
   * later view is weighed against (`retierPrimary`). */
  variant: VariantDescriptor | null;
  /** The key its channel stamps on `progress` / `resident` messages,
   * unique to this session — a replacement opened for the same bundle at
   * another tier must not be credited to the session it replaces. */
  streamKey: string;
  /** Network bytes downloaded for this variable's artifacts only. */
  bytes: number;
  /** Total bytes of this variable's artifacts (stream + index). */
  totalBytes: number;
  /** Bytes outside the channel's own reporting (e.g. the video index),
   * added on top of streaming `progress` messages. */
  extraBytes: number;
  /** The bundle's tiling, or null when it has none — a container v1 file, or
   * the video path. Tiles are what make a viewport-sized decode and a
   * one-shot point series possible. */
  tiles: TileGeometry | null;
  /** On-demand range delivery; `bytes` grows as groups arrive. */
  streaming: boolean;
  /** Nothing left to fetch for what the viewer is looking at (immediately
   * true for full downloads). */
  resident: boolean;
  /** What `resident` covers: the whole bundle, or only the tiles the view
   * narrowed to — a narrowed session never fetches the rest, so it never
   * reaches whole-bundle residency, and the data card says which it is. */
  residentScope: "bundle" | "viewport";
  /** The tiles this session decodes for the current view, or null for the
   * whole plane. Every session on screen has its own: the filled field and
   * the lines over it come from different bundles, with different grids and
   * tilings, and each narrows to what the view needs of *its* grid. */
  viewTiles: TileRect[] | null;
  /** Lead seconds → this bundle's frame offset, built on first use. An
   * overlay is keyed off the primary session's timeline, and the two axes can
   * differ (ECMWF prate has no analysis frame), so a frame is found by lead
   * time rather than by index. */
  leadOffsets: Map<number, number> | null;
  /** The altitude levels of a volume bundle (the MRMS reflectivity cube,
   * the WRF cloud water), drawn by the volume layer from all of them at
   * once; null for every other bundle. */
  volume: VolumeLevels | null;
}

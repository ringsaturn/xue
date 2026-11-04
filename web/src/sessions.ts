// The session registry: every per-variable decode session the page holds —
// the run's own, keyed by bundle id, and the mosaic members', keyed by
// member and bundle — how one is opened (tier, channel, handshake,
// identity) with concurrent opens of the same bundle folded into one, the
// channel progress that reaches a session by its stream key, and how
// sessions end. What is on screen (the primary, the slots) stays in
// `main.ts`; the types are `session.ts`.
import { CRC32_INITIAL, crc32Hex, crc32Update } from "./crc32";
import { fetchImmutable } from "./fetchimmutable";
import { t, type MessageKey } from "./i18n";
import type { CompositeField, VectorField } from "./layer";
import { isWaveVector, vectorMaxMagnitude } from "./levels";
import {
  identifyBundle,
  identityForBundleId,
  identityForParameter,
  isCompositeIdentity,
  registeredBundleId,
  sameIdentity,
  type VariableIdentity,
} from "./identity";
import {
  axisUnitSeconds,
  containerOf,
  frameOffsets,
  type BundleMetadata,
  type BundleVariable,
  type ForecastBundleId,
  type ForecastManifest,
  type ForecastModelId,
  type VariantDescriptor,
} from "./manifest";
import type { DecodeChannel, VariableSession } from "./session";
import { buildVapourFluxPalette, buildWaveFieldPalette, buildWindFieldPalette } from "./palettes";
import { coverageBox, WHOLE_PLANE_COVERAGE, type CoverageBox, type TileGeometry, type TileRect } from "./tiles";
import type { DataBackend } from "./urlstate";
import { VOLUME_BUNDLE_LEVELS, volumeLevels } from "./volume";
import { spawnZarrWorker, zarrInitMessage, zarrProbeUrl, zarrStoreFor } from "./zarr/channel";

/** Who a session is opened for: the primary layer, an overlay slot, or the
 * probe panel's rows. */
export type SessionRole = "primary" | "overlay" | "probe";

/** A mosaic member a session reads: its own run rather than the page's. */
export interface SessionMember {
  readonly id: ForecastModelId;
  readonly manifest: ForecastManifest;
  readonly manifestUrl: string;
}

export interface SessionLoadOptions {
  /** Open a fresh session even where one is resident, which the new
   * one then replaces under the bundle id; the old one is the caller's
   * to dispose (`retierPrimary`). */
  replace?: boolean;
  /** The rung to open, null for the canonical bundle, instead of what
   * the view would pick now. */
  tier?: VariantDescriptor | null;
}

/** A `progress` / `resident` message off a streaming decode channel. */
export type StreamMessage = {
  type: string;
  variableKey?: unknown;
  bytes?: unknown;
  totalBytes?: unknown;
  scope?: unknown;
};

/** What the registry reads off the page: the run and the open sequence
 * (live, through getters), the view's tier policy, and the page's channel
 * plumbing — the handshake and the binding route a channel's messages into
 * the frame cache and the probe, which stay with `main.ts`. */
export interface SessionRegistryDeps {
  readonly manifest: ForecastManifest | null;
  readonly manifestUrl: string | null;
  readonly initializeSequence: number;
  /** A showcase case is open: a store it cannot open is an error, never a
   * reason to reload for a newer shell. */
  readonly caseOpen: boolean;
  readonly dataBackend: DataBackend;
  /** The rung the view would pick for a bundle now. */
  readonly pickTier: (
    variableId: ForecastBundleId,
    descriptor: ForecastManifest["bundles"][number],
    run: ForecastManifest,
    role: SessionRole,
  ) => VariantDescriptor | null;
  readonly zarrPayloadBudgetBytes: () => number;
  readonly showError: (message: string) => void;
  /** The load status line or the data card's preload state, for the
   * primary's open only. */
  readonly status: (line: "load" | "preload", key: MessageKey, params?: Record<string, string | number>) => void;
  /** The data card's progress bar, while a whole bundle downloads. */
  readonly downloadProgress: (bytes: number, total: number) => void;
  readonly initializeChannel: (
    channel: DecodeChannel,
    initMessage: unknown,
    transfer: Transferable[],
    sequence: number,
  ) => Promise<{ worker: DecodeChannel; metadata: BundleMetadata; tiles: TileGeometry | null }>;
  readonly bindChannel: (session: VariableSession, sequence: number) => void;
}

/** A live run's store this shell could not open (`load`): carried
 * to `initialize`'s recovery as its own kind so a tab older than the data
 * reloads once, the way it does for a manifest it refuses. */
export class StoreRejectedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StoreRejectedError";
  }
}

export function memberSessionKey(member: ForecastModelId, variableId: ForecastBundleId): string {
  return `${member}:${variableId}`;
}

/** Absolute artifact URL, resolved against the run manifest's own URL
 * (manifest paths are manifest-relative, HLS style). Artifact paths are stable
 * per run, but their content can be re-encoded (e.g. a codebook change);
 * keying the URL on the artifact CRC keeps a returning visitor's HTTP cache
 * from serving bytes the integrity checks reject. */
export function artifactUrl(path: string, crc32: string, base: string | null): string {
  if (!base) throw new Error("manifest not loaded");
  const url = new URL(path, base);
  url.searchParams.set("v", crc32);
  return url.href;
}

/** True when the server honors single byte ranges with exact 206 responses,
 * which is what the on-demand streaming paths require. A single-range GET is
 * a CORS-safelisted request, so this needs no preflight. */
async function supportsRangeRequests(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { headers: { Range: "bytes=0-0" } });
    const supported = response.status === 206;
    await response.body?.cancel();
    return supported;
  } catch {
    return false;
  }
}

/** The rung's fraction of the canonical grid, read off the ladder itself:
 * the widest rung is the half tier, so the canonical width is twice it. */
export function tierGlyph(variant: VariantDescriptor | null, variants: VariantDescriptor[] | undefined): string {
  if (!variant || !variants?.length) return "";
  const widest = Math.max(...variants.map((rung) => rung.width));
  const factor = Math.round((widest * 2) / variant.width);
  return { 2: "½", 4: "¼", 8: "⅛" }[factor] ?? `1/${factor}`;
}

/** The variables to draw from a bundle whose parameter blocks say nothing
 * this build recognizes: the one named like the bundle, else simply the
 * first. A two-variable bundle that is not a component pair in the table is
 * not a vector field, so only its first variable is drawn. A bundle named by
 * the convention is still held to carrying the variable that name promises —
 * a file contradicting its own manifest entry is a broken build, not a new
 * layer. */
function fallbackVariables(variableId: ForecastBundleId, bundleMetadata: BundleMetadata): BundleVariable[] {
  const named = bundleMetadata.variables.find((item) => item.id === variableId);
  if (named) return [named];
  if (identityForBundleId(variableId) !== null) throw new Error(t("bundleMissingVariable", { id: variableId }));
  return [bundleMetadata.variables[0]!];
}

/** The manifest can only be read by the naming convention, and the file
 * itself is authoritative. Where they disagree the parameter block wins
 * silently for rendering — but a run publishing `tmp850` bytes under the name
 * `rh850` is a pipeline bug the console should say out loud. Diagnostics stay
 * English in both locales. */
function warnOnIdentityMismatch(variableId: ForecastBundleId, identity: VariableIdentity | null): void {
  const guess = identityForBundleId(variableId);
  if (guess === null || sameIdentity(guess, identity)) return;
  console.warn(
    `bundle "${variableId}" is named for ${guess.family}${guess.level ?? ""} but its parameter block says ` +
      (identity === null ? "something this build does not know" : `${identity.family}${identity.level ?? ""}`) +
      "; the file wins",
  );
}

/** The session registry (`createSessionRegistry`): owns the open sessions
 * and opens new ones. */
export function createSessionRegistry(deps: SessionRegistryDeps) {
  const sessions = new Map<ForecastBundleId, VariableSession>();
  const sessionLoads = new Map<ForecastBundleId, Promise<VariableSession>>();
  /** Sessions opened for the mosaic's other members, keyed by member and
   * bundle: they read other runs than the page's manifest, so they live
   * beside `sessions` rather than in it. */
  const memberSessions = new Map<string, VariableSession>();
  const memberSessionLoads = new Map<string, Promise<VariableSession>>();
  /** Every open session by its `streamKey`, for routing the channel's
   * progress: a run's own sessions and the mosaic members' alike. */
  const streamSessions = new Map<string, VariableSession>();
  /** Streaming progress that arrived before its session finished registering. */
  const pendingStream = new Map<
    string,
    { bytes: number; resident: boolean; scope: VariableSession["residentScope"] }
  >();
  /** Hands out `VariableSession.key`. Monotonic for the life of the page, so a
   * reloaded session never inherits a stale session's cache entries. */
  let nextSessionKey = 1;

  function spawnWorker(): Worker {
    const decodeWorker = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
    decodeWorker.onerror = (event) => {
      deps.showError(event.message || t("workerStartFailed"));
    };
    return decodeWorker;
  }

  /** Downloads and CRC32-verifies one manifest-declared `.xue` bundle. */
  async function downloadBundle(
    descriptor: { path: string; byteLength: number; crc32: string },
    sequence: number,
    quiet: boolean,
    base: string | null,
  ): Promise<ArrayBuffer> {
    if (!quiet) deps.status("preload", "receivingBundle");
    const response = await fetchImmutable(artifactUrl(descriptor.path, descriptor.crc32, base));
    if (!response.ok) throw new Error(t("bundleRequestFailed", { status: response.status }));
    const total = descriptor.byteLength;
    const data = new Uint8Array(total);
    let offset = 0;
    let crc = CRC32_INITIAL;

    if (response.body) {
      const reader = response.body.getReader();
      while (true) {
        const result = await reader.read();
        if (result.done) break;
        if (sequence !== deps.initializeSequence) throw new DOMException("aborted", "AbortError");
        const chunk = result.value;
        if (offset + chunk.byteLength > total) throw new Error(t("bundleTooLong"));
        data.set(chunk, offset);
        offset += chunk.byteLength;
        crc = crc32Update(crc, chunk);
        if (quiet) continue;
        deps.downloadProgress(offset, total);
        deps.status("load", "receivingBundlePercent", { percent: Math.round((offset / total) * 100) });
      }
    } else {
      const buffer = new Uint8Array(await response.arrayBuffer());
      if (buffer.byteLength > total) throw new Error(t("bundleTooLong"));
      data.set(buffer, 0);
      offset = buffer.byteLength;
      crc = crc32Update(crc, buffer);
      if (!quiet) deps.downloadProgress(offset, total);
    }

    if (offset !== total) throw new Error(t("bundleLengthMismatch"));
    if (crc32Hex(crc) !== descriptor.crc32) throw new Error(t("bundleChecksumMismatch"));
    return data.buffer;
  }

  /** Download and initialize one variable's bundle; resident sessions are
   * reused. An overlay loads quietly — the data card and the status line
   * describe the primary — and takes the half-resolution tier where one is
   * offered: contour lines are smoothed again in the shader, and nothing the
   * lines slot draws needs the full grid, so the bytes go to the field the
   * viewer is actually reading. The exception is a coarse grid, where the
   * half tier has too few samples left to trace a line through
   * (`overlayResolutionPreference`). `?res=` still pins every session.
   *
   * A probe session (the meteogram's rows) is the primary's tier opened
   * quietly on the streaming path alone: it exists to read one cell's series
   * out of one tile, so it never downloads a bundle whole — where the origin cannot serve ranges it is not opened at
   * all. The session it makes is otherwise the one a layer would have made,
   * and the rail reuses it should the viewer switch to that field. */
  function load(
    variableId: ForecastBundleId,
    sequence: number,
    role: SessionRole = "primary",
    member: SessionMember | null = null,
    options: SessionLoadOptions = {},
  ): Promise<VariableSession> {
    // A mosaic member's session reads that member's run, not the page's
    // manifest, and is filed beside the run's own sessions under the
    // member's key.
    const memberKey = member ? memberSessionKey(member.id, variableId) : null;
    const resident = options.replace ? undefined : memberKey !== null ? memberSessions.get(memberKey) : sessions.get(variableId);
    if (resident) return Promise.resolve(resident);
    const pending = memberKey !== null ? memberSessionLoads.get(memberKey) : sessionLoads.get(variableId);
    if (pending) return pending;
    const run = member?.manifest ?? deps.manifest;
    const runUrl = member?.manifestUrl ?? deps.manifestUrl;
    // The session's cache identity, taken now so the channel's progress can
    // carry it from the first byte: the key names this session alone.
    const sessionKey = nextSessionKey++;
    const variableKey = `${memberKey ?? variableId}#${sessionKey}`;
    const opening = (async () => {
      if (!run) throw new Error("manifest not loaded");
      const descriptor = run.bundles.find((bundle) => bundle.variable === variableId);
      if (!descriptor) throw new Error(t("manifestMissingBundle", { id: variableId }));

      let channel: DecodeChannel;
      let initMessage: unknown = { type: "init" };
      let transfer: Transferable[] = [];
      let format: VariableSession["format"];
      let downloadedBytes: number;
      let totalBytes: number;
      let streaming = false;
      // Pick a resolution tier before choosing the decode path. The view's
      // pixels say which tier it could show; the cell budget says which tier
      // a frame of it can afford to hold. The caller may have weighed the
      // rung already (`retierPrimary`), and then nothing is read at all.
      const quiet = role !== "primary";
      const variant = options.tier !== undefined ? options.tier : deps.pickTier(variableId, descriptor, run, role);
      const target = variant ?? descriptor;
      const container = containerOf(target);
      // The store is the everyday path wherever the tier carries one
      // (`?backend=xue` asks for the container instead), streamed over
      // ranges. An origin that serves none sends the session to the
      // container — streamed, else downloaded whole — and only an entry
      // that ships no container at all opens its store by whole objects,
      // one shard (one temporal group) per GET: what a whole bundle
      // download costs, group by group. A probe session is streamed or
      // nothing either way.
      //
      // A probe session reads one cell's series and nothing else, so where
      // the bundle ships a series companion it opens that instead: one inner
      // chunk is the whole axis of the cell's block, where the map store
      // costs one chunk per time chunk. The companion is full resolution, so
      // a chosen tier's series is the same read; the `.xue` container has no
      // series form, so `?backend=xue` keeps the map path.
      const seriesStore = role === "probe" && deps.dataBackend !== "xue" ? descriptor.series : undefined;
      const store = seriesStore ?? zarrStoreFor(deps.dataBackend, descriptor, variant);
      const storeStreams = store !== undefined && runUrl ? await supportsRangeRequests(zarrProbeUrl(store, runUrl)) : false;
      if (sequence !== deps.initializeSequence) throw new DOMException("aborted", "AbortError");
      if (store !== undefined && runUrl && (storeStreams || !container)) {
        if (!storeStreams && role === "probe") {
          throw new Error("range requests unsupported; a probe session never downloads a whole shard");
        }
        streaming = true;
        channel = spawnZarrWorker();
        channel.onerror = (event) => deps.showError(event.message || t("workerStartFailed"));
        initMessage = zarrInitMessage(store, descriptor.variable, runUrl, variableKey, storeStreams, deps.zarrPayloadBudgetBytes());
        downloadedBytes = 0;
        format = "Zarr";
        totalBytes = store.byteLength;
      } else if (container) {
        const url = artifactUrl(container.path, container.crc32, runUrl);
        streaming = await supportsRangeRequests(url);
        if (sequence !== deps.initializeSequence) throw new DOMException("aborted", "AbortError");
        if (streaming) {
          channel = spawnWorker();
          initMessage = {
            type: "init-stream",
            url,
            byteLength: container.byteLength,
            variableKey,
          };
          downloadedBytes = 0;
        } else if (role === "probe") {
          throw new Error("range requests unsupported; a probe session never downloads a whole bundle");
        } else {
          const initBuffer = await downloadBundle(container, sequence, quiet, runUrl);
          if (sequence !== deps.initializeSequence) throw new DOMException("aborted", "AbortError");
          channel = spawnWorker();
          initMessage = { type: "init", buffer: initBuffer };
          transfer = [initBuffer];
          downloadedBytes = container.byteLength;
        }
        format = "Xue";
        totalBytes = container.byteLength;
      } else {
        // The validator admits no entry without one delivery or the other.
        throw new Error(t("manifestMissingBundle", { id: variableId }));
      }

      if (!quiet) deps.status("load", streaming ? "readingIndex" : "initializingDecoder");
      let opened: Awaited<ReturnType<SessionRegistryDeps["initializeChannel"]>>;
      try {
        opened = await deps.initializeChannel(channel, initMessage, transfer, sequence);
      } catch (error) {
        // A store this shell cannot open on a run newer than itself is, like
        // a manifest it refuses, most likely one a newer shell reads: the
        // profile's shard layout changed once already. Same cure, same guard.
        if (format.startsWith("Zarr") && role === "primary" && !deps.caseOpen) {
          throw new StoreRejectedError(error instanceof Error ? error.message : String(error));
        }
        throw error;
      }
      const { worker: sessionWorker, metadata: bundleMetadata, tiles } = opened;
      if (sequence !== deps.initializeSequence) {
        sessionWorker.terminate();
        throw new DOMException("aborted", "AbortError");
      }
      if (run && Date.parse(bundleMetadata.runTime) !== Date.parse(run.runTime)) {
        throw new Error(t("bundleRunMismatch"));
      }
      // Grids may differ between sessions (resolution tiers), and time
      // axes may differ per variable since the ECMWF source: its de-accumulated
      // prate has no analysis frame, so that series starts at the first real
      // step. syncTimeline() rebuilds the timeline for whichever session is
      // active; only the run cycle above must agree.
      // What the bundle *is* comes from its own variables' GRIB2 parameter
      // blocks, not from the name the manifest filed it under: a vector bundle
      // is one whose two variables are a component pair in the table, and the
      // u/v order is the table's. A file this build cannot place renders its
      // first variable as a plain scalar.
      // A volume bundle is one quantity on many altitudes; a frame of it is
      // every level, and it is what its levels' parameter says: cloud water
      // is a chart of its own, and MRMS's local reflectivity number, which
      // no chart is keyed by, takes the composite reflectivity's colours.
      const volume = VOLUME_BUNDLE_LEVELS.has(variableId) ? volumeLevels(bundleMetadata.variables) : null;
      const derived = volume ? null : identifyBundle(bundleMetadata.variables);
      const sessionVariables = volume?.variables ?? derived?.variables ?? fallbackVariables(variableId, bundleMetadata);
      const volumeParameter = volume?.variables[0]?.parameter;
      const identity: VariableIdentity | null = volume
        ? ((volumeParameter ? identityForParameter(volumeParameter) : null) ?? { family: "cref", level: null, vector: false })
        : (derived?.identity ?? null);
      warnOnIdentityMismatch(variableId, identity);
      const session: VariableSession = {
        id: variableId,
        key: sessionKey,
        worker: sessionWorker,
        metadata: bundleMetadata,
        identity,
        chartId: registeredBundleId(identity),
        vector: identity?.vector === true && sessionVariables.length >= 2,
        composite: isCompositeIdentity(identity) && sessionVariables.length >= 3,
        variable: sessionVariables[0]!,
        variables: sessionVariables,
        format,
        tier: tierGlyph(variant, descriptor.variants),
        variant,
        streamKey: variableKey,
        bytes: downloadedBytes,
        totalBytes,
        tiles,
        streaming,
        resident: !streaming,
        residentScope: "bundle",
        viewTiles: null,
        leadOffsets: null,
        volume,
      };
      // Streaming progress may have raced ahead of session registration.
      const early = pendingStream.get(variableKey);
      if (early) {
        pendingStream.delete(variableKey);
        if (early.bytes > 0) session.bytes = Math.min(totalBytes, early.bytes);
        if (early.resident) {
          session.resident = true;
          session.residentScope = early.scope;
        }
      }
      if (memberKey !== null) memberSessions.set(memberKey, session);
      else sessions.set(variableId, session);
      streamSessions.set(variableKey, session);
      deps.bindChannel(session, sequence);
      return session;
    })();
    if (memberKey !== null) {
      memberSessionLoads.set(memberKey, opening);
      return opening.finally(() => memberSessionLoads.delete(memberKey));
    }
    sessionLoads.set(variableId, opening);
    return opening.finally(() => sessionLoads.delete(variableId));
  }

  /** Credit a `progress` / `resident` message to the session its stream key
   * names, and return that session; one not registered yet keeps it for
   * when it is, and null is returned. */
  function noteStream(message: StreamMessage): VariableSession | null {
    const id = message.variableKey;
    if (typeof id !== "string") return null;
    const session = streamSessions.get(id);
    if (!session) {
      const entry = pendingStream.get(id) ?? { bytes: 0, resident: false, scope: "bundle" as const };
      if (message.type === "progress" && typeof message.bytes === "number") entry.bytes = message.bytes;
      if (message.type === "resident") {
        entry.resident = true;
        entry.scope = message.scope === "viewport" ? "viewport" : "bundle";
      }
      pendingStream.set(id, entry);
      return null;
    }
    if (message.type === "progress" && typeof message.bytes === "number") {
      session.bytes = Math.min(session.totalBytes, message.bytes);
    }
    if (message.type === "resident") {
      session.resident = true;
      session.residentScope = message.scope === "viewport" ? "viewport" : "bundle";
    }
    return session;
  }

  /** End one session's channel and stop routing its stream key to it; the
   * planes it decoded are the frame cache's to drop (`disposeSession`). */
  function release(session: VariableSession): void {
    session.worker.terminate();
    streamSessions.delete(session.streamKey);
  }

  /** End the mosaic members' sessions: every one, or one member's. Their
   * stream keys are left as they are, as they always were. */
  function endMembers(member: ForecastModelId | null = null): void {
    if (member === null) {
      for (const session of memberSessions.values()) session.worker.terminate();
      memberSessions.clear();
      memberSessionLoads.clear();
      return;
    }
    for (const [key, session] of memberSessions) {
      if (!key.startsWith(`${member}:`)) continue;
      session.worker.terminate();
      memberSessions.delete(key);
    }
  }

  /** End the run's own sessions and forget every stream key, before a run
   * is opened again. */
  function endRun(): void {
    pendingStream.clear();
    for (const session of sessions.values()) session.worker.terminate();
    sessions.clear();
    sessionLoads.clear();
    streamSessions.clear();
  }

  return {
    /** The run's own sessions by bundle id. */
    sessions,
    load,
    noteStream,
    release,
    endMembers,
    endRun,
    /** True while a session for the run's bundle is being opened. */
    loading: (id: ForecastBundleId): boolean => sessionLoads.has(id),
    /** A mosaic member's resident session for a bundle. */
    member: (member: ForecastModelId, variableId: ForecastBundleId): VariableSession | undefined =>
      memberSessions.get(memberSessionKey(member, variableId)),
  };
}

export type SessionRegistry = ReturnType<typeof createSessionRegistry>;

// What one session's own axis, tiles and codebooks say, read off the
// session alone.

/** Where a session's frame offset for a lead time comes from: exact match
 * on its own axis, or null when the axis has no frame there. */
export function sessionOffsetForLead(session: VariableSession, seconds: number): number | null {
  if (!session.leadOffsets) {
    const time = session.metadata.time;
    const unit = axisUnitSeconds(time);
    session.leadOffsets = new Map(frameOffsets(time).map((offset) => [offset * unit, offset]));
  }
  return session.leadOffsets.get(seconds) ?? null;
}

/** The texture box a plane's tiles fill on its own session grid — what the
 * shader clips to, so the stale bytes outside them are never painted. */
export function sessionCoverage(session: VariableSession, tiles: TileRect[] | null): CoverageBox {
  if (!tiles || !session.tiles) return WHOLE_PLANE_COVERAGE;
  const grid = session.metadata.grid;
  return coverageBox(session.tiles, grid.width, grid.height, tiles);
}

/** The ceiling a vector session's magnitude palette tops out at, from what
 * the field is rather than what it is called. */
export function sessionMaxMagnitude(session: VariableSession): number {
  const identity = session.identity;
  return identity === null ? vectorMaxMagnitude("wind", null) : vectorMaxMagnitude(identity.family, identity.level);
}

/** The magnitude palette of a vector session: the wind ramp up to the
 * field's own ceiling, the vapour flux ramp, or — for the wave vector,
 * whose magnitude is the significant wave height — the wave height ramp. */
export function vectorPalette(session: VariableSession): Uint8Array {
  const max = sessionMaxMagnitude(session);
  const family = session.identity?.family;
  if (family === "qflux") return buildVapourFluxPalette(max);
  if (family !== undefined && isWaveVector(family)) return buildWaveFieldPalette(max);
  return buildWindFieldPalette(max);
}

const WAVE_PARTICLE_PACE = 3;

/** How fast the particles run over a vector session's field, relative to
 * the wind's pace, where the field is a speed: the wave vector's magnitude
 * is a height, and a metre of sea is made to travel like three metres a
 * second of wind — a three-metre sea like a fresh breeze, a storm sea past
 * the ramp's ceiling like a gale. */
export function sessionParticlePace(session: VariableSession): number {
  const family = session.identity?.family;
  return family !== undefined && isWaveVector(family) ? WAVE_PARTICLE_PACE : 1;
}

/** A vector bundle's own decode for the layer's magnitude mode: each
 * component's linear codebook, plus the magnitude the palette tops out at.
 * Null when either component is not linearly quantized — nothing published
 * is, and a magnitude has no meaning without both. */
export function windVectorField(session: VariableSession): VectorField | null {
  const [u, v] = session.variables;
  if (u?.quantization.type !== "linear" || v?.quantization.type !== "linear") return null;
  // One reserved code serves both channels; the encoder gives the pair the
  // same codebook, and a file that did not could not be drawn as one field.
  if (u.quantization.nodataCode !== v.quantization.nodataCode) return null;
  return {
    offset: [u.quantization.offset, v.quantization.offset],
    scale: [u.quantization.scale, v.quantization.scale],
    nodataCode: u.quantization.nodataCode,
    maxMagnitude: sessionMaxMagnitude(session),
  };
}

/** A composite bundle's own decode for the layer's colour mode: each gun's
 * linear codebook. Null when any gun is not linearly quantized — nothing
 * published is, and a picture has no meaning without all three. */
export function compositeField(session: VariableSession): CompositeField | null {
  const [r, g, b] = session.variables;
  if (r?.quantization.type !== "linear" || g?.quantization.type !== "linear" || b?.quantization.type !== "linear") return null;
  return {
    offset: [r.quantization.offset, g.quantization.offset, b.quantization.offset],
    scale: [r.quantization.scale, g.quantization.scale, b.quantization.scale],
  };
}

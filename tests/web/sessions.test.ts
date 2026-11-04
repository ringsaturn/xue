import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BundleMetadata, ForecastManifest } from "../../web/src/manifest";
import type { DecodeChannel, VariableSession } from "../../web/src/session";
import {
  artifactUrl,
  createSessionRegistry,
  memberSessionKey,
  sessionCoverage,
  sessionOffsetForLead,
  tierGlyph,
  type SessionRegistryDeps,
} from "../../web/src/sessions";
import { WHOLE_PLANE_COVERAGE } from "../../web/src/tiles";
import { globalGrid, registryEntries, registryVariable } from "./helpers";

const RUN_TIME = "2026-08-15T06:00:00Z";

/** A one-bundle run whose `tmp2m` ships only the container. */
function manifest(): ForecastManifest {
  return {
    runTime: RUN_TIME,
    bundles: [{ variable: "tmp2m", path: "tmp2m.xue", byteLength: 1000, crc32: "0000abcd" }],
  } as unknown as ForecastManifest;
}

function metadata(): BundleMetadata {
  const entry = registryEntries(["tmp2m"]).tmp2m!;
  return { ...globalGrid(), variables: [registryVariable("tmp2m", entry, entry.quality)] } as BundleMetadata;
}

/** The channel a fake `Worker` hands the registry; `terminate` is counted. */
class FakeWorker implements DecodeChannel {
  static spawned: FakeWorker[] = [];
  terminated = 0;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  constructor() {
    FakeWorker.spawned.push(this);
  }
  postMessage(): void {}
  terminate(): void {
    this.terminated += 1;
  }
}

function deps(overrides: Partial<SessionRegistryDeps> = {}): SessionRegistryDeps & { bound: VariableSession[] } {
  const bound: VariableSession[] = [];
  return {
    manifest: manifest(),
    manifestUrl: "https://example.test/gfs/2026081506/manifest.json",
    initializeSequence: 1,
    caseOpen: false,
    dataBackend: "zarr",
    pickTier: () => null,
    zarrPayloadBudgetBytes: () => 0,
    showError: () => {},
    status: () => {},
    downloadProgress: () => {},
    initializeChannel: async (channel) => ({ worker: channel, metadata: metadata(), tiles: null }),
    bindChannel: (session) => bound.push(session),
    bound,
    ...overrides,
  };
}

describe("session registry", () => {
  beforeEach(() => {
    FakeWorker.spawned = [];
    vi.stubGlobal("Worker", FakeWorker);
    // Every origin here serves ranges, so the container is streamed.
    vi.stubGlobal("fetch", async () => ({ status: 206, body: { cancel: async () => {} } }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("folds concurrent opens of one bundle into one session", async () => {
    const page = deps();
    const registry = createSessionRegistry(page);
    const first = registry.load("tmp2m", 1);
    const second = registry.load("tmp2m", 1);
    expect(registry.loading("tmp2m")).toBe(true);
    const [a, b] = await Promise.all([first, second]);
    expect(a).toBe(b);
    expect(FakeWorker.spawned).toHaveLength(1);
    expect(page.bound).toEqual([a]);
    expect(registry.loading("tmp2m")).toBe(false);
    expect(registry.sessions.get("tmp2m")).toBe(a);
    // A resident session is handed back without opening anything.
    expect(await registry.load("tmp2m", 1)).toBe(a);
    expect(FakeWorker.spawned).toHaveLength(1);
  });

  it("opens a replacement beside a resident session with a key of its own", async () => {
    const registry = createSessionRegistry(deps());
    const old = await registry.load("tmp2m", 1);
    const replacement = await registry.load("tmp2m", 1, "primary", null, { replace: true });
    expect(replacement).not.toBe(old);
    expect(replacement.key).toBeGreaterThan(old.key);
    expect(replacement.streamKey).not.toBe(old.streamKey);
    expect(registry.sessions.get("tmp2m")).toBe(replacement);
  });

  it("credits progress that raced registration to the session once it registers", async () => {
    const registry = createSessionRegistry(
      deps({
        initializeChannel: async (channel) => {
          // The stream key the session will carry: the bundle id and key 1.
          expect(registry.noteStream({ type: "progress", variableKey: "tmp2m#1", bytes: 400 })).toBeNull();
          expect(registry.noteStream({ type: "resident", variableKey: "tmp2m#1", scope: "viewport" })).toBeNull();
          return { worker: channel, metadata: metadata(), tiles: null };
        },
      }),
    );
    const session = await registry.load("tmp2m", 1);
    expect(session.streamKey).toBe("tmp2m#1");
    expect(session.bytes).toBe(400);
    expect(session.resident).toBe(true);
    expect(session.residentScope).toBe("viewport");
    expect(registry.noteStream({ type: "progress", variableKey: "tmp2m#1", bytes: 5000 })).toBe(session);
    expect(session.bytes).toBe(1000);
  });

  it("stops routing a released session's stream and terminates its channel", async () => {
    const registry = createSessionRegistry(deps());
    const session = await registry.load("tmp2m", 1);
    registry.release(session);
    expect(FakeWorker.spawned[0]!.terminated).toBe(1);
    expect(registry.noteStream({ type: "progress", variableKey: session.streamKey, bytes: 10 })).toBeNull();
  });

  it("files a member's session under the member and ends it with the member", async () => {
    const registry = createSessionRegistry(deps({ manifest: null, manifestUrl: null }));
    const member = { id: "himawari", manifest: manifest(), manifestUrl: "https://example.test/himawari/manifest.json" } as const;
    const session = await registry.load("tmp2m", 1, "overlay", member as never);
    expect(session.streamKey).toBe(`${memberSessionKey("himawari" as never, "tmp2m")}#1`);
    expect(registry.member("himawari" as never, "tmp2m")).toBe(session);
    expect(registry.sessions.size).toBe(0);
    registry.endMembers("himawari" as never);
    expect(registry.member("himawari" as never, "tmp2m")).toBeUndefined();
    expect(FakeWorker.spawned[0]!.terminated).toBe(1);
  });

  it("abandons an open the page has moved past", async () => {
    let sequence = 1;
    const page = deps({
      initializeChannel: async (channel) => {
        sequence = 2;
        return { worker: channel, metadata: metadata(), tiles: null };
      },
    });
    Object.defineProperty(page, "initializeSequence", { get: () => sequence });
    const registry = createSessionRegistry(page);
    await expect(registry.load("tmp2m", 1)).rejects.toMatchObject({ name: "AbortError" });
    expect(FakeWorker.spawned[0]!.terminated).toBe(1);
    expect(registry.sessions.size).toBe(0);
    expect(registry.loading("tmp2m")).toBe(false);
  });

  it("refuses to open anything before a run is loaded", async () => {
    const registry = createSessionRegistry(deps({ manifest: null }));
    await expect(registry.load("tmp2m", 1)).rejects.toThrow("manifest not loaded");
    expect(registry.loading("tmp2m")).toBe(false);
  });
});

describe("session helpers", () => {
  it("keys an artifact URL on its crc against the manifest", () => {
    expect(artifactUrl("tmp2m.xue", "0000abcd", "https://example.test/gfs/run/manifest.json?v=1")).toBe(
      "https://example.test/gfs/run/tmp2m.xue?v=0000abcd",
    );
    expect(() => artifactUrl("tmp2m.xue", "0000abcd", null)).toThrow("manifest not loaded");
  });

  it("names a rung by its fraction of the canonical grid", () => {
    const ladder = [{ width: 720 }, { width: 360 }, { width: 180 }] as never[];
    expect(tierGlyph(null, ladder)).toBe("");
    expect(tierGlyph({ width: 720 } as never, ladder)).toBe("½");
    expect(tierGlyph({ width: 360 } as never, ladder)).toBe("¼");
    expect(tierGlyph({ width: 180 } as never, ladder)).toBe("⅛");
  });

  it("finds a frame offset by lead seconds on the session's own axis", () => {
    const session = { metadata: globalGrid(), leadOffsets: null } as unknown as VariableSession;
    expect(sessionOffsetForLead(session, 3600)).toBe(1);
    expect(sessionOffsetForLead(session, 1800)).toBeNull();
    expect(session.leadOffsets).not.toBeNull();
  });

  it("covers the whole plane when either side has no tiles", () => {
    const session = { metadata: globalGrid(), tiles: null } as unknown as VariableSession;
    expect(sessionCoverage(session, null)).toBe(WHOLE_PLANE_COVERAGE);
    expect(sessionCoverage(session, [])).toBe(WHOLE_PLANE_COVERAGE);
  });
});

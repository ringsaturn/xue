import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SETTLE_MS, createHistory } from "../../../web/src/workbench/history";

describe("the workbench's history", () => {
  let search: string;
  const pushed = vi.spyOn(window.history, "pushState");
  const replaced = vi.spyOn(window.history, "replaceState");
  const restored = vi.fn();
  let history: ReturnType<typeof createHistory>;

  beforeEach(() => {
    vi.useFakeTimers();
    window.history.replaceState(null, "", "/workbench?p=850");
    search = "?p=850";
    history = createHistory({ search: () => search, restored });
    vi.clearAllMocks();
  });
  afterEach(() => vi.useRealTimers());

  it("has one entry for one gesture", () => {
    search = "?p=700";
    history.commit();
    history.commit();
    expect(pushed).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?p=700");
  });

  it("waits out a run of keys", () => {
    const then = vi.fn();
    for (const level of [700, 500, 300]) {
      search = `?p=${level}`;
      history.soon(then);
      vi.advanceTimersByTime(SETTLE_MS - 1);
    }
    expect(pushed).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(pushed).toHaveBeenCalledTimes(1);
    expect(then).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?p=300");
  });

  it("settles what keys moved when something else comes to rest first", () => {
    const then = vi.fn();
    search = "?p=700";
    history.soon(then);
    search = "?p=700&time=2026-11-26T03Z";
    history.commit();
    expect(pushed).toHaveBeenCalledTimes(1);
    expect(then).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(SETTLE_MS);
    expect(pushed).toHaveBeenCalledTimes(1);
    expect(then).toHaveBeenCalledTimes(1);
  });

  it("replaces the entry for how the charts are drawn", () => {
    search = "?p=850&fill=rh";
    history.replace();
    expect(pushed).not.toHaveBeenCalled();
    expect(replaced).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?p=850&fill=rh");
  });

  it("leaves what keys have moved its own entry", () => {
    search = "?p=700";
    history.soon();
    search = "?p=700&fill=rh";
    history.replace();
    expect(replaced).not.toHaveBeenCalled();
    vi.advanceTimersByTime(SETTLE_MS);
    expect(pushed).toHaveBeenCalledTimes(1);
  });

  it("hands back the address the reader went to", () => {
    search = "?p=700";
    history.soon();
    window.history.replaceState(null, "", "/workbench?p=500");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(restored).toHaveBeenCalledWith("?p=500");
    vi.advanceTimersByTime(SETTLE_MS);
    // What was still settling went with the entry it was on.
    expect(pushed).not.toHaveBeenCalled();
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchImmutable } from "../../web/src/fetchimmutable";

describe("fetchImmutable", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the first response when the plain fetch succeeds", async () => {
    const response = new Response("ok");
    const fetch = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fetch);
    await expect(fetchImmutable("https://data.test/a.json?v=1")).resolves.toBe(response);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith("https://data.test/a.json?v=1", undefined);
  });

  it("retries once past the browser cache when the fetch is rejected", async () => {
    // A CORS rejection from a poisoned cache entry surfaces as a TypeError.
    const response = new Response("ok");
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("Load failed")).mockResolvedValueOnce(response);
    vi.stubGlobal("fetch", fetch);
    await expect(fetchImmutable("https://data.test/a.json?v=1", { headers: { Range: "bytes=0-0" } })).resolves.toBe(
      response,
    );
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1]).toEqual([
      "https://data.test/a.json?v=1",
      { headers: { Range: "bytes=0-0" }, cache: "reload" },
    ]);
  });

  it("does not hide a failure that is not a network error", async () => {
    const fetch = vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError"));
    vi.stubGlobal("fetch", fetch);
    await expect(fetchImmutable("https://data.test/a.json?v=1")).rejects.toThrow("aborted");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("surfaces the second failure when the retry fails too", async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetch);
    await expect(fetchImmutable("https://data.test/a.json?v=1")).rejects.toThrow("Failed to fetch");
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
